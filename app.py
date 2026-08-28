from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List
import os
import re
import time
import html
import requests
from bs4 import BeautifulSoup

app = FastAPI()

MARKETPLACES = {
    "UK": "https://www.amazon.co.uk/dp/{}",
    "DE": "https://www.amazon.de/dp/{}",
    "FR": "https://www.amazon.fr/dp/{}",
    "IT": "https://www.amazon.it/dp/{}",
    "ES": "https://www.amazon.es/dp/{}",
    "NL": "https://www.amazon.nl/dp/{}",
    "IE": "https://www.amazon.ie/dp/{}"
}

DEFAULT_TERMS = [
    "organic",
    "bio",
    "eco",
    "lu-bio-04"
]

SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "").strip()
SCRAPERAPI_ENDPOINT = "https://api.scraperapi.com/"


class ScanRequest(BaseModel):
    asins: List[str]
    marketplace: str = "UK"
    terms: List[str] = DEFAULT_TERMS


def normalize(text):
    text = html.unescape(text or "")

    # Convert Amazon's various long dashes to normal hyphens
    text = (
        text
        .replace("–", "-")
        .replace("—", "-")
        .replace("−", "-")
        .replace("‐", "-")
        .replace("-", "-")
    )

    return re.sub(r"\s+", " ", text).strip()


def add_section(sections, name, text):
    text = normalize(text)

    if not text:
        return

    if text not in sections.values():
        sections[name] = text


def extract_normal_sections(soup):
    sections = {}

    title = soup.select_one("#productTitle")
    if title:
        add_section(
            sections,
            "Title",
            title.get_text(" ", strip=True)
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
            )
        )

    overview = soup.select_one(
        "#productOverview_feature_div"
    )

    if overview:
        add_section(
            sections,
            "Product overview",
            overview.get_text(" ", strip=True)
        )

    description = soup.select_one(
        "#productDescription"
    )

    if description:
        add_section(
            sections,
            "Description",
            description.get_text(" ", strip=True)
        )

    detail_selectors = [
        "#detailBullets_feature_div",
        "#productDetails_feature_div",
        "#productDetails_techSpec_section_1",
        "#productDetails_detailBullets_sections1",
        "#prodDetails"
    ]

    for number, selector in enumerate(
        detail_selectors,
        start=1
    ):
        node = soup.select_one(selector)

        if node:
            add_section(
                sections,
                "Product details " + str(number),
                node.get_text(" ", strip=True)
            )

    return sections


def extract_aplus(soup, sections):
    aplus = soup.select_one(
        "#aplus_feature_div"
    )

    if not aplus:
        aplus = soup.select_one("#aplus")

    if not aplus:
        return

    modules = aplus.select(".aplus-module")

    if not modules:
        modules = aplus.select(
            ".premium-aplus-module"
        )

    number = 1
    seen = set()

    for module in modules:
        text = normalize(
            module.get_text(" ", strip=True)
        )

        if len(text) < 20:
            continue

        if text in seen:
            continue

        seen.add(text)

        sections[
            "A+ module " + str(number)
        ] = text

        number += 1

    add_section(
        sections,
        "A+ full content",
        aplus.get_text(" ", strip=True)
    )


def extract_regulatory(soup, raw_html, sections):
    regulatory_patterns = [
        r"organic inspection body code",
        r"regulatory information",
        r"LU[\s\-–—−‐-]*BIO[\s\-–—−‐-]*04"
    ]

    combined = re.compile(
        "|".join(regulatory_patterns),
        re.I
    )

    # First search actual rendered DOM text
    matches = soup.find_all(
        string=combined
    )

    regulatory_number = 1
    seen = set()

    for match in matches:
        node = match.parent

        # Walk upward until we get enough useful context
        for _ in range(6):
            if node is None:
                break

            text = normalize(
                node.get_text(" ", strip=True)
            )

            if 25 <= len(text) <= 2500:
                if (
                    "organic inspection body code"
                    in text.lower()
                    or
                    "regulatory information"
                    in text.lower()
                    or
                    re.search(
                        r"LU[\s\-]*BIO[\s\-]*04",
                        text,
                        re.I
                    )
                ):
                    if text not in seen:
                        seen.add(text)

                        sections[
                            "Regulatory information "
                            + str(regulatory_number)
                        ] = text

                        regulatory_number += 1

                    break

            node = node.parent

    # Fallback:
    # Amazon sometimes embeds the regulatory block inside
    # script/JSON rather than ordinary visible DOM.
    normalized_raw = normalize(raw_html)

    regulatory_regex = re.compile(
        r".{0,500}"
        r"(?:"
        r"organic inspection body code"
        r"|LU[\s\-]*BIO[\s\-]*04"
        r")"
        r".{0,800}",
        re.I
    )

    for match in regulatory_regex.finditer(
        normalized_raw
    ):
        text = normalize(match.group(0))

        # Strip obvious HTML tags if raw markup remains
        text = normalize(
            BeautifulSoup(
                text,
                "html.parser"
            ).get_text(" ", strip=True)
        )

        if text and text not in seen:
            seen.add(text)

            sections[
                "Regulatory raw data "
                + str(regulatory_number)
            ] = text

            regulatory_number += 1


def term_pattern(term):
    term = normalize(term)

    if not term:
        return None

    if term.lower() == "lu-bio-04":
        return re.compile(
            r"LU[\s\-]*BIO[\s\-]*04",
            re.I
        )

    if re.fullmatch(
        r"[A-Za-z0-9]+",
        term
    ):
        return re.compile(
            r"(?<![A-Za-z0-9])"
            + re.escape(term)
            + r"(?![A-Za-z0-9])",
            re.I
        )

    return re.compile(
        re.escape(term),
        re.I
    )


def find_matches(sections, terms):
    results = []
    seen = set()

    for section, text in sections.items():
        for term in terms:
            pattern = term_pattern(term)

            if pattern is None:
                continue

            matches = list(
                pattern.finditer(text)
            )

            if not matches:
                continue

            first = matches[0]

            left = max(
                0,
                first.start() - 180
            )

            right = min(
                len(text),
                first.end() + 300
            )

            key = (
                section,
                normalize(term).lower()
            )

            if key in seen:
                continue

            seen.add(key)

            results.append({
                "term": normalize(term),
                "section": section,
                "snippet": text[left:right],
                "full_text": text,
                "occurrences": len(matches)
            })

    return results


def fetch_page(url, marketplace):
    if not SCRAPERAPI_KEY:
        return None

    country = (
        "uk"
        if marketplace == "UK"
        else marketplace.lower()
    )

    return requests.get(
        SCRAPERAPI_ENDPOINT,
        params={
            "api_key": SCRAPERAPI_KEY,
            "url": url,
            "country_code": country,
            "render": "true"
        },
        timeout=70
    )


@app.post("/api/scan")
def scan(req: ScanRequest):
    marketplace = req.marketplace.upper()

    if marketplace not in MARKETPLACES:
        raise HTTPException(
            status_code=400,
            detail="Unsupported marketplace"
        )

    asins = []

    for raw in req.asins:
        asin = re.sub(
            r"[^A-Z0-9]",
            "",
            raw.upper()
        )

        if (
            len(asin) == 10
            and asin not in asins
        ):
            asins.append(asin)

    if not asins:
        raise HTTPException(
            status_code=400,
            detail="No valid ASINs supplied"
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
            "warning": None
        }

        try:
            response = fetch_page(
                url,
                marketplace
            )

            if response is None:
                item["status"] = "fetch_failed"
                item["warning"] = (
                    "SCRAPERAPI_KEY is missing."
                )

                results.append(item)
                continue

            if response.status_code != 200:
                item["status"] = "fetch_failed"
                item["warning"] = (
                    "ScraperAPI returned HTTP "
                    + str(response.status_code)
                )

                results.append(item)
                continue

            lower = response.text.lower()

            if (
                "enter the characters you see below"
                in lower
            ):
                item["status"] = "blocked"
                item["warning"] = (
                    "Amazon returned a CAPTCHA."
                )

                results.append(item)
                continue

            soup = BeautifulSoup(
                response.text,
                "html.parser"
            )

            sections = extract_normal_sections(
                soup
            )

            extract_aplus(
                soup,
                sections
            )

            extract_regulatory(
                soup,
                response.text,
                sections
            )

            item["title"] = sections.get(
                "Title",
                ""
            )

            item["matches"] = find_matches(
                sections,
                req.terms
            )

            item["sections_found"] = list(
                sections.keys()
            )

            if item["matches"]:
                item["status"] = "matched"

            elif len(sections) <= 1:
                # Don't falsely call it clear when
                # ScraperAPI only returned the title.
                item["status"] = "incomplete"
                item["warning"] = (
                    "Only limited Amazon listing data "
                    "was retrieved. Result is not verified clear."
                )

            else:
                item["status"] = "clear"

            results.append(item)

        except requests.RequestException as error:
            item["status"] = "fetch_failed"
            item["warning"] = str(error)
            results.append(item)

        time.sleep(0.5)

    return {
        "marketplace": marketplace,
        "count": len(results),
        "results": results
    }


@app.get("/")
def home():
    return FileResponse("index.html")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(
        "manifest.webmanifest",
        media_type="application/manifest+json"
    )


@app.get("/sw.js")
def service_worker():
    return FileResponse(
        "sw.js",
        media_type="application/javascript"
    )