#!/usr/bin/env python3
"""
Wizard of Words Dashboard - Live Web App
Serves a dashboard with live data from Eventbrite + Facebook Ads APIs.
Covers both "Wizard of Words" and "GifterX Talks" events for Christopher Kai.
Data is cached for 10 minutes to keep things fast.
"""
import os
import requests
import json
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, Response

ET = ZoneInfo("America/New_York")

app = Flask(__name__)

# ====== CONFIG (from environment variables) ======
EB_TOKEN = os.environ.get("EB_TOKEN", "")
EB_ORG_ID = os.environ.get("EB_ORG_ID", "")
FB_TOKEN = os.environ.get("FB_TOKEN", "")
FB_AD_ACCOUNT = os.environ.get("FB_AD_ACCOUNT", "")

# Simple cache: store generated HTML and timestamp
_cache = {"html": None, "time": 0}
CACHE_TTL = 600  # 10 minutes

# Track API errors for dashboard display
_api_errors = []

# City name normalization (EB name → canonical name matching FB)
CITY_NORMALIZE = {
    "new york city": "New York",
    "st. pete's/tampa": "Tampa",
    "st pete's/tampa": "Tampa",
    "washington dc": "Washington",
}

def normalize_city(city):
    return CITY_NORMALIZE.get(city.lower().strip(), city)

# ====== FETCH EVENTBRITE DATA ======
def fetch_eb_events():
    """Fetch ALL events (live, started, completed) for the organization."""
    all_events = []
    page = 1
    while True:
        url = f"https://www.eventbriteapi.com/v3/organizations/{EB_ORG_ID}/events/"
        params = {"status": "all", "expand": "ticket_classes", "token": EB_TOKEN, "page": page}
        res = requests.get(url, params=params)
        res.raise_for_status()
        data = res.json()
        events = [e for e in data.get("events", [])
                  if e.get("status") in ("live", "started", "ended", "completed")]
        all_events.extend(events)
        if not data.get("pagination", {}).get("has_more_items"):
            break
        page += 1
        if page > 10:
            break
    return all_events

def fetch_eb_orders(event_id, since=None):
    all_orders = []
    page = 1
    while True:
        url = f"https://www.eventbriteapi.com/v3/events/{event_id}/orders/"
        params = {"token": EB_TOKEN, "page": page}
        if since:
            params["changed_since"] = since
        res = requests.get(url, params=params)
        res.raise_for_status()
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
        res = requests.get(url, params=params)
        res.raise_for_status()
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
    return "wizard of words" in lower or "gifterx" in lower

def fetch_fb_insights(since_date, until_date):
    global _api_errors
    all_results = []
    url = f"https://graph.facebook.com/v25.0/act_{FB_AD_ACCOUNT}/insights"
    params = {
        "fields": "campaign_name,campaign_id,spend,impressions,reach,actions",
        "level": "campaign",
        "time_range": json.dumps({"since": since_date, "until": until_date}),
        "limit": 200,
        "access_token": FB_TOKEN
    }
    res = requests.get(url, params=params)
    if res.status_code != 200:
        try:
            err_data = res.json()
            err_msg = err_data.get("error", {}).get("message", f"HTTP {res.status_code}")
            err_code = err_data.get("error", {}).get("code", "")
            err_sub = err_data.get("error", {}).get("error_subcode", "")
        except Exception:
            err_msg = f"HTTP {res.status_code}"
            err_code = ""
            err_sub = ""
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
        res = requests.get(paging["next"])
        if res.status_code != 200:
            break
        data = res.json()
        all_results.extend(data.get("data", []))
        paging = data.get("paging", {})
    return [r for r in all_results if is_relevant_campaign(r.get("campaign_name", ""))]

# ====== HELPERS ======
def extract_city(event_name):
    """Extract city from Eventbrite event name."""
    # GifterX Talks: "GifterX Talks 15 - Miami"
    m = re.match(r'GifterX\s+Talks\s+\d+\s*-\s*(.+)', event_name, re.I)
    if m:
        return m.group(1).strip()

    # Wizard of Words with city before colon:
    #   "Wizard of Words" Houston: ...
    #   Wizard of Words New York City: ...
    #   "Wizard of Words 4" St. Pete's/Tampa: ...
    m = re.match(r'"?Wizard\s+of\s+Words(?:\s+\d+)?"?\s+(.+?):\s', event_name, re.I)
    if m:
        city = m.group(1).strip().strip('"').strip()
        if city:
            return city

    # Older format without city: "Wizard of Words 3: Speak to Sell..."
    # Return the whole name as fallback (will be overridden by known_events)
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
    m = re.search(r'(?:V\d+\s+)?(?:Wizard\s+of\s+Words|GifterX)\s+(\d+)', campaign_name, re.I)
    return int(m.group(1)) if m else None

def extract_city_from_fb(campaign_name):
    """Extract city from FB campaign name like 'JSC - Wizard of Words 9 Houston TX 2026 MAR 12-13'."""
    m = re.search(
        r'(?:V\d+\s+)?(?:Wizard\s+of\s+Words|GifterX)\s+\d+\s+(?:VIRTUAL\s+)?(.+?)\s+[A-Z]{2}\s+\d{4}',
        campaign_name
    )
    return m.group(1).strip() if m else None

def fetch_fb_event_meta():
    """Build city → {num, brand} mapping from FB campaign names."""
    global _api_errors
    url = f"https://graph.facebook.com/v25.0/act_{FB_AD_ACCOUNT}/campaigns"
    params = {"fields": "name,status", "limit": 100, "access_token": FB_TOKEN}
    res = requests.get(url, params=params)
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
        return {}
    meta = {}
    for c in res.json().get("data", []):
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
        # Only use ACTIVE campaigns for meta (most current mapping)
        if c.get("status") == "ACTIVE":
            meta[normalize_city(city)] = {"num": num, "brand": brand}
        # Also store if city not yet mapped (from paused campaigns)
        elif normalize_city(city) not in meta:
            meta[normalize_city(city)] = {"num": num, "brand": brand}
    return meta

def build_dashboard_html():
    """Build the full dashboard HTML with live data."""
    global _api_errors
    _api_errors = []  # Reset errors for this build
    event_meta = fetch_fb_event_meta()

    # Fallback mapping if FB meta API fails to return data
    known_events = {
        "Miami": {"num": 1, "brand": "WoW"},
        "Fort Lauderdale": {"num": 7, "brand": "WoW"},
        "Orlando": {"num": 3, "brand": "WoW"},
        "Tampa": {"num": 4, "brand": "WoW"},
        "West Palm Beach": {"num": 5, "brand": "WoW"},
        "Jacksonville": {"num": 6, "brand": "WoW"},
        "Atlanta": {"num": 8, "brand": "WoW"},
        "Houston": {"num": 9, "brand": "WoW"},
        "Dallas": {"num": 10, "brand": "WoW"},
        "New York": {"num": 11, "brand": "WoW"},
        "Washington": {"num": 13, "brand": "WoW"},
        "Toronto": {"num": 12, "brand": "WoW"},
    }
    # GifterX events are always Miami — handled via event_num from EB name
    for city, info in known_events.items():
        if city not in event_meta:
            event_meta[city] = info

    events = fetch_eb_events()

    # Use the ad account's timezone (America/Los_Angeles) so "today" matches Facebook's definition
    AD_ACCOUNT_TZ = ZoneInfo("America/Los_Angeles")
    now = datetime.now(AD_ACCOUNT_TZ)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Convert to UTC for Eventbrite API (which expects UTC timestamps)
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

        # Determine event number: first try from EB name, then from FB meta
        event_num = extract_event_num_from_eb(name)
        norm_city = normalize_city(city)
        if event_num is None:
            meta = event_meta.get(norm_city, {})
            event_num = meta.get("num", 0)
            if not brand or brand == "WoW":
                brand = meta.get("brand", "WoW")
        else:
            # If we have num from EB, still check meta for brand confirmation
            meta = event_meta.get(norm_city, {})

        display_city = f"{brand} {event_num} \u2013 {norm_city}" if event_num else f"{brand} \u2013 {city}"

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

    # Fetch Facebook data for multiple periods
    fb_periods = {}
    fb_date_ranges = {
        "today": (today_start.strftime("%Y-%m-%d"), today_start.strftime("%Y-%m-%d")),
        "yesterday": ((today_start - timedelta(days=1)).strftime("%Y-%m-%d"), (today_start - timedelta(days=1)).strftime("%Y-%m-%d")),
        "last2": ((today_start - timedelta(days=2)).strftime("%Y-%m-%d"), today_start.strftime("%Y-%m-%d")),
        "last7": ((today_start - timedelta(days=7)).strftime("%Y-%m-%d"), today_start.strftime("%Y-%m-%d")),
        "last30": ((today_start - timedelta(days=30)).strftime("%Y-%m-%d"), today_start.strftime("%Y-%m-%d")),
        "all": ("2025-01-01", today_start.strftime("%Y-%m-%d"))
    }

    for period_name, (since, until) in fb_date_ranges.items():
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
            purchases = 0
            for a in c.get("actions", []):
                if a.get("action_type") == "omni_purchase":
                    purchases = int(a.get("value", 0))
                    break
            if key in fb_by_event:
                fb_by_event[key]["spend"] += spend
                fb_by_event[key]["impressions"] += impressions
                fb_by_event[key]["reach"] += reach
                fb_by_event[key]["purchases"] += purchases
            else:
                fb_by_event[key] = {"spend": spend, "impressions": impressions, "reach": reach, "purchases": purchases}
        fb_periods[period_name] = fb_by_event

    # Fetch daily FB data for custom date range support
    fb_daily = []
    daily_since = (today_start - timedelta(days=90)).strftime("%Y-%m-%d")
    daily_until = today_start.strftime("%Y-%m-%d")
    url = f"https://graph.facebook.com/v25.0/act_{FB_AD_ACCOUNT}/insights"
    params = {
        "fields": "campaign_name,campaign_id,spend,impressions,reach,actions",
        "level": "campaign",
        "time_increment": 1,
        "time_range": json.dumps({"since": daily_since, "until": daily_until}),
        "limit": 500,
        "access_token": FB_TOKEN
    }
    res = requests.get(url, params=params)
    if res.status_code == 200:
        daily_data = res.json().get("data", [])
        paging = res.json().get("paging", {})
        while paging.get("next"):
            res = requests.get(paging["next"])
            if res.status_code != 200:
                break
            page_data = res.json()
            daily_data.extend(page_data.get("data", []))
            paging = page_data.get("paging", {})

        for c in daily_data:
            cname = c.get("campaign_name", "")
            if not is_relevant_campaign(cname):
                continue
            ev_num = extract_event_num_from_fb(cname)
            if ev_num is None:
                continue
            purchases = 0
            for a in c.get("actions", []):
                if a.get("action_type") == "omni_purchase":
                    purchases = int(a.get("value", 0))
                    break
            brand_prefix = "GX" if "gifterx" in cname.lower() else "WoW"
            fb_daily.append({
                "date": c.get("date_start", ""),
                "event_num": f"{brand_prefix}-{ev_num}",
                "spend": float(c.get("spend", 0)),
                "impressions": int(c.get("impressions", 0)),
                "reach": int(c.get("reach", 0)),
                "purchases": purchases
            })

    # Sort: live/started first (by start_date asc), then completed (by start_date desc)
    active = [e for e in all_event_data if e["event_status"] in ("live", "started")]
    completed = [e for e in all_event_data if e["event_status"] not in ("live", "started")]
    active.sort(key=lambda x: x["start_date"])
    completed.sort(key=lambda x: x["start_date"], reverse=True)
    all_event_data = active + completed

    # Generate HTML
    events_json = json.dumps(all_event_data)
    tickets_json = json.dumps(all_tickets_flat[:300])
    fb_json = json.dumps(fb_periods)
    fb_daily_json = json.dumps(fb_daily)
    generated_time = datetime.now(ET).strftime("%B %d, %Y at %I:%M %p") + " ET"

    # Build API error banner HTML
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
            <button class="refresh-btn" onclick="location.reload()">&#x21BB; Refresh Data</button>
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
            <th>Event</th><th>Event Date</th><th>Days Out</th><th>Amount Spent</th><th>Tickets Sold (EB)</th><th>Cost/Ticket (EB)</th><th>Tickets Sold (Meta)</th><th>Cost/Ticket (Meta)</th><th>Total Sold</th><th>Capacity</th><th>Fill %</th><th>Period Revenue</th><th>Status</th>
        </tr></thead><tbody id="tblBody"></tbody></table></div>
    </div>
    <div class="tpanel" id="p-combined">
        <div style="padding:20px 32px 0">
            <h2 style="font-size:18px;color:#f8fafc;margin-bottom:4px">Real Tickets (EB) vs Ad Spend (Meta)</h2>
            <p style="font-size:13px;color:#94a3b8;margin-bottom:16px">EB = source of truth for sales (no attribution delay). Meta = source of truth for spend.</p>
        </div>
        <div class="tbl-wrap"><table><thead><tr>
            <th>Event</th><th>Tickets Sold (EB)</th><th>Revenue</th><th>Ad Spend</th><th>Cost/Ticket (EB)</th><th>ROAS</th><th>Tickets Sold (Meta)</th><th>Impressions</th><th>Reach</th>
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
    const fbDaily = {fb_daily_json};
    let period = 'last7';

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

    function applyCustomRange() {{
        const s = document.getElementById('customStart').value;
        const e = document.getElementById('customEnd').value;
        if (!s || !e) return;
        document.querySelectorAll('.dbtn').forEach(b=>b.classList.remove('active'));
        periodStarts['custom'] = new Date(s + 'T00:00:00');
        periodEnds['custom'] = new Date(e + 'T23:59:59.999');
        period = 'custom';
        updatePeriodLabel();
        render();
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
        if (period !== 'custom') return fbData[period] || {{}};
        const s = document.getElementById('customStart').value;
        const e = document.getElementById('customEnd').value;
        if (!s || !e) return {{}};
        const result = {{}};
        fbDaily.forEach(row => {{
            if (row.date >= s && row.date <= e) {{
                const key = row.event_num;
                if (!result[key]) result[key] = {{spend:0, impressions:0, reach:0, purchases:0}};
                result[key].spend += row.spend;
                result[key].impressions += row.impressions;
                result[key].reach += row.reach;
                result[key].purchases += row.purchases;
            }}
        }});
        return result;
    }}

    function render() {{
        const fb = getFbForPeriod();
        let totalPeriodTickets=0, totalMetaTickets=0, totalPeriodRev=0, totalSold=0, totalCap=0, totalSpend=0, drySpells=0;
        let rows='', cmbRows='', alerts=[];
        let lastSection = '';

        events.forEach(e => {{
            // Add section separator between active and completed events
            const section = (e.event_status === 'live' || e.event_status === 'started') ? 'active' : 'completed';
            if (section !== lastSection && lastSection !== '') {{
                rows += '<tr class="separator-row"><td colspan="13">&#x2500;&#x2500; Completed Events &#x2500;&#x2500;</td></tr>';
                cmbRows += '<tr class="separator-row"><td colspan="9">&#x2500;&#x2500; Completed Events &#x2500;&#x2500;</td></tr>';
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
            const fbd = fbKey ? (fb[fbKey] || null) : null;
            const spend = fbd ? fbd.spend : 0;
            const metaTickets = fbd ? fbd.purchases : 0;
            const overviewCpt = pTickets.length>0&&spend>0 ? spend/pTickets.length : 0;
            const metaCpt = metaTickets>0&&spend>0 ? spend/metaTickets : 0;

            totalPeriodTickets += pTickets.length;
            totalMetaTickets += metaTickets;
            totalPeriodRev += pRev;
            totalSold += e.total_sold;
            totalCap += e.capacity;
            totalSpend += spend;

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
            const periodTicketDisplay = isCompleted ? '<span style="color:#94a3b8" title="Per-period data not available for completed events">'+e.total_sold+'*</span>' : pTickets.length;

            rows += `<tr class="${{rowClass}}">
                <td class="cn"><span class="${{brandClass}}">${{e.brand}}</span> ${{e.display_city}}</td>
                <td style="color:#94a3b8">${{dateStr}}</td>
                <td>${{daysDisplay}}</td>
                <td style="color:#f59e0b">${{spend>0?'$'+fmt(spend):'$0.00'}}</td>
                <td style="font-weight:600;color:#4ade80">${{periodTicketDisplay}}</td>
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
            const cpt = pTickets.length>0&&spend>0 ? spend/pTickets.length : 0;
            const roas = spend>0 ? pRev/spend : 0;

            const cptColor = cpt>300?'#f87171':cpt>200?'#fbbf24':cpt>0?'#4ade80':'#94a3b8';
            const roasColor = roas>=1?'#4ade80':'#f87171';
            cmbRows += `<tr class="${{rowClass}}">
                <td class="cn"><span class="${{brandClass}}">${{e.brand}}</span> ${{e.display_city}}</td>
                <td style="font-weight:600;color:#4ade80">${{isCompleted?e.total_sold+'*':pTickets.length}}</td>
                <td style="color:#4ade80">${{isCompleted?'&#8212;':'$'+fmt(pRev)}}</td>
                <td style="color:#f59e0b">$${{fmt(spend)}}</td>
                <td style="font-weight:700;color:${{cptColor}}">${{cpt>0?'$'+fmt(cpt):'&#8212;'}}</td>
                <td style="color:${{roasColor}}">${{roas>0?roas.toFixed(2)+'x':'&#8212;'}}</td>
                <td style="color:#94a3b8">${{fbPurch}}</td>
                <td style="color:#94a3b8">${{impr.toLocaleString()}}</td>
                <td style="color:#94a3b8">${{reach.toLocaleString()}}</td>
            </tr>`;
        }});

        const tCpt = totalPeriodTickets>0&&totalSpend>0 ? totalSpend/totalPeriodTickets : 0;
        const tRoas = totalSpend>0 ? totalPeriodRev/totalSpend : 0;
        cmbRows += `<tr class="totrow">
            <td>TOTALS</td>
            <td style="color:#4ade80">${{totalPeriodTickets}}</td>
            <td style="color:#4ade80">$${{fmt(totalPeriodRev)}}</td>
            <td>$${{fmt(totalSpend)}}</td>
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
    return html

@app.route("/")
def dashboard():
    # Check cache
    if _cache["html"] and (time.time() - _cache["time"]) < CACHE_TTL:
        return Response(_cache["html"], content_type="text/html; charset=utf-8")

    try:
        html = build_dashboard_html()
        _cache["html"] = html
        _cache["time"] = time.time()
        return Response(html, content_type="text/html; charset=utf-8")
    except Exception as e:
        return f"<h1>Error loading dashboard</h1><p>{str(e)}</p>", 500

@app.route("/refresh")
def refresh():
    """Force refresh the cache."""
    _cache["html"] = None
    _cache["time"] = 0
    return '<script>window.location="/"</script>'

@app.route("/debug")
def debug():
    """Diagnostic endpoint to check API connectivity."""
    results = {"timestamp": datetime.now(ET).isoformat(), "checks": {}}

    # Check Eventbrite
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

    # Check Facebook
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

    # Config check (mask tokens)
    results["config"] = {
        "EB_TOKEN": ("set (" + EB_TOKEN[:6] + "...)" ) if EB_TOKEN else "NOT SET",
        "EB_ORG_ID": EB_ORG_ID if EB_ORG_ID else "NOT SET",
        "FB_TOKEN": ("set (" + FB_TOKEN[:6] + "...)" ) if FB_TOKEN else "NOT SET",
        "FB_AD_ACCOUNT": FB_AD_ACCOUNT if FB_AD_ACCOUNT else "NOT SET",
    }

    results["recent_errors"] = _api_errors

    return Response(json.dumps(results, indent=2), content_type="application/json")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
