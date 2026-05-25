#!/usr/bin/env python3
"""
Wizard of Words Dashboard - Live Web App
Serves a dashboard with live data from Eventbrite + Facebook Ads APIs.
Covers both "Wizard of Words" and "GifterX Talks" events for Christopher Kai.

Infrastructure: Background threading, disk cache, parallel FB calls,
retry logic, proactive refresh — ported from Speakpreneur dashboard.
"""
import os
import requests
import json
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, Response, jsonify, request

ET = ZoneInfo("America/New_York")

app = Flask(__name__)

def _eb_request(url, params, timeout=(10, 30), max_retries=5):
    """Make an Eventbrite API request with retry logic for 429 rate limits."""
    for attempt in range(max_retries):
        res = requests.get(url, params=params, timeout=timeout)
        if res.status_code == 429:
            w = min(2 ** attempt * 2, 30)  # 2s, 4s, 8s, 16s, 30s
            print(f"[EB] 429 rate limit, waiting {w}s (attempt {attempt+1}/{max_retries})...", flush=True)
            time.sleep(w)
            continue
        res.raise_for_status()
        return res
    # Final attempt without catching
    res = requests.get(url, params=params, timeout=timeout)
    res.raise_for_status()
    return res

# ====== CONFIG (from environment variables) ======
EB_TOKEN = os.environ.get("EB_TOKEN", "")
EB_ORG_ID = os.environ.get("EB_ORG_ID", "")
FB_TOKEN = os.environ.get("FB_TOKEN", "")
FB_AD_ACCOUNT = os.environ.get("FB_AD_ACCOUNT", "")

# ====== CACHE SYSTEM (background threading + disk persistence) ======
_cache = {"html": None, "time": 0, "building": False, "build_thread": None}
CACHE_TTL = 1800  # 30 minutes
CACHE_FILE = "/tmp/wow_dashboard_cache.html"
CACHE_TIME_FILE = "/tmp/wow_dashboard_cache_time.txt"

# Track API errors for dashboard display
_api_errors = []

BUILD_TIMEOUT = int(os.environ.get("BUILD_TIMEOUT", "900"))  # 15 min default, overridable via env var
_build_lock = threading.Lock()  # Prevent multiple simultaneous builds

# City name normalization (EB name → canonical name matching FB)
CITY_NORMALIZE = {
    "new york city": "New York",
    "st. pete's/tampa": "Tampa",
    "st pete's/tampa": "Tampa",
    "washington dc": "Washington",
}

def normalize_city(city):
    c = city.strip()
    c = re.sub(r',\s*[A-Z]{2}$', '', c)
    lower = c.lower().strip()
    return CITY_NORMALIZE.get(lower, c)

def _save_cache_to_disk(html):
    """Persist cache to disk so it survives Render restarts."""
    try:
        with open(CACHE_FILE, "w") as f:
            f.write(html)
        with open(CACHE_TIME_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass  # Best effort — disk cache is optional

def _load_cache_from_disk():
    """Load cached HTML from disk on startup (instant cold start)."""
    try:
        with open(CACHE_FILE, "r") as f:
            html = f.read()
        if not html or len(html) < 1000:
            return None, 0
        try:
            with open(CACHE_TIME_FILE, "r") as f:
                cache_time = float(f.read().strip())
        except Exception:
            cache_time = os.path.getmtime(CACHE_FILE)
        return html, cache_time
    except Exception:
        pass
    return None, 0

def _build_cache_background():
    """Build the dashboard HTML in a background thread."""
    if not _build_lock.acquire(blocking=False):
        return  # Another build is already running
    _cache["building"] = True
    _cache["build_start"] = time.time()
    _cache["build_thread"] = threading.current_thread()
    start = time.time()
    try:
        html = build_dashboard_html()
        _cache["html"] = html
        _cache["time"] = time.time()
        _save_cache_to_disk(html)
        print(f"[CACHE] Build succeeded in {time.time()-start:.1f}s", flush=True)
    except Exception as e:
        import traceback
        print(f"[CACHE] Background build failed after {time.time()-start:.1f}s: {e}", flush=True)
        traceback.print_exc()
        # Try loading disk cache as fallback
        disk_html, disk_time = _load_cache_from_disk()
        if disk_html:
            _cache["html"] = disk_html
            _cache["time"] = disk_time
            print(f"[CACHE] Loaded disk cache as fallback ({len(disk_html)} bytes)", flush=True)
        elif not _cache["html"]:
            _cache["html"] = _build_error_html(str(e))
            _cache["time"] = time.time()
    finally:
        _cache["building"] = False
        _cache["build_thread"] = None
        _build_lock.release()

def _ensure_cache():
    """Trigger a background rebuild if cache is stale. Non-blocking."""
    if _cache["html"] and (time.time() - _cache["time"]) < CACHE_TTL:
        return  # Cache is fresh
    if _cache["building"]:
        build_thread = _cache.get("build_thread")
        thread_dead = build_thread is not None and not build_thread.is_alive()
        build_start = _cache.get("build_start", 0)
        elapsed = time.time() - build_start if build_start else 0
        timed_out = build_start and elapsed > BUILD_TIMEOUT
        if thread_dead or timed_out:
            reason = "thread died" if thread_dead else f"timed out after {elapsed:.0f}s"
            print(f"[CACHE] Build stuck ({reason}) — resetting", flush=True)
            _cache["building"] = False
            _cache["build_thread"] = None
            try:
                _build_lock.release()
            except RuntimeError:
                pass
            if not _cache["html"]:
                disk_html, disk_time = _load_cache_from_disk()
                if disk_html:
                    _cache["html"] = disk_html
                    _cache["time"] = disk_time
                    print(f"[CACHE] Loaded disk cache as fallback ({len(disk_html)} bytes)", flush=True)
                else:
                    _cache["html"] = _build_error_html(
                        f"Dashboard build {reason}. The APIs may be slow. Try /refresh in a minute.")
                    _cache["time"] = time.time()
        else:
            return  # Still building, wait
    t = threading.Thread(target=_build_cache_background, daemon=True, name="cache-builder")
    t.start()

def _proactive_refresh_loop():
    """Proactively rebuild cache before it expires, so users never wait."""
    refresh_interval = CACHE_TTL - 300  # Rebuild 5 min before TTL expires
    if refresh_interval < 300:
        refresh_interval = 300
    while True:
        time.sleep(refresh_interval)
        try:
            if not _cache["building"]:
                _build_cache_background()
        except Exception:
            pass

def _build_error_html(error_msg):
    """Return a simple error page that auto-retries."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Wizard of Words Dashboard — Error</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 20px; }}
h1 {{ font-size: 28px; font-weight: 700; margin-bottom: 8px; }}
h1 span {{ color: #f59e0b; }}
.error {{ background: #1e293b; border: 1px solid #ef4444; border-radius: 8px; padding: 20px; max-width: 600px; margin: 20px 0; }}
.error p {{ color: #fca5a5; font-size: 14px; margin-bottom: 12px; }}
.btn {{ display: inline-block; background: #f59e0b; color: #0f172a; padding: 10px 24px; border-radius: 6px; text-decoration: none; font-weight: 600; margin-top: 12px; }}
.btn:hover {{ background: #d97706; }}
.auto {{ color: #64748b; font-size: 13px; margin-top: 16px; }}
</style></head>
<body>
<h1><span>Wizard of Words</span> Dashboard</h1>
<div class="error">
<p><strong>Build Error:</strong> {error_msg}</p>
<a href="/refresh" class="btn">Try Again</a>
</div>
<p class="auto">Auto-retrying in 30 seconds...</p>
<script>setTimeout(function(){{ window.location.href = '/refresh'; }}, 30000);</script>
</body></html>"""

# ====== FETCH EVENTBRITE DATA ======
def fetch_eb_events():
    """Fetch ALL events (live, started, completed) for the organization."""
    all_events = []

    print("[EB] Fetching live events...", flush=True)
    url = f"https://www.eventbriteapi.com/v3/organizations/{EB_ORG_ID}/events/"
    params = {"status": "live", "expand": "ticket_classes", "token": EB_TOKEN}
    res = _eb_request(url, params)
    live_events = res.json().get("events", [])
    for e in live_events:
        e["_eb_status"] = "live"
    all_events.extend(live_events)
    print(f"[EB] Got {len(live_events)} live events", flush=True)

    # Fetch ENDED events
    time.sleep(0.3)
    url = f"https://www.eventbriteapi.com/v3/organizations/{EB_ORG_ID}/events/"
    params = {"status": "ended", "expand": "ticket_classes", "order_by": "start_desc", "token": EB_TOKEN, "page": 1}
    page_count = 0
    while True:
        params["page"] = page_count + 1
        time.sleep(0.2)
        res = _eb_request(url, params)
        data = res.json()
        ended_events = data.get("events", [])
        if not ended_events:
            break
        for e in ended_events:
            e["_eb_status"] = "ended"
        all_events.extend(ended_events)
        page_count += 1
        if not data.get("pagination", {}).get("has_more_items", False) or len(all_events) >= 60 or page_count >= 2:
            break

    # Fetch COMPLETED events
    time.sleep(0.3)
    url = f"https://www.eventbriteapi.com/v3/organizations/{EB_ORG_ID}/events/"
    params = {"status": "completed", "expand": "ticket_classes", "order_by": "start_desc", "token": EB_TOKEN, "page": 1}
    page_count = 0
    while True:
        params["page"] = page_count + 1
        time.sleep(0.2)
        res = _eb_request(url, params)
        data = res.json()
        completed_events = data.get("events", [])
        if not completed_events:
            break
        for e in completed_events:
            e["_eb_status"] = "completed"
        all_events.extend(completed_events)
        page_count += 1
        if not data.get("pagination", {}).get("has_more_items", False) or len(all_events) >= 60 or page_count >= 2:
            break

    print(f"[EB] Total events: {len(all_events)}", flush=True)
    return all_events

def fetch_eb_orders(event_id, since=None):
    all_orders = []
    page = 1
    while True:
        url = f"https://www.eventbriteapi.com/v3/events/{event_id}/orders/"
        params = {"token": EB_TOKEN, "page": page}
        if since:
            params["changed_since"] = since
        res = _eb_request(url, params)
        data = res.json()
        orders = [o for o in data.get("orders", []) if o.get("status") in ("placed", "completed")]
        all_orders.extend(orders)
        if not data.get("pagination", {}).get("has_more_items"):
            break
        page += 1
        if page > 30:
            break
    return all_orders

def fetch_eb_attendees(event_id):
    all_attendees = []
    page = 1
    while True:
        url = f"https://www.eventbriteapi.com/v3/events/{event_id}/attendees/"
        params = {"token": EB_TOKEN, "page": page, "status": "attending"}
        res = _eb_request(url, params)
        data = res.json()
        all_attendees.extend(data.get("attendees", []))
        if not data.get("pagination", {}).get("has_more_items"):
            break
        page += 1
        if page > 30:
            break
    return all_attendees

# ====== FETCH FACEBOOK DATA ======
def is_relevant_campaign(name):
    """Check if a FB campaign belongs to Wizard of Words or GifterX."""
    lower = name.lower()
    return "wizard of words" in lower or "gifterx" in lower or re.match(r'wow\s+\d+', lower)

def fetch_fb_insights(since_date, until_date):
    global _api_errors
    print(f"[FB] Fetching insights {since_date} to {until_date}...", flush=True)
    all_results = []
    url = f"https://graph.facebook.com/v25.0/act_{FB_AD_ACCOUNT}/insights"
    params = {
        "fields": "campaign_name,campaign_id,spend,impressions,reach,actions",
        "level": "campaign",
        "filtering": json.dumps([{"field": "campaign.name", "operator": "CONTAIN", "value": "Wizard of Words"}]),
        "time_range": json.dumps({"since": since_date, "until": until_date}),
        "limit": 200,
        "access_token": FB_TOKEN
    }
    res = requests.get(url, params=params, timeout=(10, 30))
    if res.status_code != 200:
        try:
            err_data = res.json()
            err_msg = err_data.get("error", {}).get("message", f"HTTP {res.status_code}")
            err_code = err_data.get("error", {}).get("code", "")
        except Exception:
            err_msg = f"HTTP {res.status_code}"
            err_code = ""
        error_str = f"FB Insights API error: {err_msg}"
        if err_code == 190:
            error_str = "Facebook access token has EXPIRED. Please generate a new token in Meta Business Settings and update it in Render environment variables."
        if error_str not in _api_errors:
            _api_errors.append(error_str)
        return []
    data = res.json()
    all_results.extend(data.get("data", []))
    paging = data.get("paging", {})
    while paging.get("next"):
        res = requests.get(paging["next"], timeout=(10, 30))
        if res.status_code != 200:
            break
        data = res.json()
        all_results.extend(data.get("data", []))
        paging = data.get("paging", {})

    # Also fetch WOW campaigns (short format)
    params2 = dict(params)
    params2["filtering"] = json.dumps([{"field": "campaign.name", "operator": "CONTAIN", "value": "WOW"}])
    res2 = requests.get(url, params=params2, timeout=(10, 30))
    if res2.status_code == 200:
        data2 = res2.json()
        all_results.extend(data2.get("data", []))
        paging2 = data2.get("paging", {})
        while paging2.get("next"):
            res2 = requests.get(paging2["next"], timeout=(10, 30))
            if res2.status_code != 200:
                break
            data2 = res2.json()
            all_results.extend(data2.get("data", []))
            paging2 = data2.get("paging", {})

    # Also fetch GifterX campaigns (their names contain neither "Wizard of Words" nor "WOW")
    params3 = dict(params)
    params3["filtering"] = json.dumps([{"field": "campaign.name", "operator": "CONTAIN", "value": "GifterX"}])
    res3 = requests.get(url, params=params3, timeout=(10, 30))
    if res3.status_code == 200:
        data3 = res3.json()
        all_results.extend(data3.get("data", []))
        paging3 = data3.get("paging", {})
        while paging3.get("next"):
            res3 = requests.get(paging3["next"], timeout=(10, 30))
            if res3.status_code != 200:
                break
            data3 = res3.json()
            all_results.extend(data3.get("data", []))
            paging3 = data3.get("paging", {})

    # Dedupe by campaign_id (in case both filters match the same campaign)
    seen = set()
    deduped = []
    for r in all_results:
        cid = r.get("campaign_id", id(r))
        if cid not in seen:
            seen.add(cid)
            deduped.append(r)

    filtered = [r for r in deduped if is_relevant_campaign(r.get("campaign_name", ""))]
    print(f"[FB] Got {len(filtered)} WOW/GX campaigns for {since_date}-{until_date}", flush=True)
    return filtered

# ====== HELPERS ======
def extract_city(event_name):
    """Extract city from Eventbrite event name."""
    # GifterX Talks with number: "GifterX Talks 15 - Miami"
    m = re.match(r'GifterX\s+Talks\s+\d+\s*-\s*(.+)', event_name, re.I)
    if m:
        return m.group(1).strip()

    # GifterX Talks without number: "GifterX Talks Miami- 4th Anniversary..."
    m = re.match(r'GifterX\s+Talks\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)', event_name)
    if m:
        return m.group(1).strip()

    # Wizard of Words with city before colon:
    m = re.match(r'"?Wizard\s+of\s+Words(?:\s+\d+)?"?\s+(.+?):\s', event_name, re.I)
    if m:
        city = m.group(1).strip().strip('"').strip()
        if city and not city.isdigit() and len(city) < 40:
            return city

    # Older format without city: "Wizard of Words 3: Speak to Sell..."
    return event_name

def extract_brand(event_name):
    """Determine if event is WoW or GifterX."""
    if "gifterx" in event_name.lower():
        return "GX"
    return "WoW"

def extract_event_num_from_eb(event_name):
    """Try to get event number directly from EB event name."""
    m = re.search(r'GifterX\s+Talks\s+(\d+)', event_name, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r'Wizard\s+of\s+Words\s+(\d+)', event_name, re.I)
    if m:
        return int(m.group(1))
    return None

def extract_event_num_from_fb(campaign_name):
    """Extract event number from FB campaign name."""
    m = re.search(r'(?:V\d+\s+)?(?:Wizard\s+of\s+Words|GifterX|WOW)\s+(\d+)', campaign_name, re.I)
    return int(m.group(1)) if m else None

def _clean_city(raw):
    """Strip parenthetical suffixes and version tags from an extracted city."""
    s = (raw or "").strip()
    # Drop trailing "(...)" group: "Miami (V2 VOLUME)" -> "Miami"
    s = re.sub(r'\s*\(.*?\)\s*$', '', s)
    # Drop trailing version/volume tags if not in parens
    s = re.sub(r'\s+(V\d+|VOLUME|VIRTUAL)(\s+.*)?$', '', s, flags=re.I)
    return s.strip()

def extract_city_from_fb(campaign_name):
    """Extract city from FB campaign name."""
    # New short format: "WOW 14 Boston - ..."
    m = re.search(r'WOW\s+\d+\s+(.+?)\s*-\s', campaign_name, re.I)
    if m:
        return _clean_city(m.group(1))
    # Original long format
    m = re.search(
        r'(?:V\d+\s+)?(?:Wizard\s+of\s+Words|GifterX)\s+\d+\s+(?:VIRTUAL\s+)?(.+?)\s+(?:[A-Z]{2}\s+)?\d{4}',
        campaign_name
    )
    return _clean_city(m.group(1)) if m else None

def fetch_fb_event_meta():
    """Build event_num → {city, brand} AND city → {num, brand} mappings from FB campaign names."""
    global _api_errors
    url = f"https://graph.facebook.com/v25.0/act_{FB_AD_ACCOUNT}/campaigns"
    params = {"fields": "name,status", "limit": 100, "access_token": FB_TOKEN}
    meta_by_num = {}
    meta_by_city = {}
    page_count = 0
    while True:
        res = requests.get(url, params=params, timeout=(10, 30))
        if res.status_code != 200:
            try:
                err_data = res.json()
                err_msg = err_data.get("error", {}).get("message", f"HTTP {res.status_code}")
                err_code = err_data.get("error", {}).get("code", "")
            except Exception:
                err_msg = f"HTTP {res.status_code}"
                err_code = ""
            error_str = f"FB Campaigns API error: {err_msg}"
            if err_code == 190:
                error_str = "Facebook access token has EXPIRED. Please generate a new token in Meta Business Settings and update it in Render environment variables."
            if error_str not in _api_errors:
                _api_errors.append(error_str)
            break
        data = res.json()
        for c in data.get("data", []):
            name = c.get("name", "")
            if not is_relevant_campaign(name):
                continue
            num = extract_event_num_from_fb(name)
            if not num:
                continue
            city = extract_city_from_fb(name)
            if not city:
                continue
            brand = "GX" if "gifterx" in name.lower() else "WoW"
            norm = normalize_city(city)
            # Brand-qualified key so GX-16 and WoW-16 don't collide
            bk = f"{brand}-{num}"
            if bk not in meta_by_num:
                meta_by_num[bk] = {"city": norm, "brand": brand, "num": num}
            elif c.get("status") == "ACTIVE":
                meta_by_num[bk] = {"city": norm, "brand": brand, "num": num}
            # Key by (brand, city) so WoW Miami and GX Miami can coexist
            ck = (brand, norm)
            if c.get("status") == "ACTIVE":
                meta_by_city[ck] = {"num": num, "brand": brand}
            elif ck not in meta_by_city:
                meta_by_city[ck] = {"num": num, "brand": brand}
        paging = data.get("paging", {})
        if paging.get("next"):
            url = paging["next"]
            params = {}
            page_count += 1
            if page_count >= 5:
                break
        else:
            break
    return meta_by_num, meta_by_city

def _extract_fb_actions(actions_list):
    """Extract purchases and link_clicks from FB actions array."""
    purchases = 0
    link_clicks = 0
    for a in actions_list:
        if a.get("action_type") == "omni_purchase":
            purchases = int(a.get("value", 0))
        if a.get("action_type") == "link_click":
            link_clicks = int(a.get("value", 0))
    return purchases, link_clicks

def build_dashboard_html():
    """Build the full dashboard HTML with live data."""
    global _api_errors
    t0 = time.time()
    _api_errors = []  # Reset errors for this build
    print("[BUILD] Starting dashboard build...", flush=True)

    meta_by_num, meta_by_city = fetch_fb_event_meta()

    # Fallback mapping if FB meta API fails
    known_events_by_num = {
        "WoW-1": {"city": "Miami", "brand": "WoW", "num": 1},
        "WoW-3": {"city": "Orlando", "brand": "WoW", "num": 3},
        "WoW-4": {"city": "Tampa", "brand": "WoW", "num": 4},
        "WoW-5": {"city": "West Palm Beach", "brand": "WoW", "num": 5},
        "WoW-6": {"city": "Jacksonville", "brand": "WoW", "num": 6},
        "WoW-7": {"city": "Fort Lauderdale", "brand": "WoW", "num": 7},
        "WoW-8": {"city": "Atlanta", "brand": "WoW", "num": 8},
        "WoW-9": {"city": "Houston", "brand": "WoW", "num": 9},
        "WoW-10": {"city": "Dallas", "brand": "WoW", "num": 10},
        "WoW-11": {"city": "New York", "brand": "WoW", "num": 11},
        "WoW-12": {"city": "Toronto", "brand": "WoW", "num": 12},
        "WoW-13": {"city": "Washington", "brand": "WoW", "num": 13},
        "WoW-14": {"city": "Boston", "brand": "WoW", "num": 14},
        "WoW-15": {"city": "Chicago", "brand": "WoW", "num": 15},
        "WoW-16": {"city": "Miami", "brand": "WoW", "num": 16},
    }
    known_events_by_city = {
        ("WoW", "Miami"): {"num": 1, "brand": "WoW"},
        ("WoW", "Fort Lauderdale"): {"num": 7, "brand": "WoW"},
        ("WoW", "Orlando"): {"num": 3, "brand": "WoW"},
        ("WoW", "Tampa"): {"num": 4, "brand": "WoW"},
        ("WoW", "West Palm Beach"): {"num": 5, "brand": "WoW"},
        ("WoW", "Jacksonville"): {"num": 6, "brand": "WoW"},
        ("WoW", "Atlanta"): {"num": 8, "brand": "WoW"},
        ("WoW", "Houston"): {"num": 9, "brand": "WoW"},
        ("WoW", "Dallas"): {"num": 10, "brand": "WoW"},
        ("WoW", "New York"): {"num": 11, "brand": "WoW"},
        ("WoW", "Washington"): {"num": 13, "brand": "WoW"},
        ("WoW", "Toronto"): {"num": 12, "brand": "WoW"},
    }
    for num, info in known_events_by_num.items():
        if num not in meta_by_num:
            meta_by_num[num] = info
    for city, info in known_events_by_city.items():
        if city not in meta_by_city:
            meta_by_city[city] = info

    # ===== PHASE 1: Parallel FB calls + Sequential EB calls =====
    AD_ACCOUNT_TZ = ZoneInfo("America/Los_Angeles")
    now = datetime.now(AD_ACCOUNT_TZ)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    from datetime import timezone as _tz
    today_start_utc = today_start.astimezone(_tz.utc)
    periods = {
        "today": today_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "yesterday": (today_start_utc - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last2": (today_start_utc - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last7": (today_start_utc - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last30": (today_start_utc - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "all": "2020-01-01T00:00:00Z"
    }

    fb_date_ranges = {
        "today": (today_start.strftime("%Y-%m-%d"), today_start.strftime("%Y-%m-%d")),
        "yesterday": ((today_start - timedelta(days=1)).strftime("%Y-%m-%d"), (today_start - timedelta(days=1)).strftime("%Y-%m-%d")),
        "last2": ((today_start - timedelta(days=2)).strftime("%Y-%m-%d"), today_start.strftime("%Y-%m-%d")),
        "last7": ((today_start - timedelta(days=7)).strftime("%Y-%m-%d"), today_start.strftime("%Y-%m-%d")),
        "last30": ((today_start - timedelta(days=30)).strftime("%Y-%m-%d"), today_start.strftime("%Y-%m-%d")),
        "all": ("2025-01-01", today_start.strftime("%Y-%m-%d"))
    }

    fb_period_results = {}
    eb_events_result = [[]]

    def _fetch_fb_period(pname, since, until):
        try:
            campaigns = fetch_fb_insights(since, until)
            fb_period_results[pname] = campaigns
        except Exception as e:
            print(f"[BUILD] FB insights {pname} FAILED: {e}", flush=True)
            fb_period_results[pname] = []

    def _fetch_eb():
        try:
            eb_events_result[0] = fetch_eb_events()
        except Exception as e:
            print(f"[BUILD] EB events fetch FAILED: {e}", flush=True)
            eb_events_result[0] = []

    # Phase 1A: All FB calls in parallel (FB API is fast, high rate limits)
    print("[BUILD] Phase 1A: FB calls in parallel...", flush=True)
    _p1_pool = ThreadPoolExecutor(max_workers=4)
    try:
        _p1_futures = []
        for pname, (since, until) in fb_date_ranges.items():
            _p1_futures.append(_p1_pool.submit(_fetch_fb_period, pname, since, until))
        done, not_done = wait(_p1_futures, timeout=60)
        for f in done:
            try:
                f.result()
            except Exception as e:
                print(f"[BUILD] FB task failed: {e}", flush=True)
        if not_done:
            print(f"[BUILD] WARNING: {len(not_done)} FB tasks timed out after 60s, skipping", flush=True)
            for f in not_done:
                f.cancel()
    finally:
        _p1_pool.shutdown(wait=False)

    # Phase 1B: EB calls sequential (strict rate limits)
    print("[BUILD] Phase 1B: EB calls sequential...", flush=True)
    _fetch_eb()

    events = eb_events_result[0]

    # Process events
    all_event_data = []
    all_tickets_flat = []

    for event in events:
        eid = event["id"]
        name = event["name"]["text"]
        city = extract_city(name)
        brand = extract_brand(name)
        capacity = event.get("capacity", 0)
        start_date = event["start"]["local"]
        total_sold = sum(tc.get("quantity_sold", 0) for tc in event.get("ticket_classes", []))
        event_status = event.get("status", "")

        # Only fetch detailed attendee/order data for active events
        if event_status in ("live", "started"):
            attendees = fetch_eb_attendees(eid)
            orders = fetch_eb_orders(eid, since=periods["last30"])
        else:
            attendees = []
            orders = []

        ticket_list = []
        for a in attendees:
            ticket_list.append({
                "created": a["created"],
                "name": a.get("profile", {}).get("name", "Unknown"),
                "order_id": a.get("order_id", ""),
                "ticket_type": a.get("ticket_class_name", ""),
                "city": city
            })
            all_tickets_flat.append({
                "created": a["created"],
                "name": a.get("profile", {}).get("name", "Unknown"),
                "order_id": a.get("order_id", ""),
                "ticket_type": a.get("ticket_class_name", ""),
                "city": city
            })

        order_list = []
        for o in orders:
            cost = o.get("costs", {}).get("gross", {}).get("value", 0) / 100
            order_list.append({
                "created": o["created"],
                "name": o.get("name", "Unknown"),
                "amount": cost,
                "city": city
            })

        # Determine event number
        event_num = extract_event_num_from_eb(name)
        norm_city = normalize_city(city)
        eb_brand = brand
        if event_num is not None:
            # Brand-qualified lookup so GX-16 and WoW-16 don't collide
            bk = f"{eb_brand}-{event_num}"
            meta = meta_by_num.get(bk, {})
            if eb_brand != "GX":
                if meta.get("city"):
                    norm_city = meta["city"]
                if meta.get("brand"):
                    brand = meta["brand"]
        else:
            # Look up by (brand, city) so WoW Miami and GX Miami don't collide
            meta = meta_by_city.get((eb_brand, norm_city), {})
            event_num = meta.get("num", 0)
            if meta.get("brand"):
                brand = meta["brand"]

        display_city = f"{brand} {event_num} – {norm_city}" if event_num else f"{brand} – {city}"

        all_event_data.append({
            "city": norm_city,
            "display_city": display_city,
            "brand": brand,
            "event_num": event_num,
            "event_id": eid,
            "name": name,
            "start_date": start_date,
            "capacity": capacity,
            "total_sold": total_sold,
            "fill_pct": round(total_sold / capacity * 100) if capacity > 0 else 0,
            "tickets": ticket_list,
            "orders": order_list,
            "event_status": event_status
        })

    all_tickets_flat.sort(key=lambda x: x["created"], reverse=True)

    # Aggregate FB data by event for each period
    fb_periods = {}
    for period_name, campaigns in fb_period_results.items():
        fb_by_event = {}
        for c in campaigns:
            cname = c.get("campaign_name", "")
            ev_num = extract_event_num_from_fb(cname)
            if ev_num is None:
                continue
            brand_prefix = "GX" if "gifterx" in cname.lower() else "WoW"
            key = f"{brand_prefix}-{ev_num}"
            spend = float(c.get("spend", 0))
            impressions = int(c.get("impressions", 0))
            reach = int(c.get("reach", 0))
            purchases, link_clicks = _extract_fb_actions(c.get("actions", []))
            if key in fb_by_event:
                fb_by_event[key]["spend"] += spend
                fb_by_event[key]["impressions"] += impressions
                fb_by_event[key]["reach"] += reach
                fb_by_event[key]["purchases"] += purchases
                fb_by_event[key]["link_clicks"] += link_clicks
            else:
                fb_by_event[key] = {"spend": spend, "impressions": impressions, "reach": reach, "purchases": purchases, "link_clicks": link_clicks}
        fb_periods[period_name] = fb_by_event

    # Sort: active first, then completed
    active = [e for e in all_event_data if e["event_status"] in ("live", "started")]
    completed = [e for e in all_event_data if e["event_status"] not in ("live", "started")]
    active.sort(key=lambda x: x["start_date"])
    completed.sort(key=lambda x: x["start_date"], reverse=True)
    all_event_data = active + completed

    # Generate HTML
    events_json = json.dumps(all_event_data)
    tickets_json = json.dumps(all_tickets_flat[:300])
    fb_json = json.dumps(fb_periods)
    generated_time = datetime.now(ET).strftime("%B %d, %Y at %I:%M %p") + " ET"

    # Build API error banner
    api_error_banner = ""
    if _api_errors:
        error_items = "".join(f'<div style="margin:4px 0">&#9888; {e}</div>' for e in _api_errors)
        api_error_banner = f'''<div style="background:rgba(239,68,68,0.15);border:2px solid rgba(239,68,68,0.5);border-radius:12px;padding:16px 24px;margin:16px 32px;color:#fca5a5;font-size:14px;font-weight:500">
            <div style="font-size:16px;font-weight:700;color:#f87171;margin-bottom:8px">&#9888; Facebook Ads Data Unavailable</div>
            {error_items}
            <div style="margin-top:8px;font-size:12px;color:#94a3b8">Amount Spent, Meta Tickets, and Cost/Ticket columns will show $0 or &mdash; until this is resolved.</div>
        </div>'''

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
    <title>Wizard of Words Dashboard</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; }}
        .header {{ background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%); border-bottom: 1px solid #334155; padding: 20px 32px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }}
        .header h1 {{ font-size: 24px; font-weight: 700; color: #f8fafc; }}
        .header h1 span {{ color: #f59e0b; }}
        .generated {{ font-size: 12px; color: #94a3b8; }}
        .refresh-btn {{ padding: 6px 14px; border-radius: 8px; border: 1px solid #334155; background: #1e293b; color: #94a3b8; cursor: pointer; font-size: 12px; transition: all 0.15s; }}
        .refresh-btn:hover {{ border-color: #f59e0b; color: #f59e0b; }}
        .controls {{ padding: 16px 32px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
        .controls label {{ font-size: 12px; color: #94a3b8; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }}
        .dbtn {{ padding: 8px 16px; border-radius: 8px; border: 1px solid #334155; background: #1e293b; color: #e2e8f0; cursor: pointer; font-size: 13px; font-weight: 500; transition: all 0.15s; }}
        .dbtn:hover {{ border-color: #f59e0b; color: #f59e0b; }}
        .dbtn.active {{ background: #f59e0b; color: #0f172a; border-color: #f59e0b; font-weight: 700; }}
        .dinput {{ padding: 6px 10px; border-radius: 8px; border: 1px solid #334155; background: #1e293b; color: #e2e8f0; font-size: 13px; }}
        .dinput:focus {{ border-color: #f59e0b; outline: none; }}
        .tabs {{ display: flex; gap: 0; padding: 0 32px; border-bottom: 1px solid #334155; }}
        .tab {{ padding: 12px 24px; cursor: pointer; font-size: 14px; font-weight: 600; color: #94a3b8; border-bottom: 2px solid transparent; transition: all 0.15s; }}
        .tab:hover {{ color: #f8fafc; }}
        .tab.active {{ color: #f59e0b; border-bottom-color: #f59e0b; }}
        .tpanel {{ display: none; }}
        .tpanel.active {{ display: block; }}
        .cards {{ padding: 20px 32px; display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }}
        .card {{ background: #1e293b; border-radius: 12px; padding: 18px; border: 1px solid #334155; }}
        .card .lb {{ font-size: 11px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }}
        .card .vl {{ font-size: 26px; font-weight: 700; color: #f8fafc; }}
        .card .vl.grn {{ color: #4ade80; }}
        .card .vl.amb {{ color: #f59e0b; }}
        .card .vl.red {{ color: #f87171; }}
        .alerts {{ padding: 0 32px 12px; }}
        .alert {{ padding: 12px 18px; border-radius: 10px; margin-bottom: 6px; font-size: 13px; display: flex; align-items: center; gap: 8px; }}
        .alert-warn {{ background: rgba(248,113,113,0.1); border: 1px solid rgba(248,113,113,0.3); color: #fca5a5; }}
        .alert-info {{ background: rgba(96,165,250,0.1); border: 1px solid rgba(96,165,250,0.3); color: #93c5fd; }}
        .tbl-wrap {{ padding: 16px 32px; overflow-x: auto; }}
        table {{ width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 12px; overflow: hidden; border: 1px solid #334155; }}
        thead th {{ padding: 12px 14px; text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; color: #94a3b8; background: #0f172a; border-bottom: 1px solid #334155; font-weight: 600; white-space: nowrap; }}
        tbody td {{ padding: 12px 14px; font-size: 13px; border-bottom: 1px solid rgba(51,65,85,0.5); }}
        tbody tr:hover {{ background: #334155; }}
        tbody tr.completed-row {{ opacity: 0.6; }}
        .cn {{ font-weight: 600; color: #f8fafc; }}
        .bar {{ width: 100px; height: 7px; background: #334155; border-radius: 4px; overflow: hidden; display: inline-block; vertical-align: middle; margin-right: 6px; }}
        .bar-fill {{ height: 100%; border-radius: 4px; }}
        .bg {{ background: linear-gradient(90deg, #4ade80, #22c55e); }}
        .bb {{ background: linear-gradient(90deg, #60a5fa, #3b82f6); }}
        .ba {{ background: linear-gradient(90deg, #fbbf24, #f59e0b); }}
        .br {{ background: linear-gradient(90deg, #f87171, #ef4444); }}
        .tag {{ display: inline-block; padding: 2px 10px; border-radius: 20px; font-size: 11px; font-weight: 600; }}
        .tag-so {{ background: rgba(74,222,128,0.15); color: #4ade80; }}
        .tag-st {{ background: rgba(96,165,250,0.15); color: #60a5fa; }}
        .tag-mo {{ background: rgba(251,191,36,0.15); color: #fbbf24; }}
        .tag-sl {{ background: rgba(248,113,113,0.15); color: #f87171; }}
        .tag-done {{ background: rgba(148,163,184,0.15); color: #94a3b8; }}
        .oitem {{ display: flex; align-items: center; gap: 14px; padding: 9px 14px; background: #1e293b; border-radius: 8px; border: 1px solid #334155; font-size: 13px; margin-bottom: 4px; }}
        .oitem .oc {{ font-weight: 600; color: #f59e0b; min-width: 110px; }}
        .oitem .ot {{ color: #94a3b8; min-width: 150px; }}
        .oitem .oa {{ color: #4ade80; font-weight: 600; margin-left: auto; }}
        .totrow {{ background: #0f172a !important; font-weight: 700; border-top: 2px solid #f59e0b; }}
        .totrow td {{ color: #f59e0b; }}
        .brand-wow {{ color: #f59e0b; font-size: 10px; font-weight: 700; letter-spacing: 0.5px; }}
        .brand-gx {{ color: #a78bfa; font-size: 10px; font-weight: 700; letter-spacing: 0.5px; }}
        .separator-row td {{ background: #0f172a; padding: 6px 14px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; color: #64748b; border-bottom: 1px solid #334155; }}
    </style>
</head>
<body>
    <div class="header">
        <h1><span>Wizard of Words</span> Dashboard</h1>
        <div style="display:flex;align-items:center;gap:12px">
            <div class="generated">Data pulled: {generated_time}</div>
            <button class="refresh-btn" onclick="window.location.href='/refresh'">&#x21BB; Refresh Data</button>
        </div>
    </div>
    {api_error_banner}
    <div class="controls">
        <label>Period:</label>
        <button class="dbtn active" onclick="setPeriod('last7',this)">Last 7 Days</button>
        <button class="dbtn" onclick="setPeriod('today',this)">Today</button>
        <button class="dbtn" onclick="setPeriod('yesterday',this)">Yesterday</button>
        <button class="dbtn" onclick="setPeriod('last2',this)">Last 2 Days</button>
        <button class="dbtn" onclick="setPeriod('last30',this)">Last 30 Days</button>
        <button class="dbtn" onclick="setPeriod('all',this)">All Time</button>
        <span style="margin-left:12px;border-left:1px solid #334155;padding-left:12px">
            <label>Custom:</label>
            <input type="date" id="customStart" class="dinput" onchange="applyCustomRange()">
            <span style="color:#94a3b8;font-size:12px">to</span>
            <input type="date" id="customEnd" class="dinput" onchange="applyCustomRange()">
        </span>
        <div id="periodLabel" style="width:100%;font-size:13px;color:#94a3b8;margin-top:4px;padding-left:2px"></div>
    </div>
    <div class="tabs">
        <div class="tab active" onclick="showTab('overview',this)">Overview</div>
        <div class="tab" onclick="showTab('combined',this)">Ads + Tickets</div>
        <div class="tab" onclick="showTab('orders',this)">Ticket Sales</div>
    </div>
    <div class="tpanel active" id="p-overview">
        <div class="cards" id="summaryCards"></div>
        <div class="alerts" id="alertBox"></div>
        <div class="tbl-wrap"><table><thead><tr>
            <th>Event</th><th>Event Date</th><th>Days Out</th><th>Amount Spent</th><th>Link Clicks</th><th>Tickets Sold (EB)</th><th>Conv Rate</th><th>Cost/Ticket (EB)</th><th>Tickets Sold (Meta)</th><th>Cost/Ticket (Meta)</th><th>Total Sold</th><th>Capacity</th><th>Fill %</th><th>Period Revenue</th><th>Status</th>
        </tr></thead><tbody id="tblBody"></tbody></table></div>
    </div>
    <div class="tpanel" id="p-combined">
        <div style="padding:20px 32px 0">
            <h2 style="font-size:18px;color:#f8fafc;margin-bottom:4px">Real Tickets (EB) vs Ad Spend (Meta)</h2>
            <p style="font-size:13px;color:#94a3b8;margin-bottom:16px">EB = source of truth for sales (no attribution delay). Meta = source of truth for spend.</p>
        </div>
        <div class="tbl-wrap"><table><thead><tr>
            <th>Event</th><th>Tickets Sold (EB)</th><th>Revenue</th><th>Ad Spend</th><th>Link Clicks</th><th>Conv Rate</th><th>Cost/Ticket (EB)</th><th>ROAS</th><th>Tickets Sold (Meta)</th><th>Impressions</th><th>Reach</th>
        </tr></thead><tbody id="cmbBody"></tbody></table></div>
    </div>
    <div class="tpanel" id="p-orders">
        <div style="padding:20px 32px">
            <h2 style="font-size:18px;color:#f8fafc;margin-bottom:12px">Recent Ticket Sales <span style="font-size:13px;color:#94a3b8">(within selected period)</span></h2>
            <div id="orderList"></div>
        </div>
    </div>

    <script>
    const events = {events_json};
    const allTickets = {tickets_json};
    const fbData = {fb_json};
    let period = 'last7';
    let customFbCache = {{}};

    const periodStarts = {{
        'today': new Date(new Date().setHours(0,0,0,0)),
        'yesterday': new Date(new Date().setHours(0,0,0,0) - 86400000),
        'last2': new Date(new Date().setHours(0,0,0,0) - 2*86400000),
        'last7': new Date(new Date().setHours(0,0,0,0) - 7*86400000),
        'last30': new Date(new Date().setHours(0,0,0,0) - 30*86400000),
        'all': new Date('2020-01-01')
    }};
    const periodEnds = {{
        'today': new Date(new Date().setHours(23,59,59,999)),
        'yesterday': new Date(new Date().setHours(0,0,0,0) - 1),
        'last2': new Date(new Date().setHours(23,59,59,999)),
        'last7': new Date(new Date().setHours(23,59,59,999)),
        'last30': new Date(new Date().setHours(23,59,59,999)),
        'all': new Date(new Date().setHours(23,59,59,999))
    }};

    function formatDate(d) {{
        return d.toLocaleDateString('en-US', {{weekday:'short', month:'long', day:'numeric', year:'numeric'}});
    }}
    function updatePeriodLabel() {{
        const lbl = document.getElementById('periodLabel');
        const now = new Date();
        const todayMid = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        const labels = {{
            'today': `Today: ${{formatDate(todayMid)}}`,
            'yesterday': `Yesterday: ${{formatDate(new Date(todayMid - 86400000))}}`,
            'last2': `Last 2 Days: ${{formatDate(new Date(todayMid - 2*86400000))}} \\u2014 ${{formatDate(todayMid)}}`,
            'last7': `Last 7 Days: ${{formatDate(new Date(todayMid - 7*86400000))}} \\u2014 ${{formatDate(todayMid)}}`,
            'last30': `Last 30 Days: ${{formatDate(new Date(todayMid - 30*86400000))}} \\u2014 ${{formatDate(todayMid)}}`,
            'all': 'All Time'
        }};
        if (period === 'custom') {{
            const s = document.getElementById('customStart').value;
            const e = document.getElementById('customEnd').value;
            if (s && e) lbl.textContent = `Custom Range: ${{formatDate(new Date(s+'T00:00:00'))}} \\u2014 ${{formatDate(new Date(e+'T00:00:00'))}}`;
            else lbl.textContent = 'Select start and end dates';
        }} else {{
            lbl.textContent = labels[period] || '';
        }}
    }}

    function setPeriod(p, el) {{
        period = p;
        document.querySelectorAll('.dbtn').forEach(b=>b.classList.remove('active'));
        if(el) el.classList.add('active');
        if(p !== 'custom') {{
            document.getElementById('customStart').value = '';
            document.getElementById('customEnd').value = '';
        }}
        updatePeriodLabel();
        render();
    }}

    function fetchCustomFb(since, until) {{
        const cacheKey = since + '_' + until;
        if (customFbCache[cacheKey]) return Promise.resolve(customFbCache[cacheKey]);
        return fetch(`/api/fb_custom?since=${{since}}&until=${{until}}`)
            .then(r => r.json())
            .then(data => {{ customFbCache[cacheKey] = data; return data; }});
    }}

    function applyCustomRange() {{
        const s = document.getElementById('customStart').value;
        const e = document.getElementById('customEnd').value;
        if (!s || !e) return;
        document.querySelectorAll('.dbtn').forEach(b=>b.classList.remove('active'));
        periodStarts['custom'] = new Date(s + 'T00:00:00');
        periodEnds['custom'] = new Date(e + 'T23:59:59.999');
        period = 'custom';
        updatePeriodLabel();
        // Fetch FB data for custom range on demand
        fetchCustomFb(s, e).then(data => {{
            fbData['custom'] = data;
            render();
        }});
    }}

    function showTab(t, el) {{
        document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
        document.querySelectorAll('.tpanel').forEach(b=>b.classList.remove('active'));
        el.classList.add('active');
        document.getElementById('p-'+t).classList.add('active');
    }}

    function filterOrders(orders) {{
        const start = periodStarts[period];
        const end = periodEnds[period];
        return orders.filter(o => {{
            const d = new Date(o.created);
            return d >= start && d <= end;
        }});
    }}

    function daysOut(dateStr) {{
        return Math.ceil((new Date(dateStr) - new Date()) / 86400000);
    }}

    function fmt(n) {{ return n.toLocaleString('en-US', {{minimumFractionDigits:2, maximumFractionDigits:2}}); }}

    function barColor(pct) {{
        if(pct>=75) return 'bg';
        if(pct>=40) return 'bb';
        if(pct>=20) return 'ba';
        return 'br';
    }}

    function statusTag(pct, days, evStatus) {{
        if(evStatus === 'completed' || evStatus === 'ended') return '<span class="tag tag-done">Completed</span>';
        if(pct>=95) return '<span class="tag tag-so">Nearly Sold Out</span>';
        if(pct>=60) return '<span class="tag tag-st">Strong</span>';
        if(pct>=30||days>30) return '<span class="tag tag-mo">Moderate</span>';
        return '<span class="tag tag-sl">Needs Attention</span>';
    }}

    function filterTickets(tickets) {{
        const start = periodStarts[period];
        const end = periodEnds[period];
        return tickets.filter(t => {{
            const d = new Date(t.created);
            return d >= start && d <= end;
        }});
    }}

    function getFbForPeriod() {{
        return fbData[period] || {{}};
    }}

    function render() {{
        const fb = getFbForPeriod();
        let totalPeriodTickets=0, totalMetaTickets=0, totalPeriodRev=0, totalSold=0, totalCap=0, totalSpend=0, totalLinkClicks=0, drySpells=0;
        let rows='', cmbRows='', alerts=[];
        let lastSection = '';

        events.forEach(e => {{
            const section = (e.event_status === 'live' || e.event_status === 'started') ? 'active' : 'completed';
            if (section !== lastSection && lastSection !== '') {{
                rows += '<tr class="separator-row"><td colspan="15">&#x2500;&#x2500; Completed Events &#x2500;&#x2500;</td></tr>';
                cmbRows += '<tr class="separator-row"><td colspan="11">&#x2500;&#x2500; Completed Events &#x2500;&#x2500;</td></tr>';
            }}
            lastSection = section;

            const pTickets = filterTickets(e.tickets);
            const pOrders = filterOrders(e.orders);
            const pRev = pOrders.reduce((s,o)=>s+o.amount,0);
            const days = daysOut(e.start_date);
            const d = new Date(e.start_date);
            const dateStr = d.toLocaleDateString('en-US',{{weekday:'short',month:'short',day:'numeric',year:'numeric'}});
            const isCompleted = e.event_status === 'completed' || e.event_status === 'ended';

            const fbKey = e.event_num ? (e.brand + '-' + e.event_num) : null;
            const fbSource = isCompleted ? (fbData['all'] || {{}}) : fb;
            const fbd = fbKey ? (fbSource[fbKey] || null) : null;
            const spend = fbd ? fbd.spend : 0;
            const metaTickets = fbd ? fbd.purchases : 0;
            const linkClicks = fbd ? (fbd.link_clicks || 0) : 0;
            // Period-filtered EB tickets: use attendee data filtered by period.
            // For "all" period or completed events (no attendee data), fall back to total_sold.
            const ebTicketsForCalc = (period === 'all' || isCompleted || e.tickets.length === 0) ? e.total_sold : pTickets.length;
            const overviewCpt = ebTicketsForCalc>0&&spend>0 ? spend/ebTicketsForCalc : 0;
            const metaCpt = metaTickets>0&&spend>0 ? spend/metaTickets : 0;
            const convRate = linkClicks>0 ? (ebTicketsForCalc/linkClicks*100) : 0;

            totalPeriodTickets += ebTicketsForCalc;
            totalMetaTickets += metaTickets;
            totalPeriodRev += pRev;
            totalSold += e.total_sold;
            totalCap += e.capacity;
            totalSpend += spend;
            totalLinkClicks += linkClicks;

            if(!isCompleted && days>2 && e.total_sold>5) {{
                const recent = e.tickets.filter(t=>(new Date()-new Date(t.created))<48*3600000);
                if(recent.length===0) {{ drySpells++; alerts.push({{type:'warn',text:`${{e.city}}: No ticket sales in last 48 hours (${{days}} days out, ${{e.fill_pct}}% full)`}}); }}
            }}
            if(!isCompleted && days<=30 && days>0 && e.fill_pct<30) alerts.push({{type:'warn',text:`${{e.city}}: Only ${{e.fill_pct}}% full with ${{days}} days to go`}});
            if(!isCompleted && e.fill_pct>=90 && e.fill_pct<100) alerts.push({{type:'info',text:`${{e.city}}: ${{e.fill_pct}}% full &#8212; only ${{e.capacity-e.total_sold}} tickets remaining!`}});

            const brandClass = e.brand === 'GX' ? 'brand-gx' : 'brand-wow';
            const ebCptColor = overviewCpt>300?'#f87171':overviewCpt>200?'#fbbf24':overviewCpt>0?'#4ade80':'#94a3b8';
            const metaCptColor = metaCpt>300?'#f87171':metaCpt>200?'#fbbf24':metaCpt>0?'#60a5fa':'#94a3b8';
            const rowClass = isCompleted ? 'completed-row' : '';
            const daysDisplay = isCompleted ? '<span style="color:#94a3b8">Past</span>' : (days>0?days+'d':'<span style="color:#f59e0b">TODAY</span>');
            // Display period-filtered EB tickets. For completed events (no attendee data), dim the all-time number.
            const periodTicketDisplay = isCompleted ? '<span style="color:#94a3b8" title="Showing all-time totals for completed events">'+e.total_sold+'</span>' : ebTicketsForCalc;

            const convRateColor = convRate>=5?'#4ade80':convRate>=2?'#fbbf24':convRate>0?'#f87171':'#94a3b8';
            rows += `<tr class="${{rowClass}}">
                <td class="cn"><span class="${{brandClass}}">${{e.brand}}</span> ${{e.display_city}}</td>
                <td style="color:#94a3b8">${{dateStr}}</td>
                <td>${{daysDisplay}}</td>
                <td style="color:#f59e0b">${{spend>0?'$'+fmt(spend):'$0.00'}}</td>
                <td style="color:#60a5fa">${{linkClicks>0?linkClicks.toLocaleString():'&#8212;'}}</td>
                <td style="font-weight:600;color:#4ade80">${{periodTicketDisplay}}</td>
                <td style="font-weight:600;color:${{convRateColor}}">${{convRate>0?convRate.toFixed(1)+'%':'&#8212;'}}</td>
                <td style="font-weight:600;color:${{ebCptColor}}">${{overviewCpt>0?'$'+fmt(overviewCpt):'&#8212;'}}</td>
                <td style="color:#60a5fa">${{metaTickets}}</td>
                <td style="font-weight:600;color:${{metaCptColor}}">${{metaCpt>0?'$'+fmt(metaCpt):'&#8212;'}}</td>
                <td>${{e.total_sold}}</td>
                <td>${{e.capacity}}</td>
                <td><div class="bar"><div class="bar-fill ${{barColor(e.fill_pct)}}" style="width:${{e.fill_pct}}%"></div></div>${{e.fill_pct}}%</td>
                <td style="color:#4ade80">${{isCompleted?'&#8212;':'$'+fmt(pRev)}}</td>
                <td>${{statusTag(e.fill_pct, days, e.event_status)}}</td>
            </tr>`;

            const fbPurch = fbd ? fbd.purchases : 0;
            const impr = fbd ? fbd.impressions : 0;
            const reach = fbd ? fbd.reach : 0;
            const cpt = ebTicketsForCalc>0&&spend>0 ? spend/ebTicketsForCalc : 0;
            const roas = spend>0 ? pRev/spend : 0;

            const cptColor = cpt>300?'#f87171':cpt>200?'#fbbf24':cpt>0?'#4ade80':'#94a3b8';
            const roasColor = roas>=1?'#4ade80':'#f87171';
            const cmbConvRate = linkClicks>0 ? (ebTicketsForCalc/linkClicks*100) : 0;
            const cmbConvColor = cmbConvRate>=5?'#4ade80':cmbConvRate>=2?'#fbbf24':cmbConvRate>0?'#f87171':'#94a3b8';
            cmbRows += `<tr class="${{rowClass}}">
                <td class="cn"><span class="${{brandClass}}">${{e.brand}}</span> ${{e.display_city}}</td>
                <td style="font-weight:600;color:#4ade80">${{isCompleted ? '<span style="color:#94a3b8">'+e.total_sold+'</span>' : ebTicketsForCalc}}</td>
                <td style="color:#4ade80">${{isCompleted?'&#8212;':'$'+fmt(pRev)}}</td>
                <td style="color:#f59e0b">$${{fmt(spend)}}</td>
                <td style="color:#60a5fa">${{linkClicks>0?linkClicks.toLocaleString():'&#8212;'}}</td>
                <td style="font-weight:600;color:${{cmbConvColor}}">${{cmbConvRate>0?cmbConvRate.toFixed(1)+'%':'&#8212;'}}</td>
                <td style="font-weight:700;color:${{cptColor}}">${{cpt>0?'$'+fmt(cpt):'&#8212;'}}</td>
                <td style="color:${{roasColor}}">${{roas>0?roas.toFixed(2)+'x':'&#8212;'}}</td>
                <td style="color:#94a3b8">${{fbPurch}}</td>
                <td style="color:#94a3b8">${{impr.toLocaleString()}}</td>
                <td style="color:#94a3b8">${{reach.toLocaleString()}}</td>
            </tr>`;
        }});

        const tCpt = totalPeriodTickets>0&&totalSpend>0 ? totalSpend/totalPeriodTickets : 0;
        const tRoas = totalSpend>0 ? totalPeriodRev/totalSpend : 0;
        const tConvRate = totalLinkClicks>0 ? (totalPeriodTickets/totalLinkClicks*100) : 0;
        const tConvColor = tConvRate>=5?'#4ade80':tConvRate>=2?'#fbbf24':tConvRate>0?'#f87171':'#94a3b8';
        cmbRows += `<tr class="totrow">
            <td>TOTALS</td>
            <td style="color:#4ade80">${{totalPeriodTickets}}</td>
            <td style="color:#4ade80">$${{fmt(totalPeriodRev)}}</td>
            <td>$${{fmt(totalSpend)}}</td>
            <td style="color:#60a5fa">${{totalLinkClicks>0?totalLinkClicks.toLocaleString():'&#8212;'}}</td>
            <td style="font-weight:600;color:${{tConvColor}}">${{tConvRate>0?tConvRate.toFixed(1)+'%':'&#8212;'}}</td>
            <td style="color:${{tCpt>300?'#f87171':tCpt>200?'#fbbf24':'#4ade80'}}">${{tCpt>0?'$'+fmt(tCpt):'&#8212;'}}</td>
            <td style="color:${{tRoas>=1?'#4ade80':'#f87171'}}">${{tRoas>0?tRoas.toFixed(2)+'x':'&#8212;'}}</td>
            <td></td><td></td><td></td>
        </tr>`;

        document.getElementById('tblBody').innerHTML = rows;
        document.getElementById('cmbBody').innerHTML = cmbRows;

        const fillPct = totalCap>0?Math.round(totalSold/totalCap*100):0;
        const avgCpt = totalPeriodTickets>0&&totalSpend>0 ? totalSpend/totalPeriodTickets : 0;
        document.getElementById('summaryCards').innerHTML = `
            <div class="card"><div class="lb">Ticket Sales (Period)</div><div class="vl">${{totalPeriodTickets}}</div></div>
            <div class="card"><div class="lb">Period Revenue</div><div class="vl grn">$${{fmt(totalPeriodRev)}}</div></div>
            <div class="card"><div class="lb">Period Ad Spend</div><div class="vl amb">$${{fmt(totalSpend)}}</div></div>
            <div class="card"><div class="lb">Avg Cost/Ticket</div><div class="vl ${{avgCpt>300?'red':avgCpt>200?'amb':'grn'}}">${{avgCpt>0?'$'+fmt(avgCpt):'&#8212;'}}</div></div>
            <div class="card"><div class="lb">Total Sold (All Time)</div><div class="vl">${{totalSold}} / ${{totalCap}}</div></div>
            <div class="card"><div class="lb">Overall Fill Rate</div><div class="vl ${{fillPct>50?'grn':'amb'}}">${{fillPct}}%</div></div>
            <div class="card"><div class="lb">Dry Spell Alerts</div><div class="vl ${{drySpells>0?'red':'grn'}}">${{drySpells>0?drySpells+' cities':'None'}}</div></div>
        `;

        document.getElementById('alertBox').innerHTML = alerts.map(a=>`<div class="alert alert-${{a.type}}">${{a.type==='warn'?'\\u26A0':'\\u2139'}} ${{a.text}}</div>`).join('');

        const pTicketsAll = filterTickets(allTickets);
        if(pTicketsAll.length===0) {{
            document.getElementById('orderList').innerHTML = '<div style="text-align:center;padding:40px;color:#94a3b8">No ticket sales in selected period</div>';
        }} else {{
            document.getElementById('orderList').innerHTML = pTicketsAll.slice(0,150).map(t => {{
                const d = new Date(t.created);
                return `<div class="oitem">
                    <div class="oc">${{t.city}}</div>
                    <div class="ot">${{d.toLocaleDateString('en-US',{{weekday:'short',month:'short',day:'numeric'}})}} at ${{d.toLocaleTimeString('en-US',{{hour:'numeric',minute:'2-digit'}})}}</div>
                    <div>${{t.name}}</div>
                    <div style="color:#94a3b8;font-size:12px;margin-left:auto">${{t.ticket_type}}</div>
                </div>`;
            }}).join('');
        }}
    }}

    render();
    updatePeriodLabel();
    </script>
</body>
</html>"""
    print(f"[BUILD] Dashboard build complete in {time.time()-t0:.1f}s", flush=True)
    return html

# ====== LOADING & REFRESH SCREENS ======
LOADING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Wizard of Words Dashboard — Loading</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; }
        h1 { font-size: 28px; font-weight: 700; margin-bottom: 8px; }
        h1 span { color: #f59e0b; }
        .sub { color: #94a3b8; font-size: 15px; margin-bottom: 32px; }
        .spinner { width: 48px; height: 48px; border: 4px solid #334155; border-top-color: #f59e0b; border-radius: 50%; animation: spin 0.8s linear infinite; margin: 0 auto 24px; }
        @keyframes spin { to { transform: rotate(360deg); } }
        .status { color: #64748b; font-size: 13px; }
        .dot { animation: blink 1.4s infinite; }
        .dot:nth-child(2) { animation-delay: 0.2s; }
        .dot:nth-child(3) { animation-delay: 0.4s; }
        @keyframes blink { 0%,80%,100% { opacity: 0; } 40% { opacity: 1; } }
    </style>
</head>
<body>
    <div style="text-align:center">
        <div class="spinner"></div>
        <h1><span>Wizard of Words</span> Dashboard</h1>
        <p class="sub">Pulling live data from Eventbrite &amp; Facebook Ads</p>
        <p class="status">Loading fresh data<span class="dot">.</span><span class="dot">.</span><span class="dot">.</span></p>
    </div>
    <script>
        (function poll() {
            fetch('/api/status').then(r => r.json()).then(d => {
                if (d.ready) { window.location.reload(); }
                else { setTimeout(poll, 2000); }
            }).catch(() => setTimeout(poll, 3000));
        })();
    </script>
</body>
</html>"""

REFRESH_WAIT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Wizard of Words Dashboard — Refreshing</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; }
        h1 { font-size: 28px; font-weight: 700; margin-bottom: 8px; }
        h1 span { color: #f59e0b; }
        .sub { color: #94a3b8; font-size: 15px; margin-bottom: 32px; }
        .spinner { width: 48px; height: 48px; border: 4px solid #334155; border-top-color: #f59e0b; border-radius: 50%; animation: spin 0.8s linear infinite; margin: 0 auto 24px; }
        @keyframes spin { to { transform: rotate(360deg); } }
        .status { color: #64748b; font-size: 13px; }
        .elapsed { color: #475569; font-size: 12px; margin-top: 12px; }
        .dot { animation: blink 1.4s infinite; }
        .dot:nth-child(2) { animation-delay: 0.2s; }
        .dot:nth-child(3) { animation-delay: 0.4s; }
        @keyframes blink { 0%,80%,100% { opacity: 0; } 40% { opacity: 1; } }
    </style>
</head>
<body>
    <div style="text-align:center">
        <div class="spinner"></div>
        <h1><span>Wizard of Words</span> Dashboard</h1>
        <p class="sub">Refreshing live data from Eventbrite &amp; Facebook Ads</p>
        <p class="status">Pulling fresh numbers<span class="dot">.</span><span class="dot">.</span><span class="dot">.</span></p>
        <p class="elapsed" id="timer"></p>
    </div>
    <script>
        var start = Date.now();
        var timerEl = document.getElementById('timer');
        setInterval(function() {
            var s = Math.floor((Date.now() - start) / 1000);
            timerEl.textContent = s + 's elapsed — usually takes about 90 seconds';
        }, 1000);
        (function poll() {
            fetch('/build_status').then(function(r) { return r.json(); }).then(function(d) {
                if (d.done) { window.location.href = '/'; }
                else { setTimeout(poll, 3000); }
            }).catch(function() { setTimeout(poll, 4000); });
        })();
    </script>
</body>
</html>"""

# ====== ROUTES ======
@app.route("/api/status")
def api_status():
    if _cache["building"]:
        build_start = _cache.get("build_start", 0)
        if build_start and (time.time() - build_start) > BUILD_TIMEOUT:
            _cache["building"] = False
            _cache["html"] = _build_error_html("Dashboard build timed out. Try /refresh in a minute.")
            _cache["time"] = time.time()
    ready = _cache["html"] is not None and (time.time() - _cache["time"]) < CACHE_TTL
    return jsonify({"ready": ready, "building": _cache["building"]})

@app.route("/api/fb_custom")
def api_fb_custom():
    """On-demand FB insights for custom date ranges."""
    since = request.args.get("since")
    until = request.args.get("until")
    if not since or not until:
        return jsonify({"error": "Missing since/until params"}), 400
    campaigns = fetch_fb_insights(since, until)
    fb_by_event = {}
    for c in campaigns:
        cname = c.get("campaign_name", "")
        ev_num = extract_event_num_from_fb(cname)
        if ev_num is None:
            continue
        brand_prefix = "GX" if "gifterx" in cname.lower() else "WoW"
        key = f"{brand_prefix}-{ev_num}"
        spend = float(c.get("spend", 0))
        impressions = int(c.get("impressions", 0))
        reach = int(c.get("reach", 0))
        purchases, link_clicks = _extract_fb_actions(c.get("actions", []))
        if key in fb_by_event:
            fb_by_event[key]["spend"] += spend
            fb_by_event[key]["impressions"] += impressions
            fb_by_event[key]["reach"] += reach
            fb_by_event[key]["purchases"] += purchases
            fb_by_event[key]["link_clicks"] += link_clicks
        else:
            fb_by_event[key] = {"spend": spend, "impressions": impressions, "reach": reach, "purchases": purchases, "link_clicks": link_clicks}
    return jsonify(fb_by_event)

@app.route("/")
def dashboard():
    _ensure_cache()
    if _cache["html"]:
        return Response(_cache["html"], content_type="text/html; charset=utf-8")
    return Response(LOADING_HTML, content_type="text/html; charset=utf-8")

@app.route("/refresh")
def refresh():
    """Force refresh the cache. Shows spinner while rebuilding."""
    _cache["time"] = 0
    build_thread = _cache.get("build_thread")
    if build_thread and not build_thread.is_alive():
        _cache["building"] = False
        try:
            _build_lock.release()
        except RuntimeError:
            pass
    _ensure_cache()
    return Response(REFRESH_WAIT_HTML, content_type="text/html; charset=utf-8")

@app.route("/build_status")
def build_status():
    build_start = _cache.get("build_start", 0)
    cache_time = _cache.get("time", 0)
    fresh = cache_time > build_start and len(_cache["html"] or "") > 2000
    return jsonify({
        "building": _cache["building"],
        "done": fresh,
        "elapsed": round(time.time() - build_start) if build_start else 0,
    })

@app.route("/cache_state")
def cache_state():
    """Debug endpoint to check raw cache state."""
    disk_exists = os.path.exists(CACHE_FILE)
    disk_size = os.path.getsize(CACHE_FILE) if disk_exists else 0
    build_thread = _cache.get("build_thread")
    return jsonify({
        "building": _cache["building"],
        "html_exists": _cache["html"] is not None,
        "html_len": len(_cache["html"]) if _cache["html"] else 0,
        "cache_time": _cache["time"],
        "build_start": _cache.get("build_start", 0),
        "age_seconds": time.time() - _cache["time"] if _cache["time"] else None,
        "build_elapsed": time.time() - _cache.get("build_start", 0) if _cache.get("build_start") else None,
        "build_thread_alive": build_thread.is_alive() if build_thread else None,
        "ttl": CACHE_TTL,
        "disk_cache_exists": disk_exists,
        "disk_cache_size": disk_size,
        "active_threads": [t.name for t in threading.enumerate()],
        "thread_count": threading.active_count(),
        "now": time.time()
    })

@app.route("/debug")
def debug():
    results = {"timestamp": datetime.now(ET).isoformat(), "checks": {}}
    try:
        url = f"https://www.eventbriteapi.com/v3/organizations/{EB_ORG_ID}/events/"
        params = {"status": "live", "token": EB_TOKEN}
        res = requests.get(url, params=params, timeout=10)
        if res.status_code == 200:
            count = len(res.json().get("events", []))
            results["checks"]["eventbrite"] = {"status": "OK", "events_found": count}
        else:
            results["checks"]["eventbrite"] = {"status": "ERROR", "http_code": res.status_code, "response": res.text[:500]}
    except Exception as e:
        results["checks"]["eventbrite"] = {"status": "ERROR", "message": str(e)}
    try:
        url = f"https://graph.facebook.com/v25.0/act_{FB_AD_ACCOUNT}/campaigns"
        params = {"fields": "name", "limit": 1, "access_token": FB_TOKEN}
        res = requests.get(url, params=params, timeout=10)
        if res.status_code == 200:
            results["checks"]["facebook"] = {"status": "OK", "sample": res.json().get("data", [])[:1]}
        else:
            err = res.json() if res.headers.get("content-type", "").startswith("application/json") else {"raw": res.text[:500]}
            results["checks"]["facebook"] = {"status": "ERROR", "http_code": res.status_code, "error": err}
    except Exception as e:
        results["checks"]["facebook"] = {"status": "ERROR", "message": str(e)}
    results["config"] = {
        "EB_TOKEN": ("set (" + EB_TOKEN[:6] + "...)" ) if EB_TOKEN else "NOT SET",
        "EB_ORG_ID": EB_ORG_ID if EB_ORG_ID else "NOT SET",
        "FB_TOKEN": ("set (" + FB_TOKEN[:6] + "...)" ) if FB_TOKEN else "NOT SET",
        "FB_AD_ACCOUNT": FB_AD_ACCOUNT if FB_AD_ACCOUNT else "NOT SET",
    }
    results["recent_errors"] = _api_errors
    return Response(json.dumps(results, indent=2), content_type="application/json")

@app.route("/debug/events")
def debug_events():
    try:
        all_events = []
        page = 1
        while page <= 10:
            url = f"https://www.eventbriteapi.com/v3/organizations/{EB_ORG_ID}/events/"
            params = {"status": "all", "token": EB_TOKEN, "page": page}
            res = requests.get(url, params=params, timeout=15)
            res.raise_for_status()
            data = res.json()
            for e in data.get("events", []):
                all_events.append({
                    "id": e.get("id"),
                    "name": e.get("name", {}).get("text", ""),
                    "status": e.get("status"),
                    "start": e.get("start", {}).get("local", ""),
                    "end": e.get("end", {}).get("local", ""),
                    "capacity": e.get("capacity"),
                })
            if not data.get("pagination", {}).get("has_more_items"):
                break
            page += 1
        return Response(json.dumps({"total": len(all_events), "events": all_events}, indent=2), content_type="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), content_type="application/json"), 500

@app.route("/debug/processed")
def debug_processed():
    try:
        meta_by_num, meta_by_city = fetch_fb_event_meta()
        events = fetch_eb_events()
        result = []
        for event in events:
            name = event["name"]["text"]
            city = extract_city(name)
            brand = extract_brand(name)
            event_num = extract_event_num_from_eb(name)
            norm_city = normalize_city(city)
            num_source = "eb_name" if event_num is not None else "meta_by_city"
            eb_brand = brand
            if event_num is not None:
                bk = f"{eb_brand}-{event_num}"
                meta = meta_by_num.get(bk, {})
                if eb_brand != "GX":
                    if meta.get("brand"):
                        brand = meta["brand"]
            else:
                meta = meta_by_city.get((eb_brand, norm_city), {})
                meta_brand = meta.get("brand", eb_brand)
                if meta_brand == eb_brand:
                    event_num = meta.get("num", 0)
                    if meta.get("brand"):
                        brand = meta["brand"]
                else:
                    event_num = 0
            result.append({
                "eb_name": name,
                "eb_status": event.get("status", ""),
                "start_date": event["start"]["local"],
                "extracted_city": city,
                "norm_city": norm_city,
                "event_num": event_num,
                "num_source": num_source,
                "brand": brand,
                "fb_key": f"{brand}-{event_num}" if event_num else None,
            })
        # JSON can't serialize tuple keys — flatten "(brand, city)" to "brand | city" for display
        meta_by_city_display = {f"{k[0]} | {k[1]}" if isinstance(k, tuple) else k: v for k, v in meta_by_city.items()}
        return Response(json.dumps({"meta_by_num": meta_by_num, "meta_by_city": meta_by_city_display, "events": result}, indent=2), content_type="application/json")
    except Exception as e:
        import traceback
        return Response(json.dumps({"error": str(e), "traceback": traceback.format_exc()}), content_type="application/json"), 500

# ====== KEEP-ALIVE SELF-PING ======
def _keep_alive():
    """Ping our own /api/status endpoint to keep the server warm."""
    import urllib.request
    url = os.environ.get("RENDER_EXTERNAL_URL", "https://wizard-of-words-dashboard.onrender.com")
    if not url:
        print("[KEEPALIVE] No RENDER_EXTERNAL_URL set, skipping keep-alive", flush=True)
        return
    ping_url = f"{url}/api/status"
    print(f"[KEEPALIVE] Starting keep-alive loop, pinging {ping_url} every 8 min", flush=True)
    while True:
        time.sleep(480)  # 8 minutes
        try:
            urllib.request.urlopen(ping_url, timeout=10)
        except Exception:
            pass

# ====== STARTUP ======
print(f"[STARTUP] Checking disk cache at {CACHE_FILE}...", flush=True)
_disk_html, _disk_time = _load_cache_from_disk()
if _disk_html:
    _cache["html"] = _disk_html
    _cache["time"] = _disk_time
    print(f"[STARTUP] Loaded cache from disk ({len(_disk_html)} bytes, {time.time()-_disk_time:.0f}s old)", flush=True)
else:
    print(f"[STARTUP] No valid disk cache found — first request will see loading page until build completes", flush=True)

# Start background data build on import
_ensure_cache()

# Start keep-alive thread
_ka = threading.Thread(target=_keep_alive, daemon=True)
_ka.start()

# Start proactive refresh loop
_pr = threading.Thread(target=_proactive_refresh_loop, daemon=True)
_pr.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
