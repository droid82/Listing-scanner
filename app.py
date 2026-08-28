from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List
import os
import re
import time
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

SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "").strip()
SCRAPERAPI_ENDPOINT = "https://api.scraperapi.com/"


class ScanRequest(BaseModel):
    asins: List[str]
    marketplace: str = "UK"
    terms: List[str] = DEFAULT_TERMS


def normalize(text):
    text = html_lib.unescape(text or "")
    text = text.replace("–", "-")
    text = text.replace("—", "-")
    text = text.replace("−", "-")
    text = text.replace("‐", "-")
    text = text.replace("-", "-")
    return re.sub(r"\s+", " ", text).strip()


def add_section(sections, name, text):
    text = normalize(text)

    if not text:
        return

    if text not in sections.values():
        sections[name] = text


def extract_main_sections(soup):
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
        bullet_text = " | ".join(
            x.get_text(" ", strip=True)
            for x in bullets
            if x.get_text(" ", strip=True)
        )

        add_section(
            sections,
            "Bullet points",
            bullet_text,
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

    return sections


def extract_aplus_sections(soup, sections):
    aplus = soup.select_one(
        "#aplus_feature_div"
    )

    if not aplus:
        aplus = soup.select_one("#aplus")

    if not aplus:
        return

    modules = aplus.select(
        ".aplus-module"
    )

    if not modules:
        modules = aplus.select(
            ".premium-aplus-module"
        )

    seen = set()
    module_number = 1

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
        aplus.get_text(
            " ",
            strip=True,
        ),
    )


def extract_regulatory_sections(
    soup,
    raw_html,
    sections,
):
    regulatory_phrase = re.compile(
        r"organic inspection body code|"
        r"regulatory information|"
        r"lu[\s-]*bio[\s-]*04",
        re.I,
    )

    seen = set()
    regulatory_number = 1

    for text_node in soup.find_all(
        string=regulatory_phrase
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

            if 20 <= len(candidate) <= 2500:
                if (
                    "organic inspection body code"
                    in candidate_lower
                    or re.search(
                        r"lu[\s-]*bio[\s-]*04",
                        candidate_lower,
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
                + str(regulatory_number),
                chosen,
            )

            regulatory_number += 1

    raw_normalized = normalize(
        raw_html
    )

    fallback_patterns = [
        re.compile(
            r".{0,500}"
            r"organic inspection body code"
            r".{0,900}",
            re.I | re.S,
        ),
        re.compile(
            r".{0,500}"
            r"lu[\s-]*bio[\s-]*04"
            r".{0,900}",
            re.I | re.S,
        ),
    ]

    for pattern in fallback_patterns:
        match = pattern.search(
            raw_normalized
        )

        if not match:
            continue

        fragment = match.group(0)

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
                + str(regulatory_number),
                text,
            )

            regulatory_number += 1


def make_term_pattern(term):
    term = normalize(term)

    if not term:
        return None

    if term.lower() == "lu-bio-04":
        return re.compile(
            r"lu[\s-]*bio[\s-]*04",
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


def find_matches(
    sections,
    terms,
):
    results = []
    seen = set()

    for section, text in sections.items():
        for term in terms:
            pattern = make_term_pattern(
                term
            )

            if pattern is None:
                continue

            found = list(
                pattern.finditer(text)
            )

            if not found:
                continue

            key = (
                section,
                normalize(term).lower(),
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
                    "term": normalize(term),
                    "section": section,
                    "snippet": text[left:right],
                    "full_text": text,
                    "occurrences": len(found),
                }
            )

    return results


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
        timeout=70,
    )


@app.post("/api/scan")
def scan(req: ScanRequest):
    marketplace = (
        req.marketplace.upper()
    )

    if marketplace not in MARKETPLACES:
        raise HTTPException(
            status_code=400,
            detail="Unsupported marketplace",
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
            detail="No valid ASINs supplied",
        )

    results = []

    for asin in asins[:50]:
        url = MARKETPLACES[
            marketplace
        ].format(asin)

        item = {
           