from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List
import os
import re
import time
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

DEFAULT_TERMS = ["organic", "bio", "eco", "lu-bio-04"]
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "").strip()
SCRAPERAPI_ENDPOINT = "https://api.scraperapi.com/"

class ScanRequest(BaseModel):
    asins: List[str]
    marketplace: str = "UK"
    terms: List[str] = DEFAULT_TERMS

def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()

def extract_sections(soup):
    sections = {}

    title = soup.select_one("#productTitle")
    if title:
        sections["Title"] = clean(title.get_text(" ", strip=True))

    bullets = soup.select("#feature-bullets li span.a-list-item")
    bullet_text = [clean(x.get_text(" ", strip=True)) for x in bullets]
    bullet_text = [x for x in bullet_text if x]
    if bullet_text:
        sections["Bullet points"] = " | ".join(bullet_text)

    description = soup.select_one("#productDescription")
    if description:
        sections["Description"] = clean(description.get_text(" ", strip=True))

    details = soup.select(
        "#detailBullets_feature_div, "
        "#productDetails_feature_div, "
        "#productDetails_techSpec_section_1, "
        "#productDetails_detailBullets_sections1, "
        "#productOverview_feature_div"
    )

    if details:
        text = " | ".join(
            clean(x.get_text(" ", strip=True))
            for x in details
        )
        if text:
            sections["Product details"] = text

    aplus = soup.select_one("#aplus, #aplus_feature_div")

    if aplus:
        text = clean(aplus.get_text(" ", strip=True))
        if text:
            sections["A+ content"] = text

    return sections

def term_pattern(term):
    term = term.strip()

    if not term:
        return None

    if re.fullmatch(r"[A-Za-z0-9]+", term):
        return re.compile(
            r"(?<![A-Za-z0-9])"
            + re.escape(term)
            + r"(?![A-Za-z0-9])",
            re.I
        )

    return re.compile(re.escape(term), re.I)

def find_matches(sections, terms):
    results = []
    seen = set()

    for section, text in sections.items():
        for term in terms:

            pattern = term_pattern(term)

            if pattern is None:
                continue

            match = pattern.search(text)

            if not match:
                continue

            key = (section, term.lower())

            if key in seen:
                continue

            seen.add(key)

            left = max(0, match.start() - 120)
            right = min(len(text), match.end() + 180)

            results.append({
                "term": term,
                "section": section,
                "snippet": text[left:right],
                "full_text": text
            })

    return results

def fetch_page(url, marketplace):

    if not SCRAPERAPI_KEY:
        return None, "missing_key"

    country = (
        "uk"
        if marketplace == "UK"
        else marketplace.lower()
    )

    response = requests.get(
        SCRAPERAPI_ENDPOINT,
        params={
            "api_key": SCRAPERAPI_KEY,
            "url": url,
            "country_code": country,
            "render": "true"
        },
        timeout=70
    )

    return response, "ScraperAPI"

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

        if len(asin) == 10 and asin not in asins:
            asins.append(asin)

    if not asins:
        raise HTTPException(
            status_code=400,
            detail="No valid ASINs supplied"
        )

    results = []

    for asin in asins[:50]:

        url = MARKETPLACES[marketplace].format(asin)

        item = {
            "asin": asin,
            "url": url,
            "status": "unknown",
            "title": "",
            "matches": [],
            "warning": None
        }

        try:

            response, method = fetch_page(
                url,
                marketplace
            )

            item["fetch_method"] = method

            if response is None:

                item["status"] = "fetch_failed"
                item["warning"] = (
                    "SCRAPERAPI_KEY is missing in Render."
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

            if "enter the characters you see below" in lower:

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

            sections = extract_sections(soup)

            item["title"] = sections.get(
                "Title",
                ""
            )

            item["matches"] = find_matches(
                sections,
                req.terms
            )

            item["status"] = (
                "matched"
                if item["matches"]
                else "clear"
            )

            if not sections:
                item["warning"] = (
                    "Amazon returned limited product content."
                )

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