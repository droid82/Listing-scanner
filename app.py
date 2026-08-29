from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List
import os
import re
import html as html_lib
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

BUILT_IN_ORGANIC_TERMS = [
    "organic",
    "organically",
    "certified organic",
    "organic certificate",
    "organic certification",
    "organic inspection body",
    "bio",
    "eco",
    "ecological",
]

SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "").strip()
SCRAPERAPI_URL = "https://api.scraperapi.com/"


class ScanRequest(BaseModel):
    asins: List[str]
    marketplace: str = "UK"
    terms: List[str] = DEFAULT_TERMS


def normalize(text):
    text = html_lib.unescape(str(text or ""))

    for dash in ("–", "—", "−", "‐", "-"):
        text = text.replace(dash, "-")

    return re.sub(r"\s+", " ", text).strip()


def add_section(sections, name, text):
    text = normalize(text)

    if text and text not in sections.values():
        sections[name] = text


def extract_standard_sections(soup, sections):
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

    selectors = [
        ("Product overview", "#productOverview_feature_div"),
        ("Description", "#productDescription"),
        ("Product details", "#detailBullets_feature_div"),
        ("Product details 2", "#productDetails_feature_div"),
        ("Product details 3", "#productDetails_techSpec_section_1"),
        ("Product details 4", "#productDetails_detailBullets_sections1"),
        ("Important information", "#important-information"),
        ("Important information 2", "#importantInformation"),
        ("Safety information", "#safety-information"),
        ("Ingredients", "#ingredients"),
        ("Directions", "#directions"),
    ]

    for name, selector in selectors:
        node = soup.select_one(selector)

        if node:
            add_section(
                sections,
                name,
                node.get_text(" ", strip=True),
            )


def extract_aplus(soup, sections):
    root = (
        soup.select_one("#aplus_feature_div")
        or soup.select_one("#aplus")
    )

    if not root:
        return

    modules = root.select(
        ".aplus-module, .premium-aplus-module"
    )

    seen = set()
    number = 1

    for module in modules:
        text = normalize(
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
        root.get_text(
            " ",
            strip=True,
        ),
    )


def extract_regulatory(soup, raw_html, sections):
    marker = re.compile(
        r"organic inspection body code|"
        r"regulatory information|"
        r"safety and product resources|"
        r"\b[A-Z]{2,3}[\s\-–—−‐-]*BIO"
        r"[\s\-–—−‐-]*\d{2,3}\b",
        re.I,
    )

    seen = set()
    number = 1

    for text_node in soup.find_all(
        string=marker
    ):
        node = text_node.parent
        chosen = ""

        for _ in range(8):
            if node is None:
                break

            candidate = normalize(
                node.get_text(
                    " ",
                    strip=True,
                )
            )

            if (
                20 <= len(candidate) <= 3000
                and marker.search(candidate)
            ):
                chosen = candidate

                if (
                    "organic inspection body code"
                    in candidate.lower()
                    or re.search(
                        r"\b[A-Z]{2,3}"
                        r"[\s-]*BIO"
                        r"[\s-]*\d{2,3}\b",
                        candidate,
                        re.I,
                    )
                ):
                    break

            node = node.parent

        if (
            chosen
            and chosen not in seen
        ):
            seen.add(chosen)

            add_section(
                sections,
                "Regulatory information "
                + str(number),
                chosen,
            )

            number += 1

    raw_normalized = normalize(
        raw_html
    )

    fallback = re.compile(
        r".{0,500}"
        r"(?:organic inspection body code|"
        r"\b[A-Z]{2,3}[\s-]*BIO"
        r"[\s-]*\d{2,3}\b)"
        r".{0,900}",
        re.I | re.S,
    )

    for raw_match in fallback.finditer(
        raw_normalized
    ):
        fragment = raw_match.group(0)

        text = normalize(
            BeautifulSoup(
                fragment,
                "html.parser",
            ).get_text(
                " ",
                strip=True,
            )
        )

        if (
            text
            and text not in seen
        ):
            seen.add(text)

            add_section(
                sections,
                "Regulatory source data "
                + str(number),
                text,
            )

            number += 1


def parse_page(raw_html):
    soup = BeautifulSoup(
        raw_html,
        "html.parser",
    )

    sections = {}

    extract_standard_sections(
        soup,
        sections,
    )

    extract_aplus(
        soup,
        sections,
    )

    extract_regulatory(
        soup,
        raw_html,
        sections,
    )

    return sections


def make_pattern(term):
    term = normalize(term)
    lower = term.lower()

    if not term:
        return None

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

    if re.fullmatch(
        r"[A-Za-z0-9]+",
        term,
    ):
        return re.compile(
            r"(?<![A-Za-z0-9])"
            + re.escape(term)
            + r"(?![A-Za-z0-9])",
            re.I,
        )

    return re.compile(
        re.escape(term),
        re.I,
    )


def build_search_terms(user_terms):
    combined = []

    for term in (
        list(user_terms)
        + BUILT_IN_ORGANIC_TERMS
    ):
        term = normalize(term)

        if not term:
            continue

        if term.lower() not in [
            x.lower()
            for x in combined
        ]:
            combined.append(term)

    return combined


def find_matches(sections, terms):
    matches = []
    seen = set()

    patterns = []

    for term in terms:
        pattern = make_pattern(term)

        if pattern is not None:
            patterns.append(
                (
                    term,
                    pattern,
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
        for label, pattern in patterns:
            found = list(
                pattern.finditer(text)
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
                first.end() + 320,
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


def error_code_for_http(status):
    if status == 401:
        return (
            "E110 "
            "SCRAPERAPI_KEY_ERROR"
        )

    if status == 403:
        return (
            "E102 "
            "SCRAPERAPI_HTTP_403"
        )

    if status == 429:
        return (
            "E103 "
            "SCRAPERAPI_HTTP_429"
        )

    if 500 <= status <= 599:
        return (
            "E104 "
            "SCRAPERAPI_HTTP_"
            + str(status)
        )

    return (
        "E120 "
        "SCRAPERAPI_HTTP_"
        + str(status)
    )


def scraper_request(
    url,
    marketplace,
    render=False,
):
    country = (
        "uk"
        if marketplace == "UK"
        else marketplace.lower()
    )

    params = {
        "api_key": SCRAPERAPI_KEY,
        "url": url,
        "country_code": country,
    }

    if render:
        params["render"] = "true"

    return requests.get(
        SCRAPERAPI_URL,
        params=params,
        timeout=(
            25
            if not render
            else 35
        ),
    )


def page_is_captcha(text):
    lower = text.lower()

    return (
        "enter the characters "
        "you see below"
        in lower
        or
        "sorry, we just need "
        "to make sure you're "
        "not a robot"
        in lower
    )


@app.post("/api/scan")
def scan(req: ScanRequest):
    marketplace = (
        req.marketplace.upper()
    )

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
                "SCRAPERAPI_KEY_ERROR: "
                "SCRAPERAPI_KEY is missing "
                "in Render."
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
            detail=(
                "E002 "
                "NO_VALID_ASINS"
            ),
        )

    search_terms = (
        build_search_terms(
            req.terms
        )
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
            "http_status": None,
            "fetch_stage": "raw_html",
            "sections_found": [],
        }

        try:
            response = scraper_request(
                url,
                marketplace,
                render=False,
            )

            item["http_status"] = (
                response.status_code
            )

            if (
                response.status_code
                != 200
            ):
                code = (
                    error_code_for_http(
                        response.status_code
                    )
                )

                item["status"] = (
                    "fetch_failed"
                )

                item["error_code"] = code

                item["warning"] = (
                    code
                    + ": ScraperAPI raw "
                    "HTML request returned HTTP "
                    + str(
                        response.status_code
                    )
                    + "."
                )

                results.append(item)
                continue

            if page_is_captcha(
                response.text
            ):
                item["status"] = (
                    "blocked"
                )

                item["error_code"] = (
                    "E130 "
                    "AMAZON_CAPTCHA"
                )

                item["warning"] = (
                    "E130 AMAZON_CAPTCHA: "
                    "Amazon returned a "
                    "robot/CAPTCHA page."
                )

                results.append(item)
                continue

            sections = parse_page(
                response.text
            )

            has_regulatory = any(
                name.startswith(
                    "Regulatory"
                )
                for name in sections
            )

            has_aplus = any(
                name.startswith("A+")
                for name in sections
            )

            raw_mentions_aplus = (
                "aplus"
                in response.text.lower()
                or
                "a-plus"
                in response.text.lower()
            )

            needs_render_fallback = (
                not has_regulatory
                or (
                    raw_mentions_aplus
                    and not has_aplus
                )
            )

            if needs_render_fallback:
                try:
                    rendered = (
                        scraper_request(
                            url,
                            marketplace,
                            render=True,
                        )
                    )

                    if (
                        rendered.status_code
                        == 200
                        and not page_is_captcha(
                            rendered.text
                        )
                    ):
                        rendered_sections = (
                            parse_page(
                                rendered.text
                            )
                        )

                        for (
                            name,
                            text,
                        ) in (
                            rendered_sections.items()
                        ):
                            if (
                                name
                                not in sections
                            ):
                                sections[
                                    name
                                ] = text

                        item[
                            "fetch_stage"
                        ] = (
                            "raw_html+"
                            "render_fallback"
                        )

                    elif (
                        rendered.status_code
                        != 200
                    ):
                        item[
                            "warning"
                        ] = (
                            error_code_for_http(
                                rendered.status_code
                            )
                            + ": render fallback "
                            "returned HTTP "
                            + str(
                                rendered.status_code
                            )
                            + "."
                        )

                except requests.Timeout:
                    item[
                        "warning"
                    ] = (
                        "E101 "
                        "SCRAPERAPI_TIMEOUT: "
                        "render fallback timed "
                        "out; raw HTML result "
                        "was kept."
                    )

            item["title"] = (
                sections.get(
                    "Title",
                    "",
                )
            )

            item[
                "sections_found"
            ] = list(
                sections.keys()
            )

            item["matches"] = (
                find_matches(
                    sections,
                    search_terms,
                )
            )

            has_regulatory = any(
                name.startswith(
                    "Regulatory"
                )
                for name in sections
            )

            if item["matches"]:
                item["status"] = (
                    "matched"
                )

            elif not item["title"]:
                item["status"] = (
                    "incomplete"
                )

                item["error_code"] = (
                    "E106 "
                    "AMAZON_DATA_EMPTY"
                )

                item["warning"] = (
                    "E106 AMAZON_DATA_EMPTY: "
                    "Amazon HTML was returned, "
                    "but the product title "
                    "could not be extracted."
                )

            elif not has_regulatory:
                item["status"] = (
                    "incomplete"
                )

                item["error_code"] = (
                    "E108 "
                    "REGULATORY_DATA_NOT_RETURNED"
                )

                if not item["warning"]:
                    item["warning"] = (
                        "E108 "
                        "REGULATORY_DATA_NOT_RETURNED: "
                        "product content was "
                        "retrieved, but Amazon "
                        "Regulatory Information "
                        "was not exposed in the "
                        "raw HTML or render "
                        "fallback. This ASIN is "
                        "not confirmed clear."
                    )

            else:
                item["status"] = (
                    "clear"
                )

            results.append(item)

        except requests.Timeout:
            item["status"] = (
                "fetch_failed"
            )

            item["error_code"] = (
                "E101 "
                "SCRAPERAPI_TIMEOUT"
            )

            item["warning"] = (
                "E101 SCRAPERAPI_TIMEOUT: "
                "ScraperAPI did not return "
                "the Amazon page before "
                "the request timeout."
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
    return FileResponse(
        "index.html"
    )


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(
        "manifest.webmanifest",
        media_type=(
            "application/manifest+json"
        ),
    )


@app.get("/sw.js")
def service_worker():
    return FileResponse(
        "sw.js",
        media_type=(
            "application/javascript"
        ),
    )