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
    "IE": "https://www.amazon.ie/dp/{}",
}

DEFAULT_TERMS = [
    "organic",
    "organically",
    "certified organic",
    "organic certificate",
    "organic certification",
    "organic inspection body",
    "bio",
    "eco",
    "ecological",
    "lu-bio-04",
]

SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "").strip()
SCRAPERAPI_ENDPOINT = "https://api.scraperapi.com/"


class ScanRequest(BaseModel):
    asins: List[str]
    marketplace: str = "UK"
    terms: List[str] = DEFAULT_TERMS


def normalize(text):
    text = html.unescape(text or "")
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


def extract_sections(soup):
    sections = {}

    title = soup.select_one("#productTitle")
    if title:
        add_section(sections, "Title", title.get_text(" ", strip=True))

    bullets = soup.select("#feature-bullets li span.a-list-item")
    if bullets:
        add_section(
            sections,
            "Bullet points",
            " | ".join(x.get_text(" ", strip=True) for x in bullets),
        )

    overview = soup.select_one("#productOverview_feature_div")
    if overview:
        add_section(
            sections,
            "Product overview",
            overview.get_text(" ", strip=True),
        )

    description = soup.select_one("#productDescription")
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

    aplus = soup.select_one("#aplus_feature_div")
    if not aplus:
        aplus = soup.select_one("#aplus")

    if aplus:
        modules = aplus.select(".aplus-module")

        if not modules:
            modules = aplus.select(".premium-aplus-module")

        seen_modules = set()
        module_number = 1

        for module in modules:
            text = normalize(module.get_text(" ", strip=True))

            if len(text) < 20 or text in seen_modules:
                continue

            seen_modules.add(text)

            add_section(
                sections,
                "A+ module " + str(module_number),
                text,
            )

            module_number += 1

        add_section(
            sections,
            "A+ full content",
            aplus.get_text(" ", strip=True),
        )

    return sections


def extract_regulatory(soup, raw_html, sections):
    phrases = re.compile(
        r"organic inspection body code|regulatory information|"
        r"LU[\s\-–—−‐-]*BIO[\s\-–—−‐-]*04",
        re.I,
    )

    matches = soup.find_all(string=phrases)

    seen = set()
    number = 1

    for match in matches:
        node = match.parent

        for _ in range(7):
            if node is None:
                break

            text = normalize(node.get_text(" ", strip=True))

            if 20 <= len(text) <= 3000 and phrases.search(text):
                if text not in seen:
                    seen.add(text)
                    sections[
                        "Regulatory information " + str(number)
                    ] = text
                    number += 1

                break

            node = node.parent

    raw = raw_html.replace("–", "-")
    raw = raw.replace("—", "-")
    raw = raw.replace("−", "-")
    raw = raw.replace("‐", "-")
    raw = raw.replace("-", "-")

    raw_patterns = [
        re.compile(
            r".{0,350}organic inspection body code.{0,650}",
            re.I | re.S,
        ),
        re.compile(
            r".{0,350}LU[\s\-]*BIO[\s\-]*04.{0,650}",
            re.I | re.S,
        ),
    ]

    for pattern in raw_patterns:
        for match in pattern.finditer(raw):
            fragment = match.group(0)

            text = normalize(
                BeautifulSoup(
                    fragment,
                    "html.parser",
                ).get_text(" ", strip=True)
            )

            if not text or text in seen:
                continue

            seen.add(text)

            sections[
                "Regulatory raw data " + str(number)
            ] = text

            number += 1


def term_pattern(term):
    term = normalize(term)

    if not term:
        return None

    if term.lower() == "lu-bio-04":
        return re.compile(
            r"LU[\s\-]*BIO[\s\-]*04",
            re.I,
        )

    if re.fullmatch(r"[A-Za-z0-9]+", term):
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
        for term in terms:
            pattern = term_pattern(term)

            if pattern is None:
                continue

            found = list(pattern.finditer(text))

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

            results.append({
                "term": normalize(term),
                "section": section,
                "snippet": text[left:right],
                "full_text": text,
                "occurrences": len(found),
            })

    return results


def scraper_request(url, marketplace, render):
    if not SCRAPERAPI_KEY:
        return None

    country = (
        "uk"
        if marketplace == "UK"
        else marketplace.lower()
    )

    params = {
        "api_key": SCRAPERAPI_KEY,
        "url": url,
        "country_code": country,
        "device_type": "mobile",
        "premium": "true",
    }

    if render:
        params["render"] = "true"

    timeout = 38 if render else 22

    return requests.get(
        SCRAPERAPI_ENDPOINT,
        params=params,
        timeout=timeout,
    )


def parse_response(response):
    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    sections = extract_sections(soup)

    extract_regulatory(
        soup,
        response.text,
        sections,
    )

    return soup, sections


@app.post("/api/scan")
def scan(req: ScanRequest):
    marketplace = req.marketplace.upper()

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
        print(
            "SCAN START " + asin,
            flush=True,
        )

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
            "fetch_stage": None,
        }

        try:
            print(
                "STATIC FETCH " + asin,
                flush=True,
            )

            response = scraper_request(
                url,
                marketplace,
                render=False,
            )

            if response is None:
                item["status"] = "fetch_failed"
                item["warning"] = (
                    "SCRAPERAPI_KEY is missing."
                )

                results.append(item)
                continue

            print(
                "STATIC STATUS "
                + asin
                + " "
                + str(response.status_code),
                flush=True,
            )

            if response.status_code != 200:
                item["status"] = "fetch_failed"

                item["warning"] = (
                    "ScraperAPI returned HTTP "
                    + str(response.status_code)
                )

                results.append(item)
                continue

            soup, sections = parse_response(
                response
            )

            item["fetch_stage"] = "static"

            item["title"] = sections.get(
                "Title",
                "",
            )

            matches = find_matches(
                sections,
                req.terms,
            )

            regulatory_found = any(
                name.startswith("Regulatory")
                for name in sections
            )

            aplus_found = any(
                name.startswith("A+")
                for name in sections
            )

            if matches:
                item["matches"] = matches
                item["status"] = "matched"
                item["sections_found"] = list(
                    sections.keys()
                )

                results.append(item)

                print(
                    "STATIC MATCH " + asin,
                    flush=True,
                )

                continue

            needs_render = (
                not regulatory_found
                or not aplus_found
            )

            if needs_render:
                print(
                    "RENDER FETCH " + asin,
                    flush=True,
                )

                try:
                    rendered = scraper_request(
                        url,
                        marketplace,
                        render=True,
                    )

                except requests.Timeout:
                    rendered = None

                if rendered is not None:
                    print(
                        "RENDER STATUS "
                        + asin
                        + " "
                        + str(rendered.status_code),
                        flush=True,
                    )

                if (
                    rendered is not None
                    and rendered.status_code == 200
                ):
                    soup, rendered_sections = (
                        parse_response(rendered)
                    )

                    for name, text in (
                        rendered_sections.items()
                    ):
                        if name not in sections:
                            sections[name] = text

                    item["fetch_stage"] = "rendered"

                    item["title"] = sections.get(
                        "Title",
                        item["title"],
                    )

                    matches = find_matches(
                        sections,
                        req.terms,
                    )

            item["matches"] = matches

            item["sections_found"] = list(
                sections.keys()
            )

            if matches:
                item["status"] = "matched"

            elif any(
                name.startswith("Regulatory")
                for name in sections
            ):
                item["status"] = "clear"

            else:
                item["status"] = "incomplete"

                item["warning"] = (
                    "Amazon product data was retrieved, "
                    "but the regulatory section was not "
                    "verified. This ASIN is not confirmed clear."
                )

            results.append(item)

            print(
                "SCAN END "
                + asin
                + " "
                + item["status"],
                flush=True,
            )

        except requests.Timeout:
            item["status"] = "incomplete"

            item["warning"] = (
                "ScraperAPI timed out before the "
                "regulatory data could be verified. "
                "This ASIN is not confirmed clear."
            )

            results.append(item)

            print(
                "SCAN TIMEOUT " + asin,
                flush=True,
            )

        except requests.RequestException as error:
            item["status"] = "fetch_failed"
            item["warning"] = str(error)

            results.append(item)

            print(
                "SCAN ERROR "
                + asin
                + " "
                + str(error),
                flush=True,
            )

        time.sleep(0.3)

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