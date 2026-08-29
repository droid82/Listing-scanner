from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List
from uuid import uuid4
import html as html_lib
import os
import re
import threading
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

SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "").strip()
ASYNC_JOBS_URL = "https://async.scraperapi.com/jobs"

DEFAULT_TERMS = [
    "organic",
    "bio",
    "eco",
    "lu-bio-04",
]

BUILT_IN_TERMS = [
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

CONTROL_BODY_PATTERN = re.compile(
    r"\b[A-Z]{2,3}[\s\-–—−‐-]*BIO[\s\-–—−‐-]*\d{2,3}\b",
    re.I,
)

SCAN_STATE = {}
STATE_LOCK = threading.Lock()
STATE_TTL_SECONDS = 60 * 60 * 3


class StartRequest(BaseModel):
    asins: List[str]
    marketplace: str = "UK"
    terms: List[str] = DEFAULT_TERMS


class StatusRequest(BaseModel):
    token: str


def normalise(text):
    text = html_lib.unescape(
        str(text or "")
    )

    for dash in (
        "–",
        "—",
        "−",
        "‐",
        "-",
    ):
        text = text.replace(
            dash,
            "-",
        )

    return re.sub(
        r"\s+",
        " ",
        text,
    ).strip()


def clean_asin(raw):
    asin = re.sub(
        r"[^A-Z0-9]",
        "",
        str(raw).upper(),
    )

    if len(asin) == 10:
        return asin

    return ""


def add_section(
    sections,
    name,
    text,
):
    text = normalise(text)

    if (
        text
        and text not in sections.values()
    ):
        sections[name] = text


def extract_sections(raw_html):
    soup = BeautifulSoup(
        raw_html,
        "html.parser",
    )

    sections = {}

    title = soup.select_one(
        "#productTitle"
    )

    if title:
        add_section(
            sections,
            "Title",
            title.get_text(
                " ",
                strip=True,
            ),
        )

    bullets = soup.select(
        "#feature-bullets "
        "li span.a-list-item"
    )

    if bullets:
        add_section(
            sections,
            "Bullet points",
            " | ".join(
                x.get_text(
                    " ",
                    strip=True,
                )
                for x in bullets
            ),
        )

    selectors = [
        (
            "Product overview",
            "#productOverview_feature_div",
        ),
        (
            "Description",
            "#productDescription",
        ),
        (
            "Product details",
            "#detailBullets_feature_div",
        ),
        (
            "Product details 2",
            "#productDetails_feature_div",
        ),
        (
            "Product details 3",
            "#productDetails_techSpec_section_1",
        ),
        (
            "Product details 4",
            "#productDetails_detailBullets_sections1",
        ),
        (
            "Important information",
            "#important-information",
        ),
        (
            "Important information 2",
            "#importantInformation",
        ),
        (
            "Safety information",
            "#safety-information",
        ),
        (
            "Ingredients",
            "#ingredients",
        ),
        (
            "Directions",
            "#directions",
        ),
    ]

    for name, selector in selectors:
        node = soup.select_one(
            selector
        )

        if node:
            add_section(
                sections,
                name,
                node.get_text(
                    " ",
                    strip=True,
                ),
            )

    aplus = (
        soup.select_one(
            "#aplus_feature_div"
        )
        or soup.select_one(
            "#aplus"
        )
    )

    if aplus:
        modules = aplus.select(
            ".aplus-module, "
            ".premium-aplus-module"
        )

        seen_modules = set()
        module_number = 1

        for module in modules:
            text = normalise(
                module.get_text(
                    " ",
                    strip=True,
                )
            )

            if (
                len(text) < 20
                or text in seen_modules
            ):
                continue

            seen_modules.add(
                text
            )

            add_section(
                sections,
                "A+ module "
                + str(module_number),
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

    regulatory_marker = re.compile(
        r"organic inspection body code|"
        r"regulatory information|"
        r"safety and product resources|"
        r"\b[A-Z]{2,3}"
        r"[\s\-–—−‐-]*BIO"
        r"[\s\-–—−‐-]*"
        r"\d{2,3}\b",
        re.I,
    )

    regulatory_seen = set()
    regulatory_number = 1

    for text_node in soup.find_all(
        string=regulatory_marker
    ):
        node = text_node.parent
        chosen = ""

        for _ in range(8):
            if node is None:
                break

            candidate = normalise(
                node.get_text(
                    " ",
                    strip=True,
                )
            )

            if (
                20
                <= len(candidate)
                <= 3000
                and regulatory_marker.search(
                    candidate
                )
            ):
                chosen = candidate

                if (
                    "organic inspection body code"
                    in candidate.lower()
                    or CONTROL_BODY_PATTERN.search(
                        candidate
                    )
                ):
                    break

            node = node.parent

        if (
            chosen
            and chosen
            not in regulatory_seen
        ):
            regulatory_seen.add(
                chosen
            )

            add_section(
                sections