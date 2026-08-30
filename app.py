from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from typing import List, Optional
from uuid import uuid4
import html as html_lib
import logging
import os
import re
import threading
import copy
import time
import requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup

app = FastAPI(title="Amazon Live Listing Scanner")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("listing-scanner")

BUILD_ID = "2026-08-30-fast-regulatory-spapi-v2"

MARKETPLACES = {
    "UK": {"url": "https://www.amazon.co.uk/dp/{}", "country": "uk"},
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
    r"\b[A-Z]{2,3}[\s\-–—−‐-]*BIO[\s\-–—−‐-]*\d{2,3}\b",
    re.I,
)

SCAN_STATE = {}
STATE_LOCK = threading.Lock()
STATE_TTL_SECONDS = 60 * 60 * 3
MAX_JOB_SECONDS = 12 * 60

# ---------------------------------------------------------------------------
# Optional authorised Seller Central / SP-API catalogue admin
# ---------------------------------------------------------------------------
SP_API_ENDPOINT = os.getenv(
    "SP_API_ENDPOINT",
    "https://sellingpartnerapi-eu.amazon.com",
).rstrip("/")
SP_API_CLIENT_ID = (
    os.getenv("SP_API_CLIENT_ID", "").strip()
    or os.getenv("LWA_CLIENT_ID", "").strip()
)
SP_API_CLIENT_SECRET = (
    os.getenv("SP_API_CLIENT_SECRET", "").strip()
    or os.getenv("LWA_CLIENT_SECRET", "").strip()
)
SP_API_REFRESH_TOKEN = (
    os.getenv("SP_API_REFRESH_TOKEN", "").strip()
    or os.getenv("LWA_REFRESH_TOKEN", "").strip()
)
SP_API_SELLER_ID = (
    os.getenv("SP_API_SELLER_ID", "").strip()
    or os.getenv("SELLER_ID", "").strip()
)
CATALOG_ADMIN_KEY = os.getenv("CATALOG_ADMIN_KEY", "").strip()

SP_MARKETPLACE_IDS = {
    "UK": "A1F83G8C2ARO7P",
    "IE": "A28R8C7NBKEWEA",
    "DE": "A1PA6795UKMFR9",
    "FR": "A13V1IB3VIYZZH",
    "IT": "APJ6JRA9NG5V4",
    "ES": "A1RKKUPIHCS9HS",
    "NL": "A1805IZSGTT6HS",
    "BE": "AMEN7PMS3EDWL",
}

SP_TOKEN_STATE = {"access_token": "", "expires_at": 0.0}
SP_TOKEN_LOCK = threading.Lock()


class StartRequest(BaseModel):
    asins: List[str]
    marketplace: str = "UK"
    terms: List[str] = DEFAULT_TERMS


class StatusRequest(BaseModel):
    token: str


class CatalogLookupRequest(BaseModel):
    asin: str
    marketplace: str = "UK"
    sku: Optional[str] = None


class CategoryPreviewRequest(CatalogLookupRequest):
    new_item_type_keyword: str


class CategoryApplyRequest(CategoryPreviewRequest):
    confirm: str = ""


def normalise(text):
    text = html_lib.unescape(str(text or ""))

    for dash in ("–", "—", "−", "‐", "-"):
        text = text.replace(dash, "-")

    return re.sub(r"\s+", " ", text).strip()


def clean_asin(raw):
    asin = re.sub(
        r"[^A-Z0-9]",
        "",
        str(raw).upper(),
    )
    return asin if len(asin) == 10 else ""


def add_section(sections, name, text):
    text = normalise(text)

    if text and text not in sections.values():
        sections[name] = text


def extract_sections(raw_html, log_context=""):
    """Extract the Amazon sections we care about without scanning the full
    raw HTML with an expensive DOTALL context regex.

    Amazon pages can be several megabytes.  The old fallback normalised the
    entire HTML string and then ran a broad ``.{0,500} ... .{0,900}`` regex
    across it.  On a small Render instance that could take minutes after the
    ScraperAPI job had already finished.

    This version keeps the normal BeautifulSoup extraction and uses cheap,
    bounded raw-HTML windows only when looking for regulatory fallbacks.
    """
    extract_started = time.monotonic()
    html_chars = len(raw_html or "")

    logger.info(
        "EXTRACT_START context=%s html_chars=%s build=%s",
        log_context or "-",
        html_chars,
        BUILD_ID,
    )

    soup_started = time.monotonic()
    soup = BeautifulSoup(raw_html, "html.parser")
    logger.info(
        "EXTRACT_SOUP_READY context=%s elapsed_ms=%s",
        log_context or "-",
        int((time.monotonic() - soup_started) * 1000),
    )

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
                module.get_text(" ", strip=True)
            )

            if len(text) < 20 or text in seen_modules:
                continue

            seen_modules.add(text)

            add_section(
                sections,
                f"A+ module {module_number}",
                text,
            )

            module_number += 1

        add_section(
            sections,
            "A+ full content",
            aplus.get_text(" ", strip=True),
        )

    regulatory_started = time.monotonic()

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

    # First use parsed text nodes.  This is the most accurate source because
    # it strips scripts/markup and lets us climb to the containing section.
    for text_node in soup.find_all(string=regulatory_marker):
        node = text_node.parent
        chosen = ""

        for _ in range(8):
            if node is None:
                break

            candidate = normalise(
                node.get_text(" ", strip=True)
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

        if chosen and chosen not in regulatory_seen:
            regulatory_seen.add(chosen)

            add_section(
                sections,
                f"Regulatory information {regulatory_number}",
                chosen,
            )

            regulatory_number += 1

    # FAST raw-source fallback.
    # Do NOT normalise the whole Amazon HTML.  Instead find only the exact
    # regulatory markers in the raw source and parse small bounded windows
    # around those positions.  This retains the fallback for data embedded
    # outside normal visible DOM sections while avoiding the multi-minute
    # full-page regex seen on Render.
    raw_hits = []

    phrase_pattern = re.compile(
        r"organic inspection body code",
        re.I,
    )

    for match in phrase_pattern.finditer(raw_html):
        raw_hits.append((match.start(), match.end()))
        if len(raw_hits) >= 20:
            break

    if len(raw_hits) < 20:
        for match in CONTROL_BODY_PATTERN.finditer(raw_html):
            raw_hits.append((match.start(), match.end()))
            if len(raw_hits) >= 20:
                break

    seen_windows = set()

    for hit_start, hit_end in raw_hits:
        left = max(0, hit_start - 900)
        right = min(len(raw_html), hit_end + 1600)

        # Prevent overlapping hits from making us parse essentially the same
        # fragment repeatedly.
        window_key = (left // 500, right // 500)
        if window_key in seen_windows:
            continue
        seen_windows.add(window_key)

        fragment = raw_html[left:right]
        text = normalise(
            BeautifulSoup(
                fragment,
                "html.parser",
            ).get_text(" ", strip=True)
        )

        if not text:
            continue

        if not (
            "organic inspection body code" in text.lower()
            or CONTROL_BODY_PATTERN.search(text)
        ):
            continue

        if text in regulatory_seen:
            continue

        regulatory_seen.add(text)

        add_section(
            sections,
            f"Regulatory source data {regulatory_number}",
            text,
        )

        regulatory_number += 1

    logger.info(
        "EXTRACT_REGULATORY_DONE context=%s elapsed_ms=%s raw_hits=%s regulatory_sections=%s",
        log_context or "-",
        int((time.monotonic() - regulatory_started) * 1000),
        len(raw_hits),
        sum(1 for name in sections if name.startswith("Regulatory")),
    )

    logger.info(
        "EXTRACT_DONE context=%s elapsed_ms=%s sections=%s",
        log_context or "-",
        int((time.monotonic() - extract_started) * 1000),
        len(sections),
    )

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
            r"(?<![A-Za-z0-9])bio(?![A-Za-z0-9])",
            re.I,
        )

    if lower == "eco":
        return re.compile(
            r"(?<![A-Za-z0-9])eco(?![A-Za-z0-9])",
            re.I,
        )

    if lower == "lu-bio-04":
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

    patterns.append(
        ("organic control-body code", CONTROL_BODY_PATTERN)
    )

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

    job_status = str(
        payload.get("status")
        or ""
    ).lower()

    if job_status == "failed":
        reason = str(
            payload.get("failReason")
            or "unknown_failure"
        )

        item["status"] = "fetch_failed"
        item["error_code"] = "E214 ASYNC_JOB_FAILED"
        item["warning"] = (
            "E214 ASYNC_JOB_FAILED: "
            + reason
        )

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
            "E216 ASYNC_BODY_EMPTY: "
            "the async job completed but no "
            "Amazon HTML body was returned."
        )

        return item

    if amazon_status != 200:
        item["status"] = "fetch_failed"
        item["error_code"] = (
            f"E220 AMAZON_HTTP_{amazon_status}"
        )
        item["warning"] = (
            f"{item['error_code']}: "
            f"ScraperAPI completed the job, "
            f"but Amazon returned HTTP {amazon_status}."
        )

        return item

    lower = body.lower()

    if (
        "enter the characters you see below" in lower
        or
        "sorry, we just need to make sure you're not a robot"
        in lower
    ):
        item["status"] = "blocked"
        item["error_code"] = "E230 AMAZON_CAPTCHA"
        item["warning"] = (
            "E230 AMAZON_CAPTCHA: "
            "Amazon returned a robot/CAPTCHA page."
        )

        return item

    processing_started = time.monotonic()

    logger.info(
        "PROCESS_START asin=%s marketplace=%s html_chars=%s build=%s",
        asin,
        marketplace,
        len(body),
        BUILD_ID,
    )

    sections = extract_sections(
        body,
        log_context=asin,
    )

    item["sections_found"] = list(sections.keys())
    item["title"] = sections.get("Title", "")

    match_started = time.monotonic()
    item["matches"] = find_matches(
        sections,
        build_search_terms(terms),
    )

    logger.info(
        "PROCESS_MATCHES_DONE asin=%s elapsed_ms=%s matches=%s",
        asin,
        int((time.monotonic() - match_started) * 1000),
        len(item["matches"]),
    )

    logger.info(
        "PROCESS_DONE asin=%s elapsed_ms=%s sections=%s matches=%s",
        asin,
        int((time.monotonic() - processing_started) * 1000),
        len(sections),
        len(item["matches"]),
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
        item["status"] = "incomplete"
        item["error_code"] = "E206 AMAZON_DATA_EMPTY"
        item["warning"] = (
            "E206 AMAZON_DATA_EMPTY: "
            "the job completed, but the product "
            "title could not be extracted."
        )

    elif not has_aplus and not has_regulatory:
        item["status"] = "incomplete"
        item["error_code"] = (
            "E207 TARGET_SECTIONS_NOT_RETURNED"
        )
        item["warning"] = (
            "E207 TARGET_SECTIONS_NOT_RETURNED: "
            "neither A+ nor Regulatory Information "
            "was present in the returned HTML. "
            "This ASIN is not confirmed clear."
        )

    elif not has_regulatory:
        item["status"] = "incomplete"
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


def finish_state(token, result):
    with STATE_LOCK:
        if token in SCAN_STATE:
            SCAN_STATE[token]["status"] = "finished"
            SCAN_STATE[token]["result"] = result

    logger.info(
        "SCAN_FINISHED token=%s asin=%s status=%s error=%s",
        token[:8],
        result.get("asin", ""),
        result.get("status", ""),
        result.get("error_code", ""),
    )


def background_run_job(token):
    with STATE_LOCK:
        state = SCAN_STATE.get(token)

        if state is None:
            logger.warning(
                "BACKGROUND_ABORT token=%s reason=state_missing",
                token[:8],
            )
            return

        state_copy = dict(state)

    asin = state_copy.get("asin", "")
    marketplace = state_copy.get("marketplace", "UK")

    target_url = (
        MARKETPLACES[marketplace]["url"].format(asin)
    )

    payload = {
        "apiKey": SCRAPERAPI_KEY,
        "url": target_url,
        "apiParams": {
            "country_code": MARKETPLACES[marketplace]["country"],
            "device_type": "mobile",
            "render": True,
        },
        "expectUnsuccessReport": True,
        "timeoutSec": 600,
        "meta": {
            "token": token,
            "asin": asin,
            "marketplace": marketplace,
        },
    }

    logger.info(
        "ASYNC_SUBMIT_START token=%s asin=%s marketplace=%s",
        token[:8],
        asin,
        marketplace,
    )

    try:
        response = requests.post(
            ASYNC_JOBS_URL,
            json=payload,
            timeout=(4, 12),
        )

    except requests.Timeout:
        logger.error(
            "ASYNC_SUBMIT_TIMEOUT token=%s asin=%s",
            token[:8],
            asin,
        )

        finish_state(
            token,
            {
                "asin": asin,
                "status": "submit_failed",
                "error_code": "E201 ASYNC_SUBMIT_TIMEOUT",
                "warning": (
                    "E201 ASYNC_SUBMIT_TIMEOUT: "
                    "ScraperAPI did not accept the background "
                    "job quickly enough."
                ),
            },
        )

        return

    except requests.RequestException as error:
        logger.exception(
            "ASYNC_SUBMIT_ERROR token=%s asin=%s error=%s",
            token[:8],
            asin,
            error,
        )

        finish_state(
            token,
            {
                "asin": asin,
                "status": "submit_failed",
                "error_code": "E202 ASYNC_SUBMIT_ERROR",
                "warning": (
                    "E202 ASYNC_SUBMIT_ERROR: "
                    + str(error)
                ),
            },
        )

        return

    logger.info(
        "ASYNC_SUBMIT_HTTP token=%s asin=%s http=%s",
        token[:8],
        asin,
        response.status_code,
    )

    if response.status_code not in (200, 201, 202):
        logger.error(
            "ASYNC_SUBMIT_REJECTED token=%s asin=%s http=%s body=%s",
            token[:8],
            asin,
            response.status_code,
            response.text[:500],
        )

        finish_state(
            token,
            {
                "asin": asin,
                "status": "submit_failed",
                "error_code": (
                    f"E203 ASYNC_SUBMIT_HTTP_"
                    f"{response.status_code}"
                ),
                "warning": (
                    f"E203 ASYNC_SUBMIT_HTTP_"
                    f"{response.status_code}: "
                    f"ScraperAPI rejected the async job."
                ),
            },
        )

        return

    try:
        data = response.json()

    except ValueError:
        logger.error(
            "ASYNC_SUBMIT_INVALID_JSON token=%s asin=%s body=%s",
            token[:8],
            asin,
            response.text[:500],
        )
        data = {}

    job_id = str(
        data.get("id")
        or ""
    ).strip()

    status_url = str(
        data.get("statusUrl")
        or ""
    ).strip()

    if not job_id:
        logger.error(
            "ASYNC_JOB_ID_MISSING token=%s asin=%s response=%s",
            token[:8],
            asin,
            str(data)[:500],
        )

        finish_state(
            token,
            {
                "asin": asin,
                "status": "submit_failed",
                "error_code": "E205 ASYNC_JOB_ID_MISSING",
                "warning": (
                    "E205 ASYNC_JOB_ID_MISSING: "
                    "ScraperAPI accepted the request "
                    "but returned no job ID."
                ),
            },
        )

        return

    if not status_url:
        status_url = (
            ASYNC_JOBS_URL
            + "/"
            + job_id
        )

    with STATE_LOCK:
        if token not in SCAN_STATE:
            logger.warning(
                "ASYNC_JOB_STATE_GONE token=%s asin=%s job_id=%s",
                token[:8],
                asin,
                job_id,
            )
            return

        SCAN_STATE[token]["status"] = "scanning"
        SCAN_STATE[token]["job_id"] = job_id
        SCAN_STATE[token]["status_url"] = status_url
        SCAN_STATE[token]["remote_status"] = "submitted"

    logger.info(
        "ASYNC_JOB_SUBMITTED token=%s asin=%s job_id=%s status_url=%s",
        token[:8],
        asin,
        job_id,
        status_url,
    )

    started_at = time.time()
    consecutive_errors = 0
    last_logged_status = None
    poll_number = 0

    while time.time() - started_at < MAX_JOB_SECONDS:
        with STATE_LOCK:
            state = SCAN_STATE.get(token)

            if state is None:
                logger.warning(
                    "ASYNC_POLL_ABORT token=%s asin=%s reason=state_missing",
                    token[:8],
                    asin,
                )
                return

            if state.get("status") == "finished":
                return

            state_copy = dict(state)

        poll_number += 1

        try:
            result_response = requests.get(
                status_url,
                timeout=(3, 6),
            )

            if result_response.status_code != 200:
                consecutive_errors += 1

                logger.warning(
                    "ASYNC_STATUS_HTTP token=%s asin=%s job_id=%s poll=%s http=%s consecutive_errors=%s",
                    token[:8],
                    asin,
                    job_id,
                    poll_number,
                    result_response.status_code,
                    consecutive_errors,
                )

            else:
                try:
                    result_payload = result_response.json()

                except ValueError:
                    result_payload = None
                    consecutive_errors += 1

                    logger.warning(
                        "ASYNC_STATUS_INVALID_JSON token=%s asin=%s job_id=%s poll=%s body=%s",
                        token[:8],
                        asin,
                        job_id,
                        poll_number,
                        result_response.text[:500],
                    )

                if isinstance(result_payload, dict):
                    job_status = str(
                        result_payload.get("status")
                        or ""
                    ).lower()

                    attempts = result_payload.get("attempts")

                    with STATE_LOCK:
                        if token in SCAN_STATE:
                            SCAN_STATE[token]["remote_status"] = job_status
                            SCAN_STATE[token]["attempts"] = attempts

                    if (
                        job_status != last_logged_status
                        or poll_number % 10 == 0
                    ):
                        logger.info(
                            "ASYNC_STATUS token=%s asin=%s job_id=%s poll=%s status=%s attempts=%s",
                            token[:8],
                            asin,
                            job_id,
                            poll_number,
                            job_status or "unknown",
                            attempts,
                        )
                        last_logged_status = job_status

                    if job_status in ("finished", "failed"):
                        logger.info(
                            "ASYNC_TERMINAL token=%s asin=%s job_id=%s status=%s",
                            token[:8],
                            asin,
                            job_id,
                            job_status,
                        )

                        result = result_from_payload(
                            state_copy,
                            result_payload,
                        )

                        finish_state(
                            token,
                            result,
                        )

                        return

                    consecutive_errors = 0

        except requests.Timeout:
            consecutive_errors += 1

            logger.warning(
                "ASYNC_STATUS_TIMEOUT token=%s asin=%s job_id=%s poll=%s consecutive_errors=%s",
                token[:8],
                asin,
                job_id,
                poll_number,
                consecutive_errors,
            )

        except requests.RequestException as error:
            consecutive_errors += 1

            logger.warning(
                "ASYNC_STATUS_ERROR token=%s asin=%s job_id=%s poll=%s consecutive_errors=%s error=%s",
                token[:8],
                asin,
                job_id,
                poll_number,
                consecutive_errors,
                error,
            )

        if consecutive_errors >= 25:
            logger.error(
                "ASYNC_STATUS_GIVEUP token=%s asin=%s job_id=%s errors=%s",
                token[:8],
                asin,
                job_id,
                consecutive_errors,
            )

            finish_state(
                token,
                {
                    "asin": asin,
                    "status": "fetch_failed",
                    "error_code": "E221 BACKGROUND_STATUS_FAILURE",
                    "warning": (
                        "E221 BACKGROUND_STATUS_FAILURE: "
                        "Render could not read the ScraperAPI job status "
                        "after 25 consecutive attempts."
                    ),
                },
            )

            return

        time.sleep(4)

    logger.error(
        "ASYNC_WATCHDOG_TIMEOUT token=%s asin=%s job_id=%s",
        token[:8],
        asin,
        job_id,
    )

    finish_state(
        token,
        {
            "asin": asin,
            "status": "fetch_failed",
            "error_code": "E306 SCAN_WATCHDOG_TIMEOUT",
            "warning": (
                "E306 SCAN_WATCHDOG_TIMEOUT: "
                "the ScraperAPI job did not finish "
                "within 12 minutes."
            ),
        },
    )


@app.post("/api/scan-start")
def scan_start(req: StartRequest):
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
                "E110 SCRAPERAPI_KEY_ERROR: "
                "SCRAPERAPI_KEY is missing in Render."
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

    logger.info(
        "SCAN_START marketplace=%s asins=%s terms=%s",
        marketplace,
        asins,
        req.terms,
    )

    jobs = []

    for asin in asins[:20]:
        token = uuid4().hex

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
                "remote_status": "not_submitted",
                "attempts": None,
            }

        thread = threading.Thread(
            target=background_run_job,
            args=(token,),
            daemon=True,
        )

        thread.start()

        jobs.append(
            {
                "asin": asin,
                "token": token,
                "status": "submitted",
                "job_id": None,
            }
        )

        logger.info(
            "SCAN_THREAD_STARTED token=%s asin=%s",
            token[:8],
            asin,
        )

    return {
        "marketplace": marketplace,
        "count": len(jobs),
        "jobs": jobs,
    }


@app.post("/api/scan-status")
def scan_status(req: StatusRequest):
    cleanup_old_state()

    token = re.sub(
        r"[^a-fA-F0-9]",
        "",
        req.token,
    )

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
                "E217 SCAN_STATE_NOT_FOUND: "
                "this Render instance no longer has "
                "the scan state. Start the scan again."
            ),
        }

    if state_copy.get("status") == "finished":
        return state_copy.get("result") or {
            "asin": state_copy.get("asin", ""),
            "status": "fetch_failed",
            "error_code": "E218 FINISHED_RESULT_MISSING",
            "warning": (
                "E218 FINISHED_RESULT_MISSING: "
                "scan finished but no result was stored."
            ),
        }

    return {
        "asin": state_copy.get("asin", ""),
        "status": "scanning",
        "job_id": state_copy.get("job_id"),
        "remote_status": state_copy.get("remote_status"),
        "attempts": state_copy.get("attempts"),
        "message": "Background scan still running",
    }



# ---------------------------------------------------------------------------
# Authorised SP-API catalogue diagnostics and controlled category correction
# ---------------------------------------------------------------------------

def _admin_guard(x_admin_key: str):
    if not CATALOG_ADMIN_KEY:
        raise HTTPException(
            status_code=503,
            detail=(
                "CATALOG_ADMIN_KEY is not configured. Add a long random value "
                "in Render before enabling catalogue-admin endpoints."
            ),
        )
    if x_admin_key != CATALOG_ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid catalogue admin key")


def _sp_config_missing():
    values = {
        "SP_API_CLIENT_ID": SP_API_CLIENT_ID,
        "SP_API_CLIENT_SECRET": SP_API_CLIENT_SECRET,
        "SP_API_REFRESH_TOKEN": SP_API_REFRESH_TOKEN,
        "SP_API_SELLER_ID": SP_API_SELLER_ID,
    }
    return [name for name, value in values.items() if not value]


def _sp_require_config():
    missing = _sp_config_missing()
    if missing:
        raise HTTPException(
            status_code=503,
            detail="Missing SP-API Render environment variables: " + ", ".join(missing),
        )


def _sp_marketplace(marketplace: str):
    code = str(marketplace or "UK").strip().upper()
    if code not in SP_MARKETPLACE_IDS:
        raise HTTPException(status_code=400, detail="Unsupported SP-API marketplace: " + code)
    return code, SP_MARKETPLACE_IDS[code]


def _safe_json_response(response):
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text[:4000]}


def _lwa_access_token(force_refresh=False):
    _sp_require_config()
    now = time.time()
    with SP_TOKEN_LOCK:
        if (
            not force_refresh
            and SP_TOKEN_STATE.get("access_token")
            and SP_TOKEN_STATE.get("expires_at", 0) > now + 90
        ):
            return SP_TOKEN_STATE["access_token"]

        try:
            response = requests.post(
                "https://api.amazon.com/auth/o2/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": SP_API_REFRESH_TOKEN,
                    "client_id": SP_API_CLIENT_ID,
                    "client_secret": SP_API_CLIENT_SECRET,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                timeout=(4, 15),
            )
        except requests.RequestException as error:
            raise HTTPException(status_code=502, detail="Could not reach Amazon LWA token service: " + str(error))

        data = _safe_json_response(response)
        if response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "Amazon LWA token request failed",
                    "http_status": response.status_code,
                    "amazon": data,
                },
            )

        token = str(data.get("access_token") or "").strip()
        if not token:
            raise HTTPException(status_code=502, detail="Amazon LWA response contained no access_token")
        expires_in = int(data.get("expires_in") or 3600)
        SP_TOKEN_STATE["access_token"] = token
        SP_TOKEN_STATE["expires_at"] = now + max(300, expires_in)
        return token


def _sp_request(method, path, params=None, json_body=None, retry_auth=True):
    token = _lwa_access_token()
    url = SP_API_ENDPOINT + path
    headers = {
        "x-amz-access-token": token,
        "x-amz-date": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "user-agent": "MewFriendsListingScanner/2.0 (Language=Python)",
        "accept": "application/json",
    }
    if json_body is not None:
        headers["content-type"] = "application/json"

    try:
        response = requests.request(
            method,
            url,
            params=params,
            json=json_body,
            headers=headers,
            timeout=(5, 25),
        )
    except requests.RequestException as error:
        raise HTTPException(status_code=502, detail="SP-API request failed: " + str(error))

    if response.status_code == 401 and retry_auth:
        _lwa_access_token(force_refresh=True)
        return _sp_request(method, path, params=params, json_body=json_body, retry_auth=False)

    data = _safe_json_response(response)
    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail={
                "message": "Amazon SP-API returned an error",
                "http_status": response.status_code,
                "request_id": response.headers.get("x-amzn-RequestId"),
                "amazon": data,
            },
        )
    return data


def _safe_sp_call(label, fn):
    try:
        return {"ok": True, "data": fn()}
    except HTTPException as error:
        return {"ok": False, "error": error.detail, "label": label}
    except Exception as error:
        logger.exception("SP_API_DIAGNOSTIC_ERROR label=%s error=%s", label, error)
        return {"ok": False, "error": str(error), "label": label}


def _search_listing_by_asin(asin, marketplace_id):
    return _sp_request(
        "GET",
        f"/listings/2021-08-01/items/{SP_API_SELLER_ID}",
        params={
            "marketplaceIds": marketplace_id,
            "identifiers": asin,
            "identifiersType": "ASIN",
            "includedData": "summaries,attributes,issues,productTypes,fulfillmentAvailability,relationships",
            "pageSize": 20,
        },
    )


def _get_listing_item(sku, marketplace_id):
    return _sp_request(
        "GET",
        f"/listings/2021-08-01/items/{SP_API_SELLER_ID}/{requests.utils.quote(sku, safe='')}",
        params={
            "marketplaceIds": marketplace_id,
            "includedData": "summaries,attributes,issues,productTypes,fulfillmentAvailability,relationships",
        },
    )


def _get_catalog_item(asin, marketplace_id):
    return _sp_request(
        "GET",
        f"/catalog/2022-04-01/items/{asin}",
        params={
            "marketplaceIds": marketplace_id,
            "includedData": "attributes,classifications,productTypes,summaries,relationships",
        },
    )


def _get_listing_restrictions(asin, marketplace_id, product_type=None):
    params = {
        "asin": asin,
        "sellerId": SP_API_SELLER_ID,
        "marketplaceIds": marketplace_id,
        "conditionType": "new_new",
    }
    if product_type:
        params["productType"] = product_type
    return _sp_request("GET", "/listings/2021-08-01/restrictions", params=params)


def _get_inbound_eligibility(asin, marketplace_id):
    return _sp_request(
        "GET",
        "/fba/inbound/v1/eligibility/itemPreview",
        params={"asin": asin, "program": "INBOUND", "marketplaceIds": marketplace_id},
    )


def _product_type_recommendations(item_name, marketplace_id):
    item_name = normalise(item_name)[:500]
    if not item_name:
        return {"productTypes": []}
    return _sp_request(
        "GET",
        "/definitions/2020-09-01/productTypes",
        params={"marketplaceIds": marketplace_id, "itemName": item_name},
    )


def _current_product_type(listing):
    for entry in listing.get("productTypes") or []:
        if isinstance(entry, dict):
            value = entry.get("productType") or entry.get("product_type")
            if value:
                return str(value)
    return ""


def _listing_title(listing):
    for summary in listing.get("summaries") or []:
        if not isinstance(summary, dict):
            continue
        for key in ("itemName", "item_name"):
            if summary.get(key):
                return str(summary[key])
    return ""


def _relevant_attributes(attributes):
    wanted = {}
    needles = ("item_type", "browse", "category", "product_type", "organic", "bio", "eco", "control", "regulatory")
    for key, value in (attributes or {}).items():
        lower = str(key).lower()
        if any(needle in lower for needle in needles):
            wanted[key] = value
    return wanted


def _find_suspicious_values(value, path="$", output=None):
    if output is None:
        output = []
    if len(output) >= 100:
        return output
    if isinstance(value, dict):
        for key, child in value.items():
            _find_suspicious_values(child, f"{path}.{key}", output)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _find_suspicious_values(child, f"{path}[{index}]", output)
    else:
        text = normalise(value)
        lower = text.lower()
        if "organic" in lower or "lu-bio" in lower or "lu bio" in lower or CONTROL_BODY_PATTERN.search(text):
            output.append({"path": path, "value": text[:1000]})
    return output


def _resolve_listing(asin, marketplace_id, sku=None):
    if sku:
        listing = _get_listing_item(str(sku).strip(), marketplace_id)
        return listing, [str(sku).strip()]
    search = _search_listing_by_asin(asin, marketplace_id)
    items = search.get("items") or []
    sku_candidates = [str(item.get("sku")) for item in items if isinstance(item, dict) and item.get("sku")]
    if not items:
        raise HTTPException(status_code=404, detail="No seller SKU was returned for this ASIN in the selected marketplace.")
    return items[0], sku_candidates


def _diagnose_catalog(req):
    asin = clean_asin(req.asin)
    if not asin:
        raise HTTPException(status_code=400, detail="Invalid ASIN")
    marketplace, marketplace_id = _sp_marketplace(req.marketplace)
    listing, sku_candidates = _resolve_listing(asin, marketplace_id, sku=(req.sku or "").strip() or None)
    product_type = _current_product_type(listing)
    title = _listing_title(listing)

    catalog_call = _safe_sp_call("catalog_item", lambda: _get_catalog_item(asin, marketplace_id))
    restrictions_call = _safe_sp_call(
        "listing_restrictions",
        lambda: _get_listing_restrictions(asin, marketplace_id, product_type=product_type or None),
    )
    inbound_call = _safe_sp_call("inbound_eligibility", lambda: _get_inbound_eligibility(asin, marketplace_id))
    recommendations_call = _safe_sp_call(
        "product_type_recommendations",
        lambda: _product_type_recommendations(title, marketplace_id),
    )

    catalog_data = catalog_call.get("data") if catalog_call.get("ok") else {}
    return {
        "build": BUILD_ID,
        "marketplace": marketplace,
        "marketplace_id": marketplace_id,
        "asin": asin,
        "sku_candidates": sku_candidates,
        "selected_sku": (req.sku or "").strip() or (sku_candidates[0] if len(sku_candidates) == 1 else None),
        "current_product_type": product_type,
        "title": title,
        "listing_statuses": listing.get("summaries") or [],
        "listing_issues": listing.get("issues") or [],
        "classification_attributes": _relevant_attributes(listing.get("attributes") or {}),
        "all_product_types": listing.get("productTypes") or [],
        "catalog_classifications": catalog_data.get("classifications") or [] if isinstance(catalog_data, dict) else [],
        "catalog_product_types": catalog_data.get("productTypes") or [] if isinstance(catalog_data, dict) else [],
        "organic_or_control_body_hits": {
            "listing": _find_suspicious_values(listing),
            "catalog": _find_suspicious_values(catalog_data),
        },
        "listing_restrictions": restrictions_call,
        "inbound_eligibility": inbound_call,
        "product_type_recommendations": recommendations_call,
        "note": (
            "gl_product_group is an Amazon-internal derived field and is not directly writable here. "
            "This tool corrects seller-controlled classification inputs and rechecks the official inbound gate."
        ),
    }


def _build_item_type_patch(listing, marketplace_id, new_keyword):
    new_keyword = normalise(new_keyword)
    if not new_keyword:
        raise HTTPException(status_code=400, detail="new_item_type_keyword cannot be blank")
    attributes = listing.get("attributes") or {}
    current = attributes.get("item_type_keyword")
    if isinstance(current, list) and current:
        new_value = copy.deepcopy(current)
        first = new_value[0]
        if isinstance(first, dict):
            first["value"] = new_keyword
            first.setdefault("marketplace_id", marketplace_id)
        else:
            new_value = [{"value": new_keyword, "marketplace_id": marketplace_id}]
        op = "replace"
    else:
        new_value = [{"value": new_keyword, "marketplace_id": marketplace_id}]
        op = "add"
    return {"op": op, "path": "/attributes/item_type_keyword", "value": new_value}


def _prepare_category_patch(req):
    asin = clean_asin(req.asin)
    if not asin:
        raise HTTPException(status_code=400, detail="Invalid ASIN")
    marketplace, marketplace_id = _sp_marketplace(req.marketplace)
    listing, sku_candidates = _resolve_listing(asin, marketplace_id, sku=(req.sku or "").strip() or None)
    if len(sku_candidates) != 1 and not (req.sku or "").strip():
        raise HTTPException(
            status_code=409,
            detail={
                "message": "More than one seller SKU is attached to this ASIN. Choose the exact SKU before writing.",
                "sku_candidates": sku_candidates,
            },
        )
    sku = (req.sku or "").strip() or sku_candidates[0]
    listing = _get_listing_item(sku, marketplace_id)
    product_type = _current_product_type(listing)
    if not product_type:
        raise HTTPException(status_code=409, detail="Amazon did not return the current productType, so no write was attempted.")
    patch = _build_item_type_patch(listing, marketplace_id, req.new_item_type_keyword)
    body = {"productType": product_type, "patches": [patch]}
    return asin, marketplace, marketplace_id, sku, product_type, listing, body


def _preview_category_patch(req):
    asin, marketplace, marketplace_id, sku, product_type, listing, body = _prepare_category_patch(req)
    response = _sp_request(
        "PATCH",
        f"/listings/2021-08-01/items/{SP_API_SELLER_ID}/{requests.utils.quote(sku, safe='')}",
        params={"marketplaceIds": marketplace_id, "mode": "VALIDATION_PREVIEW", "includedData": "issues,identifiers"},
        json_body=body,
    )
    issues = response.get("issues") or []
    errors = [issue for issue in issues if str((issue or {}).get("severity", "")).upper() == "ERROR"]
    return {
        "marketplace": marketplace,
        "asin": asin,
        "sku": sku,
        "current_product_type": product_type,
        "current_item_type_keyword": (listing.get("attributes") or {}).get("item_type_keyword"),
        "proposed_item_type_keyword": req.new_item_type_keyword,
        "request_body": body,
        "validation": response,
        "has_validation_errors": bool(errors),
        "validation_errors": errors,
        "safe_to_submit": not bool(errors),
    }


@app.post("/api/catalog/diagnostic")
def catalog_diagnostic(req: CatalogLookupRequest, x_admin_key: str = Header(default="")):
    _admin_guard(x_admin_key)
    _sp_require_config()
    return _diagnose_catalog(req)


@app.post("/api/catalog/category-preview")
def catalog_category_preview(req: CategoryPreviewRequest, x_admin_key: str = Header(default="")):
    _admin_guard(x_admin_key)
    _sp_require_config()
    return _preview_category_patch(req)


@app.post("/api/catalog/category-apply")
def catalog_category_apply(req: CategoryApplyRequest, x_admin_key: str = Header(default="")):
    _admin_guard(x_admin_key)
    _sp_require_config()
    if req.confirm.strip().upper() != "APPLY":
        raise HTTPException(status_code=400, detail='To make a live catalogue change, confirm must equal "APPLY".')

    preview = _preview_category_patch(req)
    if preview.get("has_validation_errors"):
        raise HTTPException(
            status_code=409,
            detail={"message": "Amazon validation preview contains ERROR issues. Nothing was changed.", "preview": preview},
        )

    _, marketplace_id = _sp_marketplace(req.marketplace)
    sku = preview["sku"]
    body = preview["request_body"]
    response = _sp_request(
        "PATCH",
        f"/listings/2021-08-01/items/{SP_API_SELLER_ID}/{requests.utils.quote(sku, safe='')}",
        params={"marketplaceIds": marketplace_id},
        json_body=body,
    )
    inbound = _safe_sp_call(
        "inbound_eligibility_after_submit",
        lambda: _get_inbound_eligibility(preview["asin"], marketplace_id),
    )
    restrictions = _safe_sp_call(
        "listing_restrictions_after_submit",
        lambda: _get_listing_restrictions(preview["asin"], marketplace_id, product_type=preview["current_product_type"]),
    )
    return {
        "submitted": True,
        "amazon_submission": response,
        "preview_used": preview,
        "inbound_eligibility_immediate_recheck": inbound,
        "listing_restrictions_immediate_recheck": restrictions,
        "message": (
            "The seller-controlled item_type_keyword patch was submitted. Amazon may take time to "
            "recalculate catalogue classification and FBA inbound eligibility."
        ),
    }


@app.get("/catalog-admin", response_class=HTMLResponse)
def catalog_admin_page():
    return HTMLResponse(r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Amazon Inbound Gate / Catalogue Admin</title>
<style>
body{font-family:Arial,sans-serif;max-width:980px;margin:24px auto;padding:0 16px;background:#f7f7f7;color:#222}
.card{background:#fff;border:1px solid #ddd;border-radius:12px;padding:18px;margin:14px 0;box-shadow:0 1px 3px #0001}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}.row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}
label{font-weight:700;font-size:14px;display:block;margin-bottom:5px}input,select,button{font:inherit;padding:10px;border-radius:8px;border:1px solid #bbb;width:100%;box-sizing:border-box}
button{background:#1769aa;color:#fff;border:0;font-weight:700;cursor:pointer}.danger{background:#a31d1d}.secondary{background:#555}
pre{white-space:pre-wrap;word-break:break-word;background:#111;color:#eee;padding:12px;border-radius:8px;max-height:620px;overflow:auto}
.small{font-size:13px;color:#555}.warn{background:#fff4d6;border-left:5px solid #e7a300;padding:10px 12px;border-radius:6px}
.ok{color:#087a24;font-weight:700}.bad{color:#a31d1d;font-weight:700}@media(max-width:700px){.row,.row3{grid-template-columns:1fr}}
</style></head><body>
<h1>Amazon Inbound Gate / Catalogue Admin</h1>
<p class="warn"><b>This uses Amazon SP-API, not a hidden Seller Central backend.</b> It checks your listing classification, official listing restrictions and FBA INBOUND eligibility, then lets you validation-preview and submit a correction to the seller-controlled <code>item_type_keyword</code>. It cannot directly edit Amazon's internal <code>gl_product_group</code>.</p>
<div class="card"><div class="row3">
<div><label>Admin key</label><input id="key" type="password" autocomplete="off"></div>
<div><label>Marketplace</label><select id="mp"><option>UK</option><option>IE</option><option>BE</option><option>DE</option><option>FR</option><option>IT</option><option>ES</option><option>NL</option></select></div>
<div><label>ASIN</label><input id="asin" value="B083V8GFSZ"></div></div>
<div style="margin-top:12px"><label>SKU (leave blank unless Amazon returns more than one)</label><input id="sku"></div>
<div style="margin-top:12px" class="row"><button onclick="diagnose()">Diagnose classification + inbound gate</button><button class="secondary" onclick="compareIE()">Compare same ASIN in Ireland</button></div></div>
<div class="card"><h2>Controlled category correction</h2>
<p class="small">Enter only the correct Amazon item-type keyword. Preview does not change the live listing. Apply re-runs preview and refuses to submit if Amazon returns an ERROR.</p>
<label>New item_type_keyword</label><input id="keyword" placeholder="Enter the correct Amazon item type keyword">
<div class="row" style="margin-top:12px"><button onclick="previewPatch()">Validation preview only</button><button class="danger" onclick="applyPatch()">APPLY live correction</button></div></div>
<div class="card"><h2>Result</h2><div id="status" class="small"></div><pre id="out">Ready.</pre></div>
<script>
const out=document.getElementById('out'), statusEl=document.getElementById('status');
function payload(extra={}){return Object.assign({asin:document.getElementById('asin').value.trim(),marketplace:document.getElementById('mp').value,sku:document.getElementById('sku').value.trim()||null},extra)}
async function call(path,body){statusEl.textContent='Working…';out.textContent='Working…';try{const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','x-admin-key':document.getElementById('key').value},body:JSON.stringify(body)});const data=await r.json();out.textContent=JSON.stringify(data,null,2);statusEl.textContent=r.ok?'Completed':'Amazon/app returned an error';statusEl.className=r.ok?'ok':'bad';return {ok:r.ok,data};}catch(e){out.textContent=String(e);statusEl.textContent='Request failed';statusEl.className='bad';}}
function diagnose(){return call('/api/catalog/diagnostic',payload())}
async function compareIE(){const original=document.getElementById('mp').value;document.getElementById('mp').value='IE';await diagnose();document.getElementById('mp').value=original;}
function previewPatch(){const k=document.getElementById('keyword').value.trim();if(!k){alert('Enter a new item_type_keyword first.');return;}return call('/api/catalog/category-preview',payload({new_item_type_keyword:k}))}
async function applyPatch(){const k=document.getElementById('keyword').value.trim();if(!k){alert('Enter a new item_type_keyword first.');return;}if(!confirm('This will submit a LIVE seller catalogue change to Amazon. Continue?'))return;return call('/api/catalog/category-apply',payload({new_item_type_keyword:k,confirm:'APPLY'}))}
</script></body></html>''')


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
