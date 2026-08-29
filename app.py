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
    "UK": {"url": "https://www.amazon.co.uk/dp/{}", "country": "gb"},
    "DE": {"url": "https://www.amazon.de/dp/{}", "country": "de"},
    "FR": {"url": "https://www.amazon.fr/dp/{}", "country": "fr"},
    "IT": {"url": "https://www.amazon.it/dp/{}", "country": "it"},
    "ES": {"url": "https://www.amazon.es/dp/{}", "country": "es"},
    "NL": {"url": "https://www.amazon.nl/dp/{}", "country": "nl"},
    "IE": {"url": "https://www.amazon.ie/dp/{}", "country": "ie"},
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
    r"\b[A-Z]{2,3}[\s\-–—−‐‑]*BIO[\s\-–—−‐‑]*\d{2,3}\b",
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
    text = html_lib.unescape(str(text or ""))
    for dash in ("–", "—", "−", "‐", "‑"):
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
        add_section(sections, "Title", title.get_text(" ", strip=True))

    bullets = soup.select("#feature-bullets li span.a-list-item")
    if bullets:
        add_section(
            sections,
            "Bullet points",
            " | ".join(x.get_text(" ", strip=True) for x in bullets),
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
            add_section(sections, name, node.get_text(" ", strip=True))

    aplus = soup.select_one("#aplus_feature_div") or soup.select_one("#aplus")
    if aplus:
        modules = aplus.select(".aplus-module, .premium-aplus-module")
        seen_modules = set()
        module_number = 1

        for module in modules:
            text = normalise(module.get_text(" ", strip=True))
            if len(text) < 20 or text in seen_modules:
                continue
            seen_modules.add(text)
            add_section(sections, f"A+ module {module_number}", text)
            module_number += 1

        add_section(
            sections,
            "A+ full content",
            aplus.get_text(" ", strip=True),
        )

    regulatory_marker = re.compile(
        r"organic inspection body code|"
        r"regulatory information|"
        r"safety and product resources|"
        r"\b[A-Z]{2,3}[\s\-–—−‐‑]*BIO[\s\-–—−‐‑]*\d{2,3}\b",
        re.I,
    )

    regulatory_seen = set()
    regulatory_number = 1

    for text_node in soup.find_all(string=regulatory_marker):
        node = text_node.parent
        chosen = ""

        for _ in range(8):
            if node is None:
                break

            candidate = normalise(node.get_text(" ", strip=True))
            if (
                20 <= len(candidate) <= 3000
                and regulatory_marker.search(candidate)
            ):
                chosen = candidate
                if (
                    "organic inspection body code" in candidate.lower()
                    or CONTROL_BODY_PATTERN.search(candidate)
                ):
                    break

            node = node.parent

        if chosen and chosen not in regulatory_seen:
            regulatory_seen.add(chosen)
            add_section(
                sections,
                f"Regulatory information {regulatory_number}",
                chosen,
            )
            regulatory_number += 1

    raw_normalised = normalise(raw_html)
    raw_pattern = re.compile(
        r".{0,500}"
        r"(?:organic inspection body code|"
        r"\b[A-Z]{2,3}[\s-]*BIO[\s-]*\d{2,3}\b)"
        r".{0,900}",
        re.I | re.S,
    )

    for raw_match in raw_pattern.finditer(raw_normalised):
        text = normalise(
            BeautifulSoup(
                raw_match.group(0),
                "html.parser",
            ).get_text(" ", strip=True)
        )

        if text and text not in regulatory_seen:
            regulatory_seen.add(text)
            add_section(
                sections,
                f"Regulatory source data {regulatory_number}",
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
        return re.compile(r"\borganic(?:ally)?\b", re.I)

    if lower == "bio":
        return re.compile(
            r"(?<![A-Za-z0-9])bio(?![A-Za-z0-9])",
            re.I,
        )

    if lower == "eco":
        return re.compile(
            r"(?<![A-Za-z0-9])eco(?![A-Za-z0-9])",
            re.I,
        )

    if lower == "lu-bio-04":
        return re.compile(r"LU[\s-]*BIO[\s-]*04", re.I)

    if re.fullmatch(r"[A-Za-z0-9]+", term):
        return re.compile(
            r"(?<![A-Za-z0-9])"
            + re.escape(term)
            + r"(?![A-Za-z0-9])",
            re.I,
        )

    return re.compile(re.escape(term), re.I)


def build_search_terms(user_terms):
    output = []
    seen = set()

    for term in list(user_terms) + BUILT_IN_TERMS:
        term = normalise(term)
        key = term.lower()
        if term and key not in seen:
            seen.add(key)
            output.append(term)

    return output


def find_matches(sections, terms):
    matches = []
    seen = set()
    patterns = []

    for term in terms:
        pattern = make_pattern(term)
        if pattern is not None:
            patterns.append((term, pattern))

    patterns.append(("organic control-body code", CONTROL_BODY_PATTERN))

    for section, text in sections.items():
        for label, pattern in patterns:
            found = list(pattern.finditer(text))
            if not found:
                continue

            key = (section, label.lower())
            if key in seen:
                continue

            seen.add(key)
            first = found[0]
            left = max(0, first.start() - 180)
            right = min(len(text), first.end() + 320)

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


def result_from_payload(meta, payload):
    asin = meta["asin"]
    marketplace = meta["marketplace"]
    terms = meta["terms"]

    item = {
        "asin": asin,
        "url": MARKETPLACES[marketplace]["url"].format(asin),
        "status": "unknown",
        "title": "",
        "matches": [],
        "warning": None,
        "error_code": None,
        "http_status": None,
        "sections_found": [],
        "attempts": payload.get("attempts"),
    }

    job_status = str(payload.get("status") or "").lower()

    if job_status == "failed":
        reason = str(payload.get("failReason") or "unknown_failure")
        item["status"] = "fetch_failed"
        item["error_code"] = "E214 ASYNC_JOB_FAILED"
        item["warning"] = f"E214 ASYNC_JOB_FAILED: {reason}"
        return item

    response_data = payload.get("response") or {}
    body = response_data.get("body") or ""
    amazon_status = response_data.get("statusCode")

    try:
        amazon_status = int(amazon_status)
    except (TypeError, ValueError):
        amazon_status = 0

    item["http_status"] = amazon_status

    if not isinstance(body, str) or not body.strip():
        item["status"] = "incomplete"
        item["error_code"] = "E216 ASYNC_BODY_EMPTY"
        item["warning"] = (
            "E216 ASYNC_BODY_EMPTY: the async job completed "
            "but no Amazon HTML body was returned."
        )
        return item

    if amazon_status != 200:
        item["status"] = "fetch_failed"
        item["error_code"] = f"E220 AMAZON_HTTP_{amazon_status}"
        item["warning"] = (
            f"{item['error_code']}: ScraperAPI completed the job, "
            f"but Amazon returned HTTP {amazon_status}."
        )
        return item

    lower = body.lower()
    if (
        "enter the characters you see below" in lower
        or "sorry, we just need to make sure you're not a robot" in lower
    ):
        item["status"] = "blocked"
        item["error_code"] = "E230 AMAZON_CAPTCHA"
        item["warning"] = (
            "E230 AMAZON_CAPTCHA: Amazon returned a robot/CAPTCHA page."
        )
        return item

    sections = extract_sections(body)
    item["sections_found"] = list(sections.keys())
    item["title"] = sections.get("Title", "")
    item["matches"] = find_matches(
        sections,
        build_search_terms(terms),
    )

    has_aplus = any(name.startswith("A+") for name in sections)
    has_regulatory = any(name.startswith("Regulatory") for name in sections)

    if item["matches"]:
        item["status"] = "matched"
    elif not item["title"]:
        item["status"] = "incomplete"
        item["error_code"] = "E206 AMAZON_DATA_EMPTY"
        item["warning"] = (
            "E206 AMAZON_DATA_EMPTY: the job completed, "
            "but the product title could not be extracted."
        )
    elif not has_aplus and not has_regulatory:
        item["status"] = "incomplete"
        item["error_code"] = "E207 TARGET_SECTIONS_NOT_RETURNED"
        item["warning"] = (
            "E207 TARGET_SECTIONS_NOT_RETURNED: neither A+ nor Regulatory "
            "Information was present in the returned HTML. "
            "This ASIN is not confirmed clear."
        )
    elif not has_regulatory:
        item["status"] = "incomplete"
        item["error_code"] = "E208 REGULATORY_DATA_NOT_RETURNED"
        item["warning"] = (
            "E208 REGULATORY_DATA_NOT_RETURNED: A+ or other product data "
            "was found, but Regulatory Information was not present. "
            "This ASIN is not confirmed clear."
        )
    else:
        item["status"] = "clear"

    return item


def cleanup_old_state():
    cutoff = time.time() - STATE_TTL_SECONDS
    with STATE_LOCK:
        stale = [
            token
            for token, value in SCAN_STATE.items()
            if value.get("created_at", 0) < cutoff
        ]
        for token in stale:
            SCAN_STATE.pop(token, None)


def public_base_url(request: Request):
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    forwarded_host = request.headers.get("x-forwarded-host", "")
    host = forwarded_host or request.headers.get("host", "")

    if host:
        scheme = forwarded_proto or "https"
        return f"{scheme}://{host}".rstrip("/")

    return str(request.base_url).rstrip("/")


def finish_state(token, result):
    with STATE_LOCK:
        if token in SCAN_STATE:
            SCAN_STATE[token]["status"] = "finished"
            SCAN_STATE[token]["result"] = result


def process_remote_payload(token, payload):
    with STATE_LOCK:
        state = SCAN_STATE.get(token)
        state_copy = dict(state) if state else None

    if not state_copy:
        return None

    result = result_from_payload(state_copy, payload)
    finish_state(token, result)
    return result


@app.post("/api/scan-start")
def scan_start(req: StartRequest, request: Request):
    cleanup_old_state()

    marketplace = req.marketplace.upper()
    if marketplace not in MARKETPLACES:
        raise HTTPException(
            status_code=400,
            detail="E001 UNSUPPORTED_MARKETPLACE",
        )

    if not SCRAPERAPI_KEY:
        raise HTTPException(
            status_code=500,
            detail=(
                "E110 SCRAPERAPI_KEY_ERROR: SCRAPERAPI_KEY is missing in Render."
            ),
        )

    asins = []
    for raw in req.asins:
        asin = clean_asin(raw)
        if asin and asin not in asins:
            asins.append(asin)

    if not asins:
        raise HTTPException(
            status_code=400,
            detail="E002 NO_VALID_ASINS",
        )

    base_url = public_base_url(request)
    jobs = []

    for asin in asins[:20]:
        token = uuid4().hex
        target_url = MARKETPLACES[marketplace]["url"].format(asin)
        callback_url = f"{base_url}/api/scraper-callback/{token}"

        with STATE_LOCK:
            SCAN_STATE[token] = {
                "created_at": time.time(),
                "status": "submitting",
                "asin": asin,
                "marketplace": marketplace,
                "terms": list(req.terms),
                "result": None,
                "job_id": None,
                "status_url": None,
                "last_remote_check": 0.0,
            }

        payload = {
            "apiKey": SCRAPERAPI_KEY,
            "url": target_url,
            "callback": {
                "type": "webhook",
                "url": callback_url,
            },
            "apiParams": {
                "country_code": MARKETPLACES[marketplace]["country"],
                "device_type": "mobile",
                "render": "true",
            },
            "expectUnsuccessReport": True,
            "timeoutSec": 600,
            "meta": {
                "token": token,
                "asin": asin,
                "marketplace": marketplace,
            },
        }

        try:
            response = requests.post(
                ASYNC_JOBS_URL,
                json=payload,
                timeout=(4, 10),
            )
        except requests.Timeout:
            result = {
                "asin": asin,
                "status": "submit_failed",
                "error_code": "E201 ASYNC_SUBMIT_TIMEOUT",
                "warning": (
                    "E201 ASYNC_SUBMIT_TIMEOUT: ScraperAPI did not "
                    "accept the job quickly enough."
                ),
            }
            finish_state(token, result)
            jobs.append({**result, "token": token})
            continue
        except requests.RequestException as error:
            result = {
                "asin": asin,
                "status": "submit_failed",
                "error_code": "E202 ASYNC_SUBMIT_ERROR",
                "warning": f"E202 ASYNC_SUBMIT_ERROR: {error}",
            }
            finish_state(token, result)
            jobs.append({**result, "token": token})
            continue

        if response.status_code not in (200, 201, 202):
            result = {
                "asin": asin,
                "status": "submit_failed",
                "error_code": f"E203 ASYNC_SUBMIT_HTTP_{response.status_code}",
                "warning": (
                    f"E203 ASYNC_SUBMIT_HTTP_{response.status_code}: "
                    "ScraperAPI rejected the async job."
                ),
            }
            finish_state(token, result)
            jobs.append({**result, "token": token})
            continue

        try:
            data = response.json()
        except ValueError:
            data = {}

        job_id = str(data.get("id") or "").strip()
        status_url = str(data.get("statusUrl") or "").strip()

        if not job_id:
            result = {
                "asin": asin,
                "status": "submit_failed",
                "error_code": "E205 ASYNC_JOB_ID_MISSING",
                "warning": (
                    "E205 ASYNC_JOB_ID_MISSING: ScraperAPI accepted "
                    "the request but returned no job ID."
                ),
            }
            finish_state(token, result)
            jobs.append({**result, "token": token})
            continue

        with STATE_LOCK:
            SCAN_STATE[token]["status"] = "scanning"
            SCAN_STATE[token]["job_id"] = job_id
            SCAN_STATE[token]["status_url"] = status_url

        jobs.append(
            {
                "asin": asin,
                "token": token,
                "status": "submitted",
                "job_id": job_id,
            }
        )

    return {
        "marketplace": marketplace,
        "count": len(jobs),
        "jobs": jobs,
    }


@app.post("/api/scraper-callback/{token}")
async def scraper_callback(token: str, request: Request):
    try:
        payload = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid_json"}

    result = process_remote_payload(token, payload)
    if result is None:
        return {"ok": False, "error": "unknown_token"}

    return {"ok": True}


@app.post("/api/scan-status")
def scan_status(req: StatusRequest):
    cleanup_old_state()

    token = re.sub(r"[^a-fA-F0-9]", "", req.token)
    if not token:
        raise HTTPException(
            status_code=400,
            detail="E209 INVALID_SCAN_TOKEN",
        )

    with STATE_LOCK:
        state = SCAN_STATE.get(token)
        state_copy = dict(state) if state else None

    if not state_copy:
        return {
            "status": "poll_error",
            "error_code": "E217 SCAN_STATE_NOT_FOUND",
            "warning": (
                "E217 SCAN_STATE_NOT_FOUND: this Render instance no longer "
                "has the scan state. Start the scan again."
            ),
        }

    if state_copy.get("status") == "finished":
        return state_copy.get("result") or {
            "asin": state_copy.get("asin", ""),
            "status": "fetch_failed",
            "error_code": "E218 FINISHED_RESULT_MISSING",
            "warning": (
                "E218 FINISHED_RESULT_MISSING: scan finished "
                "but no result was stored."
            ),
        }

    status_url = state_copy.get("status_url") or ""
    now = time.time()
    last_remote_check = float(state_copy.get("last_remote_check") or 0.0)

    if status_url and now - last_remote_check >= 12:
        with STATE_LOCK:
            if token in SCAN_STATE:
                SCAN_STATE[token]["last_remote_check"] = now

        try:
            remote = requests.get(
                status_url,
                timeout=(2, 4),
            )

            if remote.status_code == 200:
                try:
                    payload = remote.json()
                except ValueError:
                    payload = None

                if isinstance(payload, dict):
                    remote_status = str(payload.get("status") or "").lower()

                    if remote_status in ("finished", "failed"):
                        result = process_remote_payload(token, payload)
                        if result is not None:
                            return result
        except requests.RequestException:
            pass

    return {
        "asin": state_copy.get("asin", ""),
        "status": "scanning",
        "job_id": state_copy.get("job_id"),
        "message": "Waiting for ScraperAPI result",
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
