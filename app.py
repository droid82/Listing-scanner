from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List
import os
import re
import requests
from bs4 import BeautifulSoup

app = FastAPI(title="Amazon Live Listing Scanner")

MARKETPLACES = {
    "UK": "https://www.amazon.co.uk/dp/{}",
    "DE": "https://www.amazon.de/dp/{}",
    "FR": "https://www.amazon.fr/dp/{}",
    "IT": "https://www.amazon.it/dp/{}",
    "ES": "https://www.amazon.es/dp/{}",
    "NL": "https://www.amazon.nl/dp/{}",
    "IE": "https://www.amazon.ie/dp/{}",
}

DEFAULT_TERMS = ["organic", "bio", "eco", "lu-bio-04"]

SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "").strip()
SCRAPERAPI_URL = "https://api.scraperapi.com/"


class ScanRequest(BaseModel):
    asins: List[str]
    marketplace: str = "UK"
    terms: List[str] = DEFAULT_TERMS


def normalise(text):
    text = str(text or "")

    for dash in ("–", "—", "−", "‐", "-"):
        text = text.replace(dash, "-")

    return re.sub(r"\s+", " ", text).strip()


def add_section(sections, name, text):
    text = normalise(text)

    if text and text not in sections.values():
        sections[name] = text


def extract_sections(soup, raw_html):
    sections = {}

    title = soup.select_one("#productTitle")

    if title:
        add_section(
            sections,
            "Title",
            title.get_text(" ", strip=True),
        )

    bullets = soup.select(
        "#feature-bullets li span.a-list-item"
    )

    if bullets:
        add_section(
            sections,
            "Bullet points",
            " | ".join(
                x.get_text(" ", strip=True)
                for x in bullets
            ),
        )

    for name, selector in [
        ("Product overview", "#productOverview_feature_div"),
        ("Description", "#productDescription"),
        ("Product details", "#detailBullets_feature_div"),
        ("Product details 2", "#productDetails_feature_div"),
    ]:
        node = soup.select_one(selector)

        if node:
            add_section(
                sections,
                name,
                node.get_text(" ", strip=True),
            )

    aplus = (
        soup.select_one("#aplus_feature_div")
        or soup.select_one("#aplus")
    )

    if aplus:
        modules = aplus.select(
            ".aplus-module, .premium-aplus-module"
        )

        seen = set()
        number = 1

        for module in modules:
            text = normalise(
                module.get_text(
                    " ",
                    strip=True,
                )
            )

            if len(text) < 20:
                continue

            if text in seen:
                continue

            seen.add(text)

            add_section(
                sections,
                "A+ module " + str(number),
                text,
            )

            number += 1

        add_section(
            sections,
            "A+ full content",
            aplus.get_text(
                " ",
                strip=True,
            ),
        )

    regulatory_regex = re.compile(
        r"organic inspection body code|"
        r"regulatory information|"
        r"[A-Z]{2,3}[\s\-–—−‐-]*BIO"
        r"[\s\-–—−‐-]*\d{2,3}",
        re.I,
    )

    regulatory_seen = set()
    regulatory_number = 1

    for text_node in soup.find_all(
        string=regulatory_regex
    ):
        node = text_node.parent
        chosen = ""

        for _ in range(7):
            if node is None:
                break

            candidate = normalise(
                node.get_text(
                    " ",
                    strip=True,
                )
            )

            if (
                20 <= len(candidate) <= 2000
                and regulatory_regex.search(candidate)
            ):
                chosen = candidate
                break

            node = node.parent

        if (
            chosen
            and chosen not in regulatory_seen
        ):
            regulatory_seen.add(chosen)

            add_section(
                sections,
                "Regulatory information "
                + str(regulatory_number),
                chosen,
            )

            regulatory_number += 1

    raw_text = normalise(
        BeautifulSoup(
            raw_html,
            "html.parser",
        ).get_text(
            " ",
            strip=True,
        )
    )

    raw_match = re.search(
        r".{0,350}"
        r"(organic inspection body code|"
        r"[A-Z]{2,3}[\s-]*BIO[\s-]*\d{2,3})"
        r".{0,650}",
        raw_text,
        re.I,
    )

    if raw_match:
        add_section(
            sections,
            "Regulatory raw text",
            raw_match.group(0),
        )

    return sections


def term_regex(term):
    term = normalise(term)
    lower = term.lower()

    if lower == "organic":
        return re.compile(
            r"\borganic(?:ally)?\b",
            re.I,
        )

    if lower == "bio":
        return re.compile(
            r"(?<![A-Za-z0-9])"
            r"bio"
            r"(?![A-Za-z0-9])",
            re.I,
        )

    if lower == "eco":
        return re.compile(
            r"(?<![A-Za-z0-9])"
            r"eco"
            r"(?![A-Za-z0-9])",
            re.I,
        )

    if lower == "lu-bio-04":
        return re.compile(
            r"LU[\s-]*BIO[\s-]*04",
            re.I,
        )

    if not term:
        return None

    return re.compile(
        re.escape(term),
        re.I,
    )


def find_matches(sections, terms):
    matches = []
    seen = set()
    patterns = []

    for term in terms:
        regex = term_regex(term)

        if regex:
            patterns.append(
                (
                    normalise(term),
                    regex,
                )
            )

    patterns.append(
        (
            "organic control-body code",
            re.compile(
                r"\b[A-Z]{2,3}"
                r"[\s-]*BIO"
                r"[\s-]*\d{2,3}\b",
                re.I,
            ),
        )
    )

    for section, text in sections.items():
        for label, regex in patterns:
            found = list(
                regex.finditer(text)
            )

            if not found:
                continue

            key = (
                section,
                label.lower(),
            )

            if key in seen:
                continue

            seen.add(key)

            first = found[0]

            left = max(
                0,
                first.start() - 180,
            )

            right = min(
                len(text),
                first.end() + 300,
            )

            matches.append(
                {
                    "term": label,
                    "section": section,
                    "snippet": text[left:right],
                    "full_text": text,
                    "occurrences": len(found),
                }
            )

    return matches


def fetch_amazon(url, marketplace):
    country = (
        "uk"
        if marketplace == "UK"
        else marketplace.lower()
    )

    return requests.get(
        SCRAPERAPI_URL,
        params={
            "api_key": SCRAPERAPI_KEY,
            "url": url,
            "country_code": country,
            "render": "true",
        },
        timeout=30,
    )


def http_error_code(status):
    if status == 401:
        return "E110 SCRAPERAPI_KEY_ERROR"

    if status == 403:
        return "E102 SCRAPERAPI_HTTP_403"

    if status == 429:
        return "E103 SCRAPERAPI_HTTP_429"

    if 500 <= status <= 599:
        return (
            "E104 SCRAPERAPI_HTTP_"
            + str(status)
        )

    return (
        "E120 SCRAPERAPI_HTTP_"
        + str(status)
    )


@app.post("/api/scan")
def scan(req: ScanRequest):
    marketplace = req.marketplace.upper()

    if marketplace not in MARKETPLACES:
        raise HTTPException(
            status_code=400,
            detail=(
                "E001 "
                "UNSUPPORTED_MARKETPLACE"
            ),
        )

    if not SCRAPERAPI_KEY:
        raise HTTPException(
            status_code=500,
            detail=(
                "E110 "
                "SCRAPERAPI_KEY_ERROR"
            ),
        )

    asins = []

    for raw in req.asins:
        asin = re.sub(
            r"[^A-Z0-9]",
            "",
            raw.upper(),
        )

        if (
            len(asin) == 10
            and asin not in asins
        ):
            asins.append(asin)

    if not asins:
        raise HTTPException(
            status_code=400,
            detail="E002 NO_VALID_ASINS",
        )

    results = []

    for asin in asins[:50]:
        url = MARKETPLACES[
            marketplace
        ].format(asin)

        item = {
            "asin": asin,
            "url": url,
            "status": "unknown",
            "title": "",
            "matches": [],
            "warning": None,
            "error_code": None,
            "sections_found": [],
        }

        try:
            response = fetch_amazon(
                url,
                marketplace,
            )

            if response.status_code != 200:
                code = http_error_code(
                    response.status_code
                )

                item["status"] = (
                    "fetch_failed"
                )

                item["error_code"] = code

                item["warning"] = (
                    code
                    + ": HTTP "
                    + str(
                        response.status_code
                    )
                )

                results.append(item)
                continue

            lower = response.text.lower()

            if (
                "enter the characters you see below"
                in lower
            ):
                item["status"] = "blocked"
                item["error_code"] = (
                    "E130 AMAZON_CAPTCHA"
                )
                item["warning"] = (
                    "E130 AMAZON_CAPTCHA"
                )

                results.append(item)
                continue

            soup = BeautifulSoup(
                response.text,
                "html.parser",
            )

            sections = extract_sections(
                soup,
                response.text,
            )

            item["title"] = (
                sections.get(
                    "Title",
                    "",
                )
            )

            item["sections_found"] = (
                list(
                    sections.keys()
                )
            )

            item["matches"] = find_matches(
                sections,
                req.terms,
            )

            has_aplus = any(
                name.startswith("A+")
                for name in sections
            )

            has_regulatory = any(
                name.startswith(
                    "Regulatory"
                )
                for name in sections
            )

            if item["matches"]:
                item["status"] = "matched"

            elif not item["title"]:
                item["status"] = (
                    "incomplete"
                )

                item["error_code"] = (
                    "E106 AMAZON_DATA_EMPTY"
                )

                item["warning"] = (
                    "E106 AMAZON_DATA_EMPTY: "
                    "no product title extracted"
                )

            elif (
                not has_aplus
                and not has_regulatory
            ):
                item["status"] = (
                    "incomplete"
                )

                item["error_code"] = (
                    "E107 "
                    "TARGET_SECTIONS_NOT_RETURNED"
                )

                item["warning"] = (
                    "E107 "
                    "TARGET_SECTIONS_NOT_RETURNED: "
                    "A+ and Regulatory Information "
                    "were absent from fetched HTML"
                )

            elif not has_regulatory:
                item["status"] = (
                    "incomplete"
                )

                item["error_code"] = (
                    "E108 "
                    "REGULATORY_DATA_NOT_RETURNED"
                )

                item["warning"] = (
                    "E108 "
                    "REGULATORY_DATA_NOT_RETURNED"
                )

            else:
                item["status"] = "clear"

            results.append(item)

        except requests.Timeout:
            item["status"] = (
                "fetch_failed"
            )

            item["error_code"] = (
                "E101 SCRAPERAPI_TIMEOUT"
            )

            item["warning"] = (
                "E101 SCRAPERAPI_TIMEOUT: "
                "no response within 30 seconds"
            )

            results.append(item)

        except requests.ConnectionError as error:
            item["status"] = (
                "fetch_failed"
            )

            item["error_code"] = (
                "E109 "
                "UPSTREAM_CONNECTION_ERROR"
            )

            item["warning"] = (
                "E109 "
                "UPSTREAM_CONNECTION_ERROR: "
                + str(error)
            )

            results.append(item)

        except requests.RequestException as error:
            item["status"] = (
                "fetch_failed"
            )

            item["error_code"] = (
                "E111 "
                "SCRAPERAPI_REQUEST_ERROR"
            )

            item["warning"] = (
                "E111 "
                "SCRAPERAPI_REQUEST_ERROR: "
                + str(error)
            )

            results.append(item)

        except Exception as error:
            item["status"] = (
                "fetch_failed"
            )

            item["error_code"] = (
                "E199 "
                "APP_PROCESSING_ERROR"
            )

            item["warning"] = (
                "E199 "
                "APP_PROCESSING_ERROR: "
                + type(error).__name__
                + ": "
                + str(error)
            )

            results.append(item)

    return {
        "marketplace": marketplace,
        "count": len(results),
        "results": results,
    }


@app.get("/")
def home():
    return FileResponse("index.html")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(
        "manifest.webmanifest",
        media_type="application/manifest+json",
    )


@app.get("/sw.js")
def service_worker():
    return FileResponse(
        "sw.js",
        media_type="application/javascript",
    )