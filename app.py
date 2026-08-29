from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List
import os
import re
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

SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "").strip()
ASYNC_JOBS_URL = "https://async.scraperapi.com/jobs"

DEFAULT_TERMS = ["organic", "bio", "eco", "lu-bio-04"]

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


class StartRequest(BaseModel):
    asins: List[str]
    marketplace: str = "UK"
    terms: List[str] = DEFAULT_TERMS


class StatusRequest(BaseModel):
    job_id: str
    asin: str
    marketplace: str = "UK"
    terms: List[str] = DEFAULT_TERMS


def normalise(text):
    text = html_lib.unescape(str(text or ""))

    for dash in ("–", "—", "−", "‐", "-"):
        text = text.replace(dash, "-")

    return re.sub(r"\s+", " ", text).strip()


def clean_asin(raw):
    asin = re.sub(r"[^A-Z0-9]", "", str(raw).upper())
    return asin if len(asin) == 10 else ""


def add_section(sections, name, text):
    text = normalise(text)

    if text and text not in sections.values():
        sections[name] = text


def extract_sections(raw_html):
    soup = BeautifulSoup(raw_html, "html.parser")
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

    selectors = [
        ("Product overview", "#productOverview_feature_div"),
        ("Description", "#productDescription"),
        ("Product details", "#detailBullets_feature_div"),
        ("Product details 2", "#productDetails_feature_div"),
        ("Product details 3", "#productDetails_techSpec_section_1"),
        ("Product details 4", "#productDetails_detailBullets_sections1"),
        ("Important information", "#important-information"),
        ("Important information 2", "#importantInformation"),
        ("Safety information", "#safety-information"),
        ("Ingredients", "#ingredients"),
        ("Directions", "#directions"),
    ]

    for name, selector in selectors:
        node = soup.select_one(selector)

        if node:
            add_section(
                sections,
                name,
                node.get_text(" ", strip=True),
            )

    aplus = (
        soup.select_one("#aplus_feature_div")
        or soup.select_one("#aplus")
    )

    if aplus:
        modules = aplus.select(
            ".aplus-module, .premium-aplus-module"
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
            aplus.get_text(
                " ",
                strip=True,
            ),
        )

    regulatory_marker = re.compile(
        r"organic inspection body code|"
        r"regulatory information|"
        r"safety and product resources|"
        r"\b[A-Z]{2,3}[\s\-–—−‐-]*BIO"
        r"[\s\-–—−‐-]*\d{2,3}\b",
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
                20 <= len(candidate) <= 3000
                and regulatory_marker.search(candidate)
            ):
                chosen = candidate

                if (
                    "organic inspection body code"
                    in candidate.lower()
                    or CONTROL_BODY_PATTERN.search(candidate)
                ):
                    break

            node = node.parent

        if (
            chosen
            and chosen not in regulatory_seen
        ):
            regulatory_seen.add(chosen)

            add_section(
                sections,
                "Regulatory information "
                + str(regulatory_number),
                chosen,
            )

            regulatory_number += 1

    raw_normalised = normalise(raw_html)

    raw_pattern = re.compile(
        r".{0,500}"
        r"(?:organic inspection body code|"
        r"\b[A-Z]{2,3}[\s-]*BIO"
        r"[\s-]*\d{2,3}\b)"
        r".{0,900}",
        re.I | re.S,
    )

    for raw_match in raw_pattern.finditer(
        raw_normalised
    ):
        text = normalise(
            BeautifulSoup(
                raw_match.group(0),
                "html.parser",
            ).get_text(
                " ",
                strip=True,
            )
        )

        if (
            text
            and text not in regulatory_seen
        ):
            regulatory_seen.add(text)

            add_section(
                sections,
                "Regulatory source data "
                + str(regulatory_number),
                text,
            )

            regulatory_number += 1

    return sections


def make_pattern(term):
    term = normalise(term)
    lower = term.lower()

    if not term:
        return None

    if lower == "organic":
        return re.compile(
            r"\borganic(?:ally)?\b",
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

    if lower == "lu-bio-04":
        return re.compile(
            r"LU[\s-]*BIO[\s-]*04",
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


def build_search_terms(user_terms):
    output = []
    seen = set()

    for term in (
        list(user_terms)
        + BUILT_IN_TERMS
    ):
        term = normalise(term)
        key = term.lower()

        if (
            term
            and key not in seen
        ):
            seen.add(key)
            output.append(term)

    return output


def find_matches(
    sections,
    terms,
):
    matches = []
    seen = set()
    patterns = []

    for term in terms:
        pattern = make_pattern(term)

        if pattern is not None:
            patterns.append(
                (
                    term,
                    pattern,
                )
            )

    patterns.append(
        (
            "organic control-body code",
            CONTROL_BODY_PATTERN,
        )
    )

    for section, text in sections.items():
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
                first.end() + 320,
            )

            matches.append(
                {
                    "term": label,
                    "section": section,
                    "snippet": text[left:right],
                    "full_text": text,
                    "occurrences": len(found),
                }
            )

    return matches


def result_from_html(
    asin,
    marketplace,
    raw_html,
    status_code,
    terms,
):
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
        "http_status": status_code,
        "sections_found": [],
    }

    if status_code != 200:
        item["status"] = (
            "fetch_failed"
        )

        item["error_code"] = (
            "E220 AMAZON_HTTP_"
            + str(status_code)
        )

        item["warning"] = (
            item["error_code"]
            + ": ScraperAPI finished the job, "
            "but Amazon returned HTTP "
            + str(status_code)
            + "."
        )

        return item

    lower = raw_html.lower()

    if (
        "enter the characters you see below"
        in lower
        or
        "sorry, we just need to make sure you're not a robot"
        in lower
    ):
        item["status"] = "blocked"

        item["error_code"] = (
            "E230 AMAZON_CAPTCHA"
        )

        item["warning"] = (
            "E230 AMAZON_CAPTCHA: "
            "Amazon returned a robot/CAPTCHA page."
        )

        return item

    sections = extract_sections(
        raw_html
    )

    item["sections_found"] = (
        list(
            sections.keys()
        )
    )

    item["title"] = sections.get(
        "Title",
        "",
    )

    item["matches"] = find_matches(
        sections,
        build_search_terms(terms),
    )

    has_aplus = any(
        name.startswith("A+")
        for name in sections
    )

    has_regulatory = any(
        name.startswith("Regulatory")
        for name in sections
    )

    if item["matches"]:
        item["status"] = "matched"

    elif not item["title"]:
        item["status"] = (
            "incomplete"
        )

        item["error_code"] = (
            "E206 AMAZON_DATA_EMPTY"
        )

        item["warning"] = (
            "E206 AMAZON_DATA_EMPTY: "
            "the async job finished, "
            "but the product title could not be extracted."
        )

    elif (
        not has_aplus
        and not has_regulatory
    ):
        item["status"] = (
            "incomplete"
        )

        item["error_code"] = (
            "E207 TARGET_SECTIONS_NOT_RETURNED"
        )

        item["warning"] = (
            "E207 TARGET_SECTIONS_NOT_RETURNED: "
            "the product loaded, but neither "
            "A+ nor Regulatory Information "
            "was present in the returned HTML. "
            "This ASIN is not confirmed clear."
        )

    elif not has_regulatory:
        item["status"] = (
            "incomplete"
        )

        item["error_code"] = (
            "E208 REGULATORY_DATA_NOT_RETURNED"
        )

        item["warning"] = (
            "E208 REGULATORY_DATA_NOT_RETURNED: "
            "A+ or other product data was found, "
            "but Regulatory Information was not present. "
            "This ASIN is not confirmed clear."
        )

    else:
        item["status"] = "clear"

    return item


@app.post("/api/scan-start")
def scan_start(req: StartRequest):
    marketplace = (
        req.marketplace.upper()
    )

    if marketplace not in MARKETPLACES:
        raise HTTPException(
            status_code=400,
            detail="E001 UNSUPPORTED_MARKETPLACE",
        )

    if not SCRAPERAPI_KEY:
        raise HTTPException(
            status_code=500,
            detail=(
                "E110 SCRAPERAPI_KEY_ERROR: "
                "SCRAPERAPI_KEY is missing in Render."
            ),
        )

    asins = []

    for raw in req.asins:
        asin = clean_asin(raw)

        if (
            asin
            and asin not in asins
        ):
            asins.append(asin)

    if not asins:
        raise HTTPException(
            status_code=400,
            detail="E002 NO_VALID_ASINS",
        )

    jobs = []

    for asin in asins[:20]:
        target_url = MARKETPLACES[
            marketplace
        ].format(asin)

        country = (
            "uk"
            if marketplace == "UK"
            else marketplace.lower()
        )

        payload = {
            "apiKey": SCRAPERAPI_KEY,
            "url": target_url,
            "apiParams": {
                "country_code": country,
                "device_type": "mobile",
                "render": True,
            },
            "expectUnsuccessReport": True,
            "timeoutSec": 180,
            "meta": {
                "asin": asin,
                "marketplace": marketplace,
            },
        }

        try:
            response = requests.post(
                ASYNC_JOBS_URL,
                json=payload,
                timeout=15,
            )

        except requests.Timeout:
            jobs.append(
                {
                    "asin": asin,
                    "status": "submit_failed",
                    "error_code": (
                        "E201 ASYNC_SUBMIT_TIMEOUT"
                    ),
                    "warning": (
                        "E201 ASYNC_SUBMIT_TIMEOUT: "
                        "ScraperAPI did not accept "
                        "the job within 15 seconds."
                    ),
                }
            )

            continue

        except requests.RequestException as error:
            jobs.append(
                {
                    "asin": asin,
                    "status": "submit_failed",
                    "error_code": (
                        "E202 ASYNC_SUBMIT_ERROR"
                    ),
                    "warning": (
                        "E202 ASYNC_SUBMIT_ERROR: "
                        + str(error)
                    ),
                }
            )

            continue

        if response.status_code not in (
            200,
            201,
            202,
        ):
            jobs.append(
                {
                    "asin": asin,
                    "status": "submit_failed",
                    "error_code": (
                        "E203 ASYNC_SUBMIT_HTTP_"
                        + str(
                            response.status_code
                        )
                    ),
                    "warning": (
                        "E203 ASYNC_SUBMIT_HTTP_"
                        + str(
                            response.status_code
                        )
                        + ": ScraperAPI rejected "
                        "the async job."
                    ),
                }
            )

            continue

        try:
            data = response.json()

        except ValueError:
            jobs.append(
                {
                    "asin": asin,
                    "status": "submit_failed",
                    "error_code": (
                        "E204 ASYNC_SUBMIT_INVALID_JSON"
                    ),
                    "warning": (
                        "E204 ASYNC_SUBMIT_INVALID_JSON: "
                        "ScraperAPI returned an invalid job response."
                    ),
                }
            )

            continue

        job_id = str(
            data.get("id")
            or ""
        ).strip()

        status_url = str(
            data.get("statusUrl")
            or ""
        ).strip()

        if not job_id:
            jobs.append(
                {
                    "asin": asin,
                    "status": "submit_failed",
                    "error_code": (
                        "E205 ASYNC_JOB_ID_MISSING"
                    ),
                    "warning": (
                        "E205 ASYNC_JOB_ID_MISSING: "
                        "ScraperAPI accepted the request "
                        "but returned no job ID."
                    ),
                }
            )

            continue

        jobs.append(
            {
                "asin": asin,
                "status": "submitted",
                "job_id": job_id,
                "status_url": status_url,
                "marketplace": marketplace,
                "terms": req.terms,
            }
        )

    return {
        "marketplace": marketplace,
        "count": len(jobs),
        "jobs": jobs,
    }


@app.post("/api/scan-status")
def scan_status(req: StatusRequest):
    marketplace = (
        req.marketplace.upper()
    )

    asin = clean_asin(
        req.asin
    )

    job_id = re.sub(
        r"[^A-Za-z0-9\-]",
        "",
        req.job_id,
    )

    if marketplace not in MARKETPLACES:
        raise HTTPException(
            status_code=400,
            detail="E001 UNSUPPORTED_MARKETPLACE",
        )

    if not asin:
        raise HTTPException(
            status_code=400,
            detail="E002 INVALID_ASIN",
        )

    if not job_id:
        raise HTTPException(
            status_code=400,
            detail="E209 INVALID_JOB_ID",
        )

    status_url = (
        ASYNC_JOBS_URL
        + "/"
        + job_id
    )

    try:
        response = requests.get(
            status_url,
            timeout=12,
        )

    except requests.Timeout:
        return {
            "asin": asin,
            "status": "poll_error",
            "error_code": (
                "E210 ASYNC_STATUS_TIMEOUT"
            ),
            "warning": (
                "E210 ASYNC_STATUS_TIMEOUT: "
                "the job status check timed out. "
                "The job may still be running."
            ),
        }

    except requests.RequestException as error:
        return {
            "asin": asin,
            "status": "poll_error",
            "error_code": (
                "E211 ASYNC_STATUS_ERROR"
            ),
            "warning": (
                "E211 ASYNC_STATUS_ERROR: "
                + str(error)
            ),
        }

    if response.status_code != 200:
        return {
            "asin": asin,
            "status": "poll_error",
            "error_code": (
                "E212 ASYNC_STATUS_HTTP_"
                + str(
                    response.status_code
                )
            ),
            "warning": (
                "E212 ASYNC_STATUS_HTTP_"
                + str(
                    response.status_code
                )
                + ": ScraperAPI status endpoint "
                "returned HTTP "
                + str(
                    response.status_code
                )
                + "."
            ),
        }

    try:
        data = response.json()

    except ValueError:
        return {
            "asin": asin,
            "status": "poll_error",
            "error_code": (
                "E213 ASYNC_STATUS_INVALID_JSON"
            ),
            "warning": (
                "E213 ASYNC_STATUS_INVALID_JSON: "
                "invalid status response from ScraperAPI."
            ),
        }

    job_status = str(
        data.get("status")
        or ""
    ).lower()

    if job_status in (
        "pending",
        "running",
        "processing",
        "queued",
        "created",
    ):
        return {
            "asin": asin,
            "status": "scanning",
            "job_status": job_status,
            "attempts": data.get(
                "attempts"
            ),
        }

    if job_status == "failed":
        reason = str(
            data.get("failReason")
            or "unknown_failure"
        )

        return {
            "asin": asin,
            "status": "fetch_failed",
            "error_code": (
                "E214 ASYNC_JOB_FAILED"
            ),
            "warning": (
                "E214 ASYNC_JOB_FAILED: "
                + reason
            ),
            "job_status": job_status,
            "attempts": data.get(
                "attempts"
            ),
        }

    if job_status != "finished":
        return {
            "asin": asin,
            "status": "poll_error",
            "error_code": (
                "E215 UNKNOWN_ASYNC_STATUS"
            ),
            "warning": (
                "E215 UNKNOWN_ASYNC_STATUS: "
                "ScraperAPI returned job status '"
                + job_status
                + "'."
            ),
        }

    scraper_response = (
        data.get("response")
        or {}
    )

    body = (
        scraper_response.get("body")
        or ""
    )

    amazon_status = (
        scraper_response.get(
            "statusCode"
        )
    )

    if amazon_status is None:
        amazon_status = 0

    if (
        not isinstance(
            body,
            str,
        )
        or not body.strip()
    ):
        return {
            "asin": asin,
            "status": "incomplete",
            "error_code": (
                "E216 ASYNC_BODY_EMPTY"
            ),
            "warning": (
                "E216 ASYNC_BODY_EMPTY: "
                "the job finished but no "
                "Amazon HTML body was returned."
            ),
            "job_status": job_status,
            "attempts": data.get(
                "attempts"
            ),
        }

    result = result_from_html(
        asin=asin,
        marketplace=marketplace,
        raw_html=body,
        status_code=int(
            amazon_status
        ),
        terms=req.terms,
    )

    result["job_status"] = (
        job_status
    )

    result["attempts"] = (
        data.get("attempts")
    )

    result["job_id"] = job_id

    return result


@app.get("/")
def home():
    return FileResponse(
        "index.html"
    )


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