
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List
import requests
import os
from bs4 import BeautifulSoup
import re
import time

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
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14; Mobile) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Mobile Safari/537.36"
    ),
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

def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

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

    desc = soup.select_one("#productDescription")
    if desc:
        sections["Description"] = clean_text(desc.get_text(" ", strip=True))

    # Product details / technical details / regulatory information often appear in tables or divs.
    detail_candidates = soup.select(
        "#detailBullets_feature_div, #productDetails_feature_div, "
        "#productDetails_techSpec_section_1, #productDetails_detailBullets_sections1, "
        "#productOverview_feature_div"
    )
    if detail_candidates:
        details = " | ".join(clean_text(x.get_text(" ", strip=True)) for x in detail_candidates)
        if details:
            sections["Product details"] = details

    # A+ modules are public HTML when present.
    aplus = soup.select_one("#aplus, #aplus_feature_div")
    if aplus:
        txt = clean_text(aplus.get_text(" ", strip=True))
        if txt:
            sections["A+ content"] = txt

    # Catch other visible page text, useful for regulatory info not covered above.
    body = soup.body
    if body:
        full = clean_text(body.get_text(" ", strip=True))
        if full:
            sections["Visible page text"] = full

    return sections

def find_matches(sections, terms):
    matches = []
    for section, text in sections.items():
        low = text.lower()
        for term in terms:
            t = term.lower().strip()
            if not t:
                continue
            start = 0
            while True:
                idx = low.find(t, start)
                if idx == -1:
                    break
                left = max(0, idx - 100)
                right = min(len(text), idx + len(t) + 140)
                snippet = text[left:right]
                matches.append({
                    "term": term,
                    "section": section,
                    "snippet": snippet
                })
                start = idx + len(t)
                # avoid floods from the full-page text
                if section == "Visible page text":
                    break
    # De-duplicate substantially identical matches.
    out, seen = [], set()
    for m in matches:
        key = (m["term"].lower(), m["section"], m["snippet"].lower())
        if key not in seen:
            seen.add(key)
            out.append(m)
    return out[:30]

@app.post("/api/scan")
def scan(req: ScanRequest):
    mp = req.marketplace.upper()
    if mp not in MARKETPLACES:
        raise HTTPException(400, "Unsupported marketplace")

    cleaned = []
    for raw in req.asins:
        asin = re.sub(r"[^A-Z0-9]", "", raw.upper())
        if len(asin) == 10:
            cleaned.append(asin)
    cleaned = list(dict.fromkeys(cleaned))[:50]

    if not cleaned:
        raise HTTPException(400, "No valid 10-character ASINs supplied")

    results = []

    session = requests.Session()
    session.headers.update(HEADERS)

    for asin in cleaned:
        url = MARKETPLACES[mp].format(asin)
        item = {
            "asin": asin,
            "url": url,
            "status": "unknown",
            "http_status": None,
            "title": "",
            "matches": [],
            "warning": None,
        }
        try:
            if SCRAPERAPI_KEY:
                scraper_params = {
                    "api_key": SCRAPERAPI_KEY,
                    "url": url,
                    "country_code": "uk" if mp == "UK" else mp.lower(),
                    "render": "true",
                }
                r = session.get(
                    SCRAPERAPI_ENDPOINT,
                    params=scraper_params,
                    timeout=70,
                    allow_redirects=True,
                )
                item["fetch_method"] = "ScraperAPI"
            else:
                r = session.get(url, timeout=18, allow_redirects=True)
                item["fetch_method"] = "Direct"

            item["http_status"] = r.status_code

            if r.status_code != 200:
                item["status"] = "fetch_failed"
                item["warning"] = f"Amazon returned HTTP {r.status_code}."
                results.append(item)
                continue

            html_low = r.text.lower()
            if "captcha" in html_low or "enter the characters you see below" in html_low:
                item["status"] = "blocked"
                if SCRAPERAPI_KEY:
                    item["warning"] = "Amazon still returned a CAPTCHA through ScraperAPI. Retry or enable stronger proxy settings."
                else:
                    item["warning"] = "Amazon served a CAPTCHA/robot check. Add SCRAPERAPI_KEY in Render to use ScraperAPI."
                results.append(item)
                continue

            soup = BeautifulSoup(r.text, "html.parser")
            sections = extract_sections(soup)
            item["title"] = sections.get("Title", "")
            item["matches"] = find_matches(sections, req.terms)
            item["status"] = "matched" if item["matches"] else "clear"

            if not item["title"] and not sections.get("Product details"):
                item["warning"] = (
                    "Page loaded but Amazon returned limited HTML. "
                    "A proxy/API provider may be needed for reliable scanning."
                )
            results.append(item)
        except requests.RequestException as e:
            item["status"] = "fetch_failed"
            item["warning"] = str(e)
            results.append(item)

        time.sleep(0.7)

    return {
        "marketplace": mp,
        "terms": req.terms,
        "count": len(results),
        "source": "ScraperAPI" if SCRAPERAPI_KEY else "Direct",
        "results": results,
    }

@app.get("/")
def home():
    return FileResponse("index.html")

@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse("manifest.webmanifest", media_type="application/manifest+json")

@app.get("/sw.js")
def sw():
    return FileResponse("sw.js", media_type="application/javascript")
