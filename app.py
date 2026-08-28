from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List
import os
import re
import time
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
SCRAPERAPI_ENDPOINT = "https://api.scraperapi.com/"


class ScanRequest(BaseModel):
    asins: List[str]
    marketplace: str = "UK"
    terms: List[str] = DEFAULT_TERMS


def normalize(text):
    text = str(text or "")

    for dash in ("–", "—", "−", "‐", "-"):
        text = text.replace(dash, "-")

    return re.sub(r"\s+", " ", text).strip()


def add_section(sections, name, text):
    text = normalize(text)

    if text and text not in sections.values():
        sections[name] = text


def extract_product_sections(soup):
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

    overview = soup.select_one(
        "#productOverview_feature_div"
    )

    if overview:
        add_section(
            sections,
            "Product overview",
            overview.get_text(" ", strip=True),
        )

    description = soup.select_one(
        "#productDescription"
    )

    if description:
        add_section(
            sections,
            "Description",
            description.get_text(" ", strip=True),
        )

    detail_selectors = [
        "#detailBullets_feature_div",
        "#productDetails_feature_div",
        "#productDetails_techSpec_section_1",
        "#productDetails_detailBullets_sections1",
        "#prodDetails",
    ]

    detail_number = 1

    for selector in detail_selectors:
        node = soup.select_one(selector)

        if node:
            add_section(
                sections,
                "Product details " + str(detail_number),
                node.get_text(" ", strip=True),
            )

            detail_number += 1

    extract_aplus(soup, sections)
    extract_regulatory(soup, sections)

    return sections


def extract_aplus(soup, sections):
    root = soup.select_one("#aplus_feature_div")

    if not root:
        root = soup.select_one("#aplus")

    if not root:
        return

    modules = root.select(".aplus-module")

    if not modules:
        modules = root.select(
            ".premium-aplus-module"
        )

    module_number = 1
    seen = set()

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
            "A+ module " + str(module_number),
            text,
        )

        module_number += 1

    add_section(
        sections,
        "A+ full content",
        root.get_text(
            " ",
            strip=True,
        ),
    )


def extract_regulatory(soup, sections):
    target = re.compile(
        r"organic inspection body code|"
        r"regulatory information|"
        r"[A-Z]{2,3}[\s\-–—−‐-]*BIO"
        r"[\s\-–—−‐-]*\d{2,3}",
        re.I,
    )

    seen = set()
    block_number = 1

    for text_node in soup.find_all(
        string=target
    ):
        node = text_node.parent
        chosen = ""

        for _ in range(7):
            if node is None:
                break

            candidate = normalize(
                node.get_text(
                    " ",
                    strip=True,
                )
            )

            candidate_lower = (
                candidate.lower()
            )

            has_regulatory_phrase = (
                "organic inspection body code"
                in candidate_lower
                or
                "regulatory information"
                in candidate_lower
            )

            has_bio_code = bool(
                re.search(
                    r"[A-Z]{2,3}"
                    r"[\s-]*BIO"
                    r"[\s-]*\d{2,3}",
                    candidate,
                    re.I,
                )
            )

            if (
                20 <= len(candidate) <= 1800
                and (
                    has_regulatory_phrase
                    or has_bio_code
                )
            ):
                chosen = candidate
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
                + str(block_number),
                chosen,
            )

            block_number += 1


def make_term_pattern(term):
    term = normalize(term)
    lower = term.lower()

    if not term:
        return None

    if lower == "organic":
        return re.compile(
            r"\borganic(?:ally)?\b",
            re.I,
        )

    if lower == "lu-bio-04":
        return re.compile(
            r"LU[\s-]*BIO[\s-]*04",
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


def find_matches(sections, terms):
    results = []
    seen = set()

    for section, text in sections.items():
        patterns = []

        for term in terms:
            pattern = make_term_pattern(term)

            if pattern is not None:
                patterns.append(
                    (
                        normalize(term),
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
                first.end() + 300,
            )

            results.append(
                {
                    "term": label,
                    "section": section,
                    "snippet": text[left:right],
                    "full_text": text,
                    "occurrences": len(found),
                }
            )

    return results


def scraper_error_code(status_code):
    if status_code == 401:
        return "E110 SCRAPERAPI_KEY_ERROR"

    if status_code == 403:
        return "E102 SCRAPERAPI_HTTP_403"

    if status_code == 429:
        return "E103 SCRAPERAPI_HTTP_429"

    if 500 <= status_code <= 599:
        return (
            "E104 SCRAPERAPI_HTTP_"
            + str(status_code)
        )

    return (
        "E120 SCRAPERAPI_HTTP_"
        + str(status_code)
    )


def fetch_amazon_page(
    url,
    marketplace,
):
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
            "render": "true",
        },
        timeout=35,
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
                "E110 SCRAPERAPI_KEY_ERROR: "
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
            "http_status": None,
            "fetch_stage": (
                "rendered_amazon_html"
            ),
            "sections_found": [],
        }

        try:
            response = fetch_amazon_page(
                url,
                marketplace,
            )

            if response is None:
                item["status"] = (
                    "fetch_failed"
                )

                item["error_code"] = (
                    "E110 "
                    "SCRAPERAPI_KEY_ERROR"
                )

                item["warning"] = (
                    "E110 SCRAPERAPI_KEY_ERROR: "
                    "SCRAPERAPI_KEY is missing "
                    "in Render."
                )

                results.append(item)
                continue

            item["http_status"] = (
                response.status_code
            )

            if response.status_code != 200:
                code = scraper_error_code(
                    response.status_code
                )

                item["status"] = (
                    "fetch_failed"
                )

                item["error_code"] = code

                item["warning"] = (
                    code
                    + ": ScraperAPI returned HTTP "
                    + str(response.status_code)
                    + "."
                )

                results.append(item)
                continue

            page_lower = (
                response.text.lower()
            )

            if (
                "enter the characters "
                "you see below"
                in page_lower
            ):
                item["status"] = "blocked"

                item["error_code"] = (
                    "E130 AMAZON_CAPTCHA"
                )

                item["warning"] = (
                    "E130 AMAZON_CAPTCHA: "
                    "Amazon returned a "
                    "robot/CAPTCHA page."
                )

                results.append(item)
                continue

            soup = BeautifulSoup(
                response.text,
                "html.parser",
            )

            sections =