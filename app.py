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
    text = text or ""

    for dash in ["–", "—", "−", "‐", "-"]:
        text = text.replace(dash, "-")

    return re.sub(r"\s+", " ", text).strip()


def add_section(sections, name, text):
    text = normalize(text)

    if text and text not in sections.values():
        sections[name] = text


def extract_sections(soup):
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
        "#prodDetails",
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

    aplus = (
        soup.select_one("#aplus_feature_div")
        or soup.select_one("#aplus")
    )

    if aplus:
        modules = aplus.select(
            ".aplus-module, .premium-aplus-module"
        )

        if modules:
            seen = set()
            module_number = 1

            for module in modules:
                text = normalize(
                    module.get_text(
                        " ",
                        strip=True
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
                    text
                )

                module_number += 1

        add_section(
            sections,
            "A+ full content",
            aplus.get_text(" ", strip=True)
        )

    extract_regulatory(
        soup,
        sections
    )

    return sections


def extract_regulatory(soup, sections):
    code_pattern = re.compile(
        r"LU[\s\-–—−‐-]*BIO[\s\-–—−‐-]*04",
        re.I
    )

    text_nodes = soup.find_all(
        string=True
    )

    found = []
    seen = set()

    for text_node in text_nodes:
        text = normalize(
            str(text_node)
        )

        lower = text.lower()

        if (
            "organic inspection body code"
            not in lower
            and
            "regulatory information"
            not in lower
            and
            not code_pattern.search(text)
        ):
            continue

        node = text_node.parent

        best_text = ""

        for _ in range(6):
            if node is None:
                break

            candidate = normalize(
                node.get_text(
                    " ",
                    strip=True
                )
            )

            if 20 <= len(candidate) <= 1800:

                if (
                    "organic inspection body code"
                    in candidate.lower()
                    or
                    code_pattern.search(candidate)
                ):
                    best_text = candidate
                    break

            node = node.parent

        if (
            best_text
            and best_text not in seen
        ):
            seen.add(best_text)
            found.append(best_text)

    for number, text in enumerate(
        found,
        start=1
    ):
        add_section(
            sections,
            "Regulatory information " + str(number),
            text
        )


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
        re