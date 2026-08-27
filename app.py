from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List
import os
import re
import time
import requests
from bs4 import BeautifulSoup

app = FastAPI(title="Amazon Live Listing Scanner")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Cache-Control": "no-cache",
}

SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "").strip()
SCRAPERAPI_ENDPOINT = "https://api.scraperapi.com/"


class ScanRequest(BaseModel):
    asins: List[str]
    marketplace: str = "UK"
    terms: List[str] = DEFAULT_TERMS


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def extract_sections(soup: BeautifulSoup):
    sections = {}

    title = soup.select_one("#productTitle")
    if title:
        sections["Title"] = clean_text(title.get_text(" ", strip=True))

    bullets = soup.select("#feature-bullets li span.a-list-item")
    bullet_text = [clean_text(x.get_text(" ", strip=True)) for x in bullets]
    bullet_text = [x for x in bullet_text if x]

    if bullet_text:
        sections["Bullet points"] = " | ".join(bullet_text)

    description = soup.select_one("#productDescription")
    if description:
        sections["Description"] = clean_text(
            description.get_text(" ", strip=True)
        )

    detail_candidates = soup.select(
        "#detailBullets_feature_div, "
        "#productDetails_feature_div, "
        "#productDetails_techSpec_section_1, "
        "#productDetails_detailBullets_sections1, "
        "#productOverview_feature_div"
    )

    if detail_candidates:
        details = " | ".join(
            clean_text(x.get_text(" ", strip=True))
            for x in detail_candidates
        )

        if details:
            sections["Product details"] = details

    aplus = soup.select_one("#aplus, #aplus_feature_div")

    if aplus:
        aplus_text = clean_text(aplus.get_text(" ", strip=True))

        if aplus_text:
            sections["A+ content"] = aplus_text

    return sections


def build_term_regex(term: str):
    term = term.strip()

    if not term:
        return None

    if re.fullmatch(r"[A-Za-z0-9]+", term):
        return re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])",
            re.I,
        )

    return re.compile(re.escape(term), re.I)


def find_matches(sections, terms):
    matches = []

    for section, text in sections.items():
        for term in terms:
            regex = build_term_regex(term)

            if regex is None:
                continue

            match = regex.search(text)

            if not match:
                continue

            left = max(0, match.start() - 120)
            right = min(len(text), match.end() + 180)

            matches.append(
                {
                    "term": term.strip(),
                    "section": section,
                    "snippet": text[left:right],
                    "full_text": text,
                }
            )

    deduplicated = []
    seen = set()

    for match in matches:
        key = (
            match["term"].lower(),
            match["section"],
            match["full_text"].lower(),
        )

        if key not in seen:
            seen.add(key)
            deduplicated.append(match)

    return deduplicated[:30]


def fetch_amazon_page(session, url, marketplace):
    if SCRAPERAPI_KEY:
        country_code = "uk" if marketplace == "UK" else marketplace.lower()

        params = {
            "api_key": SCRAPERAPI_KEY,
            "url": url,
            "country_code": country_code,
            "render": "true",
        }

        response = session.get(
            SCRAPERAPI_ENDPOINT,
            params=params,
            timeout=70,
            allow_redirects=True,
        )

        return response, "ScraperAPI"

    response = session.get(
        url,
        timeout=18,
        allow_redirects=True,
    )

    return response, "Direct"


@app.post("/api/scan")
def scan(req: ScanRequest):
    marketplace = req.marketplace.upper()

    if marketplace not in MARKETPLACES:
        raise HTTPException(
            status_code=400,
            detail="Unsupported marketplace",
        )

    cleaned_asins = []

    for raw_asin in req.asins:
        asin = re.sub(
            r"[^A-Z0-9]",
            "",
            raw_asin.upper(),
        )

        if len(asin) == 10:
            cleaned_asins.append(asin