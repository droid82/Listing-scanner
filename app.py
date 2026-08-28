from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List
import os
import re
import requests

app = FastAPI(title="Amazon Live Listing Scanner")

MARKETPLACES = {
    "UK": {
        "country_code": "gb",
        "tld": "co.uk",
        "url": "https://www.amazon.co.uk/dp/{}",
    },
    "DE": {
        "country_code": "de",
        "tld": "de",
        "url": "https://www.amazon.de/dp/{}",
    },
    "FR": {
        "country_code": "fr",
        "tld": "fr",
        "url": "https://www.amazon.fr/dp/{}",
    },
    "IT": {
        "country_code": "it",
        "tld": "it",
        "url": "https://www.amazon.it/dp/{}",
    },
    "ES": {
        "country_code": "es",
        "tld": "es",
        "url": "https://www.amazon.es/dp/{}",
    },
    "NL": {
        "country_code": "nl",
        "tld": "nl",
        "url": "https://www.amazon.nl/dp/{}",
    },
    "IE": {
        "country_code": "ie",
        "tld": "ie",
        "url": "https://www.amazon.ie/dp/{}",
    },
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

AMAZON_PRODUCT_ENDPOINT = (
    "https://api.scraperapi.com/structured/amazon/product"
)


class ScanRequest(BaseModel):
    asins: List[str]
    marketplace: str = "UK"
    terms: List[str] = DEFAULT_TERMS


def normalize(text):
    text = str(text or "")

    for dash in ["–", "—", "−", "‐", "-"]:
        text = text.replace(dash, "-")

    return re.sub(r"\s+", " ", text).strip()


def make_pattern(term):
    term = normalize(term)

    if not term:
        return None

    if term.lower() == "lu-bio-04":
        return re.compile(
            r"LU[\s-]*BIO[\s-]*04",
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


def flatten_json(value, path="product"):
    rows = []

    if isinstance(value, dict):

        for key, child in value.items():

            child_path = (
                path
                + "."
                + str(key)
            )

            rows.extend(
                flatten_json(
                    child,
                    child_path,
                )
            )

    elif isinstance(value, list):

        for index, child in enumerate(value):

            child_path = (
                path
                + "["
                + str(index)
                + "]"
            )

            rows.extend(
                flatten_json(
                    child,
                    child_path,
                )
            )

    elif value is not None:

        text = normalize(value)

        if text:
            rows.append(
                (
                    path,
                    text,
                )
            )

    return rows


def find_matches(data, terms):
    matches = []
    seen = set()

    for path, text in flatten_json(data):

        for term in terms:

            pattern = make_pattern(
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
                path,
                normalize(term).lower(),
                text.lower(),
            )

            if key in seen:
                continue

            seen.add(key)

            first = found[0]

            left = max(
                0,
                first.start() - 160,
            )

            right = min(
                len(text),
                first.end() + 260,
            )

            matches.append(
                {
                    "term": normalize(term),
                    "section": path,
                    "snippet": text[left:right],
                    "full_text": text,
                    "occurrences": len(found),
                }
            )

    return matches


def has_regulatory_data(data):
    regulatory_terms = (
        "regulatory",
        "safety",
        "inspection body",
        "organic inspection",
        "lu-bio",
    )

    for path, text in flatten_json(data):

        combined = (
            path
            + " "
            + text
        ).lower()

        if any(
            term in combined
            for term in regulatory_terms
        ):
            return True

    return False


def get_title(data):

    for key in (
        "name",
        "title",
        "product_title",
    ):

        value = data.get(key)

        if value:
            return normalize(value)

    return ""


def fetch_product(
    asin,
    marketplace,
):
    market = MARKETPLACES[
        marketplace
    ]

    return requests.get(
        AMAZON_PRODUCT_ENDPOINT,
        params={
            "api_key": SCRAPERAPI_KEY,
            "asin": asin,
            "country_code": (
                market["country_code"]
            ),
            "tld": market["tld"],
        },
        timeout=45,
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

    if not SCRAPERAPI_KEY:
        raise HTTPException(
            status_code=500,
            detail=(
                "SCRAPERAPI_KEY "
                "is missing in Render"
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
            detail="No valid ASINs supplied",
        )

    results = []

    for asin in asins[:50]:

        item = {
            "asin": asin,
            "url": (
                MARKETPLACES[
                    marketplace
                ]["url"].format(asin)
            ),
            "status": "unknown",
            "title": "",
            "matches": [],
            "warning": None,
        }

        try:

            response = fetch_product(
                asin,
                marketplace,
            )

            if response.status_code != 200:

                item["status"] = (
                    "fetch_failed"
                )

                item["warning"] = (
                    "ScraperAPI Amazon "
                    "Product endpoint "
                    "returned HTTP "
                    + str(
                        response.status_code
                    )
                )

                results.append(item)

                continue

            try:
                data = response.json()

            except ValueError:

                item["status"] = (
                    "fetch_failed"
                )

                item["warning"] = (
                    "ScraperAPI did not "
                    "return valid JSON."
                )

                results.append(item)

                continue

            if not isinstance(
                data,
                dict,
            ):

                item["status"] = (
                    "fetch_failed"
                )

                item["warning"] = (
                    "Unexpected "
                    "ScraperAPI response."
                )

                results.append(item)

                continue

            item["title"] = (
                get_title(data)
            )

            item["matches"] = (
                find_matches(
                    data,
                    req.terms,
                )
            )

            if item["matches"]:

                item["status"] = (
                    "matched"
                )

            elif has_regulatory_data(
                data
            ):

                item["status"] = (
                    "clear"
                )

            else:

                item["status"] = (
                    "incomplete"
                )

                item["warning"] = (
                    "Amazon product data "
                    "was retrieved, but "
                    "ScraperAPI did not "
                    "expose a regulatory/"
                    "safety section. "
                    "This ASIN is not "
                    "confirmed clear."
                )

            results.append(item)

        except requests.Timeout:

            item["status"] = (
                "fetch_failed"
            )

            item["warning"] = (
                "ScraperAPI timed out."
            )

            results.append(item)

        except requests.RequestException as error:

            item["status"] = (
                "fetch_failed"
            )

            item["warning"] = str(
                error
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