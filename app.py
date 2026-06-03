#!/usr/bin/env python3
"""
Wizard of Words Dashboard — live-API edition.

Covers both "Wizard of Words" and "GifterX Talks" events for Christopher Kai.

Live-API pattern: static HTML shell at /, JSON endpoints fetched on demand by
the browser. No batch build, no threading, no disk cache. If an upstream API
hiccups, the page still renders.

Hardening (matches the client-dashboard-builder skill's Layer 3 defenses):
  - In-memory cache per endpoint, configurable TTL
  - Per-call timeout + retry on 429/5xx (Eventbrite has strict rate limits)
  - Deep /api/health that probes both upstreams and classifies errors
  - Startup token validation (loud ████ banner in logs if a key is dead)
  - Never-crash error handlers (HTTPException passthrough — 404s stay 404s)
  - Structured JSON logging
"""
import os
import re
import json
import time
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

# ─── Config ──────────────────────────────────────────────────────────────────
APP_VERSION = "v3.0-live-api"
ET = ZoneInfo("America/New_York")
AD_ACCOUNT_TZ = ZoneInfo("America/Los_Angeles")  # CK's ad account is in LA tz

EB_TOKEN = os.environ.get("EB_TOKEN", "")
EB_ORG_ID = os.environ.get("EB_ORG_ID", "")
FB_TOKEN = os.environ.get("FB_TOKEN", "")
FB_AD_ACCOUNT = os.environ.get("FB_AD_ACCOUNT", "")

CACHE_TTL = int(os.environ.get("CACHE_TTL_SECONDS", "60"))
STALE_THRESHOLD = int(os.environ.get("STALE_THRESHOLD_SECONDS", "300"))
DATA_CACHE_TTL = int(os.environ.get("DATA_CACHE_SECONDS", "300"))  # /api/data expensive — cache 5m

BRAND_NAME = "Wizard of Words"

app = Flask(__name__, static_folder="static", static_url_path="/static")

# ─── Structured logging ──────────────────────────────────────────────────────
def log(level, msg, **extra):
    try:
        print(json.dumps({"ts": datetime.utcnow().isoformat() + "Z", "level": level, "msg": msg, **extra}), flush=True)
    except Exception:
        print(f"[{level}] {msg}", flush=True)

# ─── In-memory cache ─────────────────────────────────────────────────────────
_cache: dict = {}

def cache_get(key):
    entry = _cache.get(key)
    if not entry:
        return None
    if time.time() - entry["ts"] > entry["ttl"]:
        _cache.pop(key, None)
        return None
    return entry["value"]

def cache_set(key, value, ttl=CACHE_TTL):
    _cache[key] = {"value": value, "ts": time.time(), "ttl": ttl}

# ─── Health state ────────────────────────────────────────────────────────────
state = {
    "eb": {"last_success_at": None, "last_error": None},
    "fb": {"last_success_at": None, "last_error": None},
}

def classify_error(msg: str) -> str:
    m = (msg or "").lower()
    if "expired" in m or "invalid oauth" in m or "code 190" in m: return "TOKEN_EXPIRED"
    if "429" in m or "rate limit" in m or "throttle" in m: return "RATE_LIMITED"
    if "timeout" in m or "timed out" in m: return "TIMEOUT"
    if " 403" in m or "forbidden" in m: return "FORBIDDEN"
    if " 401" in m or "unauthorized" in m: return "UNAUTHORIZED"
    return "GENERIC"

def is_stale(ts):
    return bool(ts) and (time.time() - ts) > STALE_THRESHOLD

# ─── HTTP helpers ────────────────────────────────────────────────────────────
def _eb_request(url, params, timeout=(10, 30), max_retries=3):
    """Eventbrite request with bounded retry on 429. Total worst-case time
    is ~45s (3 attempts × 30s read-timeout + small backoff)."""
    last_err = None
    for attempt in range(max_retries):
        try:
            res = requests.get(url, params=params, timeout=timeout)
            if res.status_code == 429:
                wait = min(2 ** attempt + 1, 8)  # 2s, 3s, 5s — bounded
                log("warn", "eb_429", attempt=attempt + 1, wait_s=wait)
                time.sleep(wait)
                continue
            res.raise_for_status()
            return res
        except Exception as e:
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(min(2 ** attempt, 4))
                continue
    raise last_err if last_err else Exception("eb_request exhausted retries")

def _fb_get(url, params=None, timeout=(10, 30), retries=2):
    """Facebook Graph request with timeout + retry on 5xx."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            res = requests.get(url, params=params, timeout=timeout)
            if res.status_code >= 500 and attempt < retries:
                time.sleep(0.5 * (2 ** attempt))
                continue
            return res
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.5 * (2 ** attempt))
                continue
    raise last_err if last_err else Exception("fb_get exhausted retries")

# ─── WoW-specific helpers (preserved verbatim from the previous build) ───────
CITY_NORMALIZE = {
    "new york city": "New York",
    "st. pete's/tampa": "Tampa",
    "st pete's/tampa": "Tampa",
    "washington dc": "Washington",
}

def normalize_city(city):
    c = (city or "").strip()
    c = re.sub(r',\s*[A-Z]{2}$', '', c)
    lower = c.lower().strip()
    return CITY_NORMALIZE.get(lower, c)

def extract_city(event_name):
    """Extract city from Eventbrite event name."""
    # GifterX Talks with number: "GifterX Talks 15 - Miami"
    m = re.match(r'GifterX\s+Talks\s+\d+\s*-\s*(.+)', event_name, re.I)
    if m: return m.group(1).strip()
    # GifterX Talks without number: "GifterX Talks Miami- 4th Anniversary"
    m = re.match(r'GifterX\s+Talks\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)', event_name)
    if m: return m.group(1).strip()
    # Wizard of Words with city before colon
    m = re.match(r'"?Wizard\s+of\s+Words(?:\s+\d+)?"?\s+(.+?):\s', event_name, re.I)
    if m:
        city = m.group(1).strip().strip('"').strip()
        if city and not city.isdigit() and len(city) < 40:
            return city
    return event_name

def extract_brand(event_name):
    return "GX" if "gifterx" in (event_name or "").lower() else "WoW"

def extract_event_num_from_eb(event_name):
    m = re.search(r'GifterX\s+Talks\s+(\d+)', event_name, re.I)
    if m: return int(m.group(1))
    m = re.search(r'Wizard\s+of\s+Words\s+(\d+)', event_name, re.I)
    if m: return int(m.group(1))
    return None

def extract_event_num_from_fb(campaign_name):
    m = re.search(r'(?:V\d+\s+)?(?:Wizard\s+of\s+Words|GifterX|WOW)\s+(\d+)', campaign_name, re.I)
    return int(m.group(1)) if m else None

def _clean_city(raw):
    s = (raw or "").strip()
    s = re.sub(r'\s*\(.*?\)\s*$', '', s)
    s = re.sub(r'\s+(V\d+|VOLUME|VIRTUAL)(\s+.*)?$', '', s, flags=re.I)
    return s.strip()

def extract_city_from_fb(campaign_name):
    m = re.search(r'WOW\s+\d+\s+(.+?)\s*-\s', campaign_name, re.I)
    if m: return _clean_city(m.group(1))
    m = re.search(
        r'(?:V\d+\s+)?(?:Wizard\s+of\s+Words|GifterX)\s+\d+\s+(?:VIRTUAL\s+)?(.+?)\s+(?:[A-Z]{2}\s+)?\d{4}',
        campaign_name
    )
    return _clean_city(m.group(1)) if m else None

def is_relevant_campaign(name):
    lower = (name or "").lower()
    return "wizard of words" in lower or "gifterx" in lower or bool(re.match(r'wow\s+\d+', lower))

def _extract_fb_actions(actions_list):
    purchases, link_clicks = 0, 0
    for a in actions_list or []:
        if a.get("action_type") == "omni_purchase":
            purchases = int(float(a.get("value", 0) or 0))
        if a.get("action_type") == "link_click":
            link_clicks = int(float(a.get("value", 0) or 0))
    return purchases, link_clicks

# ─── Eventbrite fetches ──────────────────────────────────────────────────────
def fetch_eb_events():
    all_events = []
    base = f"https://www.eventbriteapi.com/v3/organizations/{EB_ORG_ID}/events/"

    # Live
    res = _eb_request(base, {"status": "live", "expand": "ticket_classes", "token": EB_TOKEN})
    live = res.json().get("events", [])
    for e in live: e["_eb_status"] = "live"
    all_events.extend(live)
    log("info", "eb_live_events", count=len(live))

    for status in ["ended", "completed"]:
        page = 1
        page_count = 0
        while True:
            res = _eb_request(base, {
                "status": status, "expand": "ticket_classes",
                "order_by": "start_desc", "token": EB_TOKEN, "page": page,
            })
            data = res.json()
            evs = data.get("events", [])
            if not evs: break
            for e in evs: e["_eb_status"] = status
            all_events.extend(evs)
            page += 1
            page_count += 1
            if not data.get("pagination", {}).get("has_more_items"): break
            if len(all_events) >= 60 or page_count >= 2: break
            time.sleep(0.2)

    log("info", "eb_total_events", count=len(all_events))
    return all_events

def fetch_eb_orders(event_id, since=None):
    all_orders = []
    page = 1
    while True:
        params = {"token": EB_TOKEN, "page": page}
        if since: params["changed_since"] = since
        res = _eb_request(f"https://www.eventbriteapi.com/v3/events/{event_id}/orders/", params)
        data = res.json()
        all_orders.extend([o for o in data.get("orders", []) if o.get("status") in ("placed", "completed")])
        if not data.get("pagination", {}).get("has_more_items"): break
        page += 1
        if page > 30: break
    return all_orders

def fetch_eb_attendees(event_id):
    all_attendees = []
    page = 1
    while True:
        res = _eb_request(f"https://www.eventbriteapi.com/v3/events/{event_id}/attendees/",
                          {"token": EB_TOKEN, "page": page, "status": "attending"})
        data = res.json()
        all_attendees.extend(data.get("attendees", []))
        if not data.get("pagination", {}).get("has_more_items"): break
        page += 1
        if page > 30: break
    return all_attendees

# ─── Facebook fetches (three filter passes + dedup) ──────────────────────────
def fetch_fb_insights(since_date, until_date):
    url = f"https://graph.facebook.com/v25.0/act_{FB_AD_ACCOUNT}/insights"
    base_params = {
        "fields": "campaign_name,campaign_id,spend,impressions,reach,actions",
        "level": "campaign",
        "time_range": json.dumps({"since": since_date, "until": until_date}),
        "limit": 200,
        "access_token": FB_TOKEN,
    }
    all_results = []
    errors_first = None

    for filt_value in ["Wizard of Words", "WOW", "GifterX"]:
        params = dict(base_params)
        params["filtering"] = json.dumps([{"field": "campaign.name", "operator": "CONTAIN", "value": filt_value}])
        res = _fb_get(url, params)
        if res.status_code != 200:
            body = res.json() if res.headers.get("content-type","").startswith("application/json") else {}
            err = body.get("error", {})
            if err.get("code") == 190:
                # Token expired — fail fast on first pass
                raise Exception(f"FB token expired: {err.get('message', f'HTTP {res.status_code}')}")
            if errors_first is None:
                errors_first = f"FB insights ({filt_value}): {err.get('message', f'HTTP {res.status_code}')}"
            continue
        data = res.json()
        all_results.extend(data.get("data", []))
        paging = data.get("paging", {})
        while paging.get("next"):
            r2 = _fb_get(paging["next"])
            if r2.status_code != 200: break
            d2 = r2.json()
            all_results.extend(d2.get("data", []))
            paging = d2.get("paging", {})

    if not all_results and errors_first:
        # If all three filter passes failed, raise so the caller knows
        raise Exception(errors_first)

    # Dedupe by campaign_id (filters may overlap)
    seen, deduped = set(), []
    for r in all_results:
        cid = r.get("campaign_id", id(r))
        if cid not in seen:
            seen.add(cid)
            deduped.append(r)

    return [r for r in deduped if is_relevant_campaign(r.get("campaign_name", ""))]

def fetch_fb_event_meta():
    """Build event_num → {city, brand} AND city → {num, brand} mappings."""
    url = f"https://graph.facebook.com/v25.0/act_{FB_AD_ACCOUNT}/campaigns"
    params = {"fields": "name,status", "limit": 100, "access_token": FB_TOKEN}
    meta_by_num = {}
    meta_by_city = {}
    page_count = 0
    while True:
        res = _fb_get(url, params=params)
        if res.status_code != 200:
            body = res.json() if res.headers.get("content-type","").startswith("application/json") else {}
            err = body.get("error", {})
            raise Exception(f"FB campaigns: {err.get('message', f'HTTP {res.status_code}')}")
        data = res.json()
        for c in data.get("data", []):
            name = c.get("name", "")
            if not is_relevant_campaign(name): continue
            num = extract_event_num_from_fb(name)
            if not num: continue
            city = extract_city_from_fb(name)
            if not city: continue
            brand = "GX" if "gifterx" in name.lower() else "WoW"
            norm = normalize_city(city)
            bk = f"{brand}-{num}"
            if bk not in meta_by_num:
                meta_by_num[bk] = {"city": norm, "brand": brand, "num": num}
            elif c.get("status") == "ACTIVE":
                meta_by_num[bk] = {"city": norm, "brand": brand, "num": num}
            ck = (brand, norm)
            if c.get("status") == "ACTIVE":
                meta_by_city[ck] = {"num": num, "brand": brand}
            elif ck not in meta_by_city:
                meta_by_city[ck] = {"num": num, "brand": brand}
        paging = data.get("paging", {})
        if not paging.get("next"): break
        url = paging["next"]; params = {}
        page_count += 1
        if page_count >= 5: break

    # Fallback known events when FB API has no data
    known_events_by_num = {
        "WoW-1":  {"city": "Miami",              "brand": "WoW", "num": 1},
        "WoW-3":  {"city": "Orlando",            "brand": "WoW", "num": 3},
        "WoW-4":  {"city": "Tampa",              "brand": "WoW", "num": 4},
        "WoW-5":  {"city": "West Palm Beach",    "brand": "WoW", "num": 5},
        "WoW-6":  {"city": "Jacksonville",       "brand": "WoW", "num": 6},
        "WoW-7":  {"city": "Fort Lauderdale",    "brand": "WoW", "num": 7},
        "WoW-8":  {"city": "Atlanta",            "brand": "WoW", "num": 8},
        "WoW-9":  {"city": "Houston",            "brand": "WoW", "num": 9},
        "WoW-10": {"city": "Dallas",             "brand": "WoW", "num": 10},
        "WoW-11": {"city": "New York",           "brand": "WoW", "num": 11},
        "WoW-12": {"city": "Toronto",            "brand": "WoW", "num": 12},
        "WoW-13": {"city": "Washington",         "brand": "WoW", "num": 13},
        "WoW-14": {"city": "Boston",             "brand": "WoW", "num": 14},
        "WoW-15": {"city": "Chicago",            "brand": "WoW", "num": 15},
        "WoW-16": {"city": "Miami",              "brand": "WoW", "num": 16},
    }
    known_events_by_city = {
        ("WoW", "Miami"):           {"num": 1,  "brand": "WoW"},
        ("WoW", "Fort Lauderdale"): {"num": 7,  "brand": "WoW"},
        ("WoW", "Orlando"):         {"num": 3,  "brand": "WoW"},
        ("WoW", "Tampa"):           {"num": 4,  "brand": "WoW"},
        ("WoW", "West Palm Beach"): {"num": 5,  "brand": "WoW"},
        ("WoW", "Jacksonville"):    {"num": 6,  "brand": "WoW"},
        ("WoW", "Atlanta"):         {"num": 8,  "brand": "WoW"},
        ("WoW", "Houston"):         {"num": 9,  "brand": "WoW"},
        ("WoW", "Dallas"):          {"num": 10, "brand": "WoW"},
        ("WoW", "New York"):        {"num": 11, "brand": "WoW"},
        ("WoW", "Washington"):      {"num": 13, "brand": "WoW"},
        ("WoW", "Toronto"):         {"num": 12, "brand": "WoW"},
    }
    for k, v in known_events_by_num.items():
        meta_by_num.setdefault(k, v)
    for k, v in known_events_by_city.items():
        meta_by_city.setdefault(k, v)

    log("info", "fb_meta_done", by_num=len(meta_by_num), by_city=len(meta_by_city))
    return meta_by_num, meta_by_city

# ─── Top-level data assembly ─────────────────────────────────────────────────
def compute_dashboard_data():
    """Build the full payload the frontend renders from."""
    t0 = time.time()

    # FB meta map (campaign names → city/brand)
    try:
        meta_by_num, meta_by_city = fetch_fb_event_meta()
    except Exception as e:
        log("warn", "fb_meta_failed_continuing", error=str(e))
        meta_by_num, meta_by_city = {}, {}

    # FB period date ranges (LA timezone, FB ad-account local)
    now_la = datetime.now(AD_ACCOUNT_TZ)
    today_la = now_la.replace(hour=0, minute=0, second=0, microsecond=0)
    fb_ranges = {
        "today":     (today_la.strftime("%Y-%m-%d"), today_la.strftime("%Y-%m-%d")),
        "yesterday": ((today_la - timedelta(days=1)).strftime("%Y-%m-%d"), (today_la - timedelta(days=1)).strftime("%Y-%m-%d")),
        "last2":     ((today_la - timedelta(days=2)).strftime("%Y-%m-%d"), today_la.strftime("%Y-%m-%d")),
        "last7":     ((today_la - timedelta(days=7)).strftime("%Y-%m-%d"), today_la.strftime("%Y-%m-%d")),
        "last30":    ((today_la - timedelta(days=30)).strftime("%Y-%m-%d"), today_la.strftime("%Y-%m-%d")),
        "all":       ("2025-01-01", today_la.strftime("%Y-%m-%d")),
    }

    # Fetch all 6 periods + EB events in parallel
    fb_period_results = {name: [] for name in fb_ranges}
    eb_events_box = [[]]

    def _fetch_fb(name, since, until):
        try:
            fb_period_results[name] = fetch_fb_insights(since, until)
        except Exception as e:
            log("warn", "fb_period_failed", period=name, error=str(e))
            fb_period_results[name] = []

    def _fetch_eb():
        try:
            eb_events_box[0] = fetch_eb_events()
        except Exception as e:
            log("warn", "eb_events_failed", error=str(e))
            eb_events_box[0] = []

    # Hard timeouts + manual shutdown(wait=False) so a hung upstream future
    # can't deadlock the whole compute (the 'with' block's default shutdown
    # waits forever for stuck submissions — see Speakpreneur post-mortem).
    pool = ThreadPoolExecutor(max_workers=7)
    try:
        futs = [pool.submit(_fetch_fb, n, s, u) for n, (s, u) in fb_ranges.items()]
        futs.append(pool.submit(_fetch_eb))
        for f in futs:
            try:
                f.result(timeout=45)
            except Exception as e:
                log("warn", "parallel_fetch_timeout_or_error", error=str(e))
    finally:
        pool.shutdown(wait=False)

    events = eb_events_box[0]
    state["eb"]["last_success_at"] = time.time() if events else state["eb"]["last_success_at"]
    state["fb"]["last_success_at"] = time.time() if any(fb_period_results.values()) else state["fb"]["last_success_at"]

    # Process events into the flat array the JS renderer expects
    all_event_data = []
    all_tickets_flat = []

    # Orders filter: only fetch since N days ago for active events (matches original)
    since_iso = (today_la - timedelta(days=30)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for event in events:
        eid = event["id"]
        name = event["name"]["text"]
        city = extract_city(name)
        brand = extract_brand(name)
        capacity = event.get("capacity", 0) or 0
        start_date = event["start"]["local"]
        total_sold = sum(tc.get("quantity_sold", 0) for tc in event.get("ticket_classes", []))
        event_status = event.get("status", "")

        # Only fetch attendee/order detail for active events
        attendees, orders = [], []
        if event_status in ("live", "started"):
            try:
                attendees = fetch_eb_attendees(eid)
                orders = fetch_eb_orders(eid, since=since_iso)
            except Exception as e:
                log("warn", "eb_active_enrich_failed", eid=eid, city=city, error=str(e))

        ticket_list = []
        for a in attendees:
            ticket = {
                "created": a.get("created"),
                "name": (a.get("profile", {}) or {}).get("name", "Unknown"),
                "order_id": a.get("order_id", ""),
                "ticket_type": a.get("ticket_class_name", ""),
                "city": city,
            }
            ticket_list.append(ticket)
            all_tickets_flat.append(ticket)

        order_list = []
        for o in orders:
            # EB gross.value is in cents; divide by 100 to get dollars (matches original)
            cost = (o.get("costs", {}).get("gross", {}).get("value", 0) or 0) / 100
            order_list.append({
                "created": o.get("created"),
                "name": o.get("name", "Unknown"),
                "amount": cost,
                "city": city,
            })

        # Brand-qualified meta lookup
        event_num = extract_event_num_from_eb(name)
        norm_city = normalize_city(city)
        eb_brand = brand
        if event_num is not None:
            bk = f"{eb_brand}-{event_num}"
            meta = meta_by_num.get(bk, {})
            if eb_brand != "GX":
                if meta.get("city"): norm_city = meta["city"]
                if meta.get("brand"): brand = meta["brand"]
        else:
            meta = meta_by_city.get((eb_brand, norm_city), {})
            event_num = meta.get("num", 0)
            if meta.get("brand"): brand = meta["brand"]

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
            "event_status": event_status,
        })

    all_tickets_flat.sort(key=lambda x: x.get("created") or "", reverse=True)

    # Aggregate FB by brand-qualified event key (e.g. "WoW-14", "GX-15")
    fb_periods = {}
    for period_name, campaigns in fb_period_results.items():
        fb_by_event = {}
        for c in campaigns:
            cname = c.get("campaign_name", "")
            ev_num = extract_event_num_from_fb(cname)
            if ev_num is None: continue
            brand_prefix = "GX" if "gifterx" in cname.lower() else "WoW"
            key = f"{brand_prefix}-{ev_num}"
            spend = float(c.get("spend", 0) or 0)
            impressions = int(c.get("impressions", 0) or 0)
            reach = int(c.get("reach", 0) or 0)
            purchases, link_clicks = _extract_fb_actions(c.get("actions", []))
            if key in fb_by_event:
                agg = fb_by_event[key]
                agg["spend"] += spend
                agg["impressions"] += impressions
                agg["reach"] += reach
                agg["purchases"] += purchases
                agg["link_clicks"] += link_clicks
            else:
                fb_by_event[key] = {"spend": spend, "impressions": impressions, "reach": reach,
                                    "purchases": purchases, "link_clicks": link_clicks}
        fb_periods[period_name] = fb_by_event

    # Sort: active first, then completed (matches original)
    active = [e for e in all_event_data if e["event_status"] in ("live", "started")]
    completed = [e for e in all_event_data if e["event_status"] not in ("live", "started")]
    active.sort(key=lambda x: x.get("start_date", ""))
    completed.sort(key=lambda x: x.get("start_date", ""), reverse=True)
    sorted_events = active + completed

    log("info", "compute_done", elapsed_s=round(time.time() - t0, 1),
        events=len(sorted_events), active=len(active), completed=len(completed),
        tickets=len(all_tickets_flat), fb_periods=list(fb_periods.keys()))

    return {
        "events": sorted_events,
        "allTickets": all_tickets_flat[:300],
        "fbData": fb_periods,
        "generatedAt": datetime.now(ET).isoformat(),
        "version": APP_VERSION,
    }

# ─── Routes ──────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/config")
def api_config():
    return jsonify({
        "brandName": BRAND_NAME,
        "version": APP_VERSION,
        "cacheTtlSeconds": DATA_CACHE_TTL,
        "staleThresholdSeconds": STALE_THRESHOLD,
    })

@app.route("/api/health")
def api_health():
    out = {
        "eb": {"status": "unknown", "error": None, "errorClass": None,
               "lastSuccessAt": state["eb"]["last_success_at"],
               "stale": is_stale(state["eb"]["last_success_at"] or 0)},
        "fb": {"status": "unknown", "error": None, "errorClass": None,
               "lastSuccessAt": state["fb"]["last_success_at"],
               "stale": is_stale(state["fb"]["last_success_at"] or 0)},
    }
    try:
        if not EB_TOKEN or not EB_ORG_ID: raise Exception("EB env vars missing")
        res = _eb_request(
            f"https://www.eventbriteapi.com/v3/organizations/{EB_ORG_ID}/events/",
            {"status": "live", "token": EB_TOKEN, "page_size": 1}, timeout=(5, 10), max_retries=1,
        )
        res.raise_for_status()
        out["eb"]["status"] = "ok"
    except Exception as e:
        out["eb"]["status"] = "error"
        out["eb"]["error"] = str(e)[:300]
        out["eb"]["errorClass"] = classify_error(str(e))

    try:
        if not FB_TOKEN or not FB_AD_ACCOUNT: raise Exception("FB env vars missing")
        res = _fb_get(
            f"https://graph.facebook.com/v25.0/act_{FB_AD_ACCOUNT}",
            {"fields": "name", "access_token": FB_TOKEN}, timeout=(5, 10), retries=1,
        )
        body = res.json() if res.headers.get("content-type","").startswith("application/json") else {}
        if res.status_code != 200 or body.get("error"):
            raise Exception(body.get("error", {}).get("message", f"HTTP {res.status_code}"))
        out["fb"]["status"] = "ok"
    except Exception as e:
        out["fb"]["status"] = "error"
        out["fb"]["error"] = str(e)[:300]
        out["fb"]["errorClass"] = classify_error(str(e))

    resp = jsonify(out)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp

@app.route("/api/fb_custom")
def api_fb_custom():
    """On-demand FB insights for custom date ranges. Used by the frontend's
    custom date picker — bypasses the precomputed period buckets."""
    since = request.args.get("since")
    until = request.args.get("until")
    if not since or not until:
        return jsonify({"error": "Missing since/until params"}), 400

    cache_key = f"fb_custom:{since}:{until}"
    cached = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    try:
        campaigns = fetch_fb_insights(since, until)
    except Exception as e:
        log("error", "fb_custom_failed", since=since, until=until, error=str(e))
        return jsonify({"error": str(e)[:300], "errorClass": classify_error(str(e))}), 500

    fb_by_event = {}
    for c in campaigns:
        cname = c.get("campaign_name", "")
        ev_num = extract_event_num_from_fb(cname)
        if ev_num is None: continue
        brand_prefix = "GX" if "gifterx" in cname.lower() else "WoW"
        key = f"{brand_prefix}-{ev_num}"
        spend = float(c.get("spend", 0) or 0)
        impressions = int(c.get("impressions", 0) or 0)
        reach = int(c.get("reach", 0) or 0)
        purchases, link_clicks = _extract_fb_actions(c.get("actions", []))
        if key in fb_by_event:
            agg = fb_by_event[key]
            agg["spend"] += spend
            agg["impressions"] += impressions
            agg["reach"] += reach
            agg["purchases"] += purchases
            agg["link_clicks"] += link_clicks
        else:
            fb_by_event[key] = {"spend": spend, "impressions": impressions, "reach": reach,
                                "purchases": purchases, "link_clicks": link_clicks}

    cache_set(cache_key, fb_by_event, ttl=CACHE_TTL)
    return jsonify(fb_by_event)

@app.route("/api/data")
def api_data():
    force = request.args.get("force") == "1"
    cache_key = "data"
    if not force:
        cached = cache_get(cache_key)
        if cached: return jsonify({**cached, "cached": True})
    try:
        payload = compute_dashboard_data()
        cache_set(cache_key, payload, ttl=DATA_CACHE_TTL)
        return jsonify({**payload, "cached": False})
    except Exception as e:
        log("error", "api_data_failed", error=str(e), trace=traceback.format_exc()[-500:])
        state["eb"]["last_error"] = {"ts": time.time(), "message": str(e)[:300]}
        stale = _cache.get(cache_key)
        if stale:
            return jsonify({**stale["value"], "cached": True, "stale": True,
                            "error": f"Live fetch failed, serving stale data: {str(e)[:200]}"})
        return jsonify({"error": str(e)[:300], "errorClass": classify_error(str(e))}), 500

# ─── Startup validation ──────────────────────────────────────────────────────
def validate_on_startup():
    log("info", "startup_validation_begin")
    if EB_TOKEN and EB_ORG_ID:
        try:
            res = requests.get(
                f"https://www.eventbriteapi.com/v3/organizations/{EB_ORG_ID}/events/",
                params={"status": "live", "token": EB_TOKEN, "page_size": 1}, timeout=10,
            )
            if res.status_code == 200:
                log("info", "eb_ok_on_startup")
            else:
                log("error", "eb_invalid_on_startup", status=res.status_code, body=res.text[:200])
                if res.status_code in (401, 403):
                    print("\n████████████████████████████████████████████████████")
                    print("██  EB_TOKEN UNAUTHORIZED — fix in Render env vars ██")
                    print("████████████████████████████████████████████████████\n", flush=True)
        except Exception as e:
            log("warn", "eb_startup_network_error", error=str(e))
    else:
        log("warn", "eb_not_configured")
    if FB_TOKEN and FB_AD_ACCOUNT:
        try:
            res = requests.get(
                f"https://graph.facebook.com/v25.0/act_{FB_AD_ACCOUNT}",
                params={"fields": "name", "access_token": FB_TOKEN}, timeout=10,
            )
            body = res.json() if res.status_code != 500 else {}
            if res.status_code == 200 and not body.get("error"):
                log("info", "fb_ok_on_startup", account=body.get("name"))
            else:
                err = body.get("error", {})
                log("error", "fb_invalid_on_startup", status=res.status_code, error=err)
                if err.get("code") == 190:
                    print("\n████████████████████████████████████████████████████")
                    print("██  FB_TOKEN EXPIRED — rotate System User token   ██")
                    print("████████████████████████████████████████████████████\n", flush=True)
        except Exception as e:
            log("warn", "fb_startup_network_error", error=str(e))
    else:
        log("warn", "fb_not_configured")
    log("info", "startup_validation_complete")

import threading
threading.Thread(target=validate_on_startup, daemon=True).start()

# ─── Never-crash handlers (preserve HTTP errors like 404) ────────────────────
@app.errorhandler(Exception)
def handle_unexpected(e):
    if isinstance(e, HTTPException):
        return e
    log("error", "unhandled_exception", error=str(e), trace=traceback.format_exc()[-500:])
    return jsonify({"error": "internal error", "detail": str(e)[:200]}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
