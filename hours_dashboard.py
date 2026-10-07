"""
Tech hours dashboard builder.

Pulls the Work Order Labor Summary report from the AppFolio Reports API (v2) for
the last 90 days, and writes the tech hours dashboard to _site/hours/index.html,
next to the open work orders dashboard. Clicking a tech's day on the page shows
the work orders behind those hours.

Credentials come from the APPFOLIO_CLIENT_ID and APPFOLIO_CLIENT_SECRET
environment variables (GitHub secrets), or from config.ini when run locally.

Optional settings (environment variables):
    APPFOLIO_LABOR_REPORT   AppFolio's API name for the labor report, if the
                            built-in guesses don't find it
    APPFOLIO_LABOR_FILTERS  extra report filters as JSON, e.g. a date filter
    HOURS_DAYS              how many days to pull and show (default 90)

Usage:
    python hours_dashboard.py --out _site   # build _site/hours/index.html
    python hours_dashboard.py --inspect     # find the report and list its fields, build nothing
"""
import argparse
import configparser
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests

VERSION = "2026-10-07 hours v6 (90 days, weekly pulls)"
HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "hours_template.html"
CONFIG_PATH = HERE / "config.ini"
TZ = ZoneInfo("America/Los_Angeles")
DAYS = int(os.environ.get("HOURS_DAYS", "90"))

# AppFolio's API name for the Work Order Labor Summary report
# (from Reports > Work Order Labor Summary: /buffered_reports/work_order_labor_summary).
REPORT_CANDIDATES = ["work_order_labor_summary"]

# Field names for each thing the dashboard needs. The first name in each list is the
# report's own column (WorkOrderNumber, Date, MaintenanceTech, PropertyName, UnitName,
# StartTime, EndTime, WorkedHours, WorkOrderStatus, WorkOrderIssue) in the API's snake_case.
# The report's "hours" column is BILLABLE hours, so it's deliberately not used here.
# If --inspect says one is missing, add the right name from its list to that line.
FIELD_MAP = {
    "date":      ["date", "labor_date", "labor_performed_on"],
    "tech":      ["maintenance_tech", "maintenance_technician", "technician"],
    "hours":     ["worked_hours", "hours_worked"],
    "start":     ["start_time", "started_at"],
    "end":       ["end_time", "ended_at"],
    "wo_number": ["work_order_number", "work_order"],
    "wo_id":     ["work_order_id"],
    "sr_id":     ["service_request_id"],
    "property":  ["property_name", "property"],
    "unit":      ["unit_name", "unit"],
    "status":    ["work_order_status", "status"],
    "issue":     ["work_order_issue", "issue"],
}
REQUIRED = ["tech", "hours", "wo_number"]

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("hours")


# ---------------------------------------------------------------- config / API
def load_cfg():
    sub = os.environ.get("APPFOLIO_SUBDOMAIN", "guidemanagement").strip()
    cid, sec = os.environ.get("APPFOLIO_CLIENT_ID"), os.environ.get("APPFOLIO_CLIENT_SECRET")
    if cid and sec:
        return {"subdomain": sub, "client_id": cid.strip(), "client_secret": sec.strip()}
    if CONFIG_PATH.exists():
        cp = configparser.ConfigParser()
        cp.read(CONFIG_PATH)
        c = cp["appfolio"]
        return {"subdomain": c.get("subdomain", sub).strip(),
                "client_id": c["client_id"].strip(), "client_secret": c["client_secret"].strip()}
    raise SystemExit("No AppFolio credentials. Set the APPFOLIO_CLIENT_ID / APPFOLIO_CLIENT_SECRET secrets.")


def date_filters():
    """Only ask AppFolio for the days the page shows (the report's Date Range filter)."""
    today = datetime.now(TZ).date()
    return {"labor_performed_from": (today - timedelta(days=DAYS - 1)).isoformat(),
            "labor_performed_to": today.isoformat()}


def extra_filters():
    raw = os.environ.get("APPFOLIO_LABOR_FILTERS", "").strip()
    if not raw:
        return date_filters()
    try:
        f = json.loads(raw)
    except json.JSONDecodeError:
        raise SystemExit(f"APPFOLIO_LABOR_FILTERS isn't valid JSON: {raw}")
    log.info("Extra report filters: %s", f)
    return {**date_filters(), **f}


def check(r):
    if r.status_code == 401:
        raise SystemExit("AppFolio rejected the API credentials (401). Check the APPFOLIO_CLIENT_ID / APPFOLIO_CLIENT_SECRET secrets.")
    if r.status_code >= 400:
        raise SystemExit(f"AppFolio returned {r.status_code}: {r.text[:500]}")


def post(cfg, report, body):
    url = f"https://{cfg['subdomain']}.appfolio.com/api/v2/reports/{report}.json"
    return requests.post(url, json=body, auth=(cfg["client_id"], cfg["client_secret"]), timeout=180)


def find_report(cfg, filters):
    """Return (report name, first response) for the first labor report name AppFolio accepts."""
    forced = os.environ.get("APPFOLIO_LABOR_REPORT", "").strip()
    names = [forced] if forced else REPORT_CANDIDATES
    for name in names:
        r = post(cfg, name, {"paginate_results": True, **filters})
        if r.status_code == 400 and "labor_performed_from" in filters:
            log.info("AppFolio rejected the date filter (%s); pulling without it: %s", r.status_code, r.text[:200])
            filters = {k: v for k, v in filters.items() if not k.startswith("labor_performed_")}
            r = post(cfg, name, {"paginate_results": True, **filters})
        if r.status_code in (400, 404) and not forced:
            log.info("Report '%s': not available (%s)", name, r.status_code)
            continue
        check(r)
        log.info("Using AppFolio report '%s'", name)
        return name, r, filters
    raise SystemExit(
        "AppFolio didn't accept these report names: " + ", ".join(names) + ".\n"
        "Find the labor report's API name in AppFolio's Reports API documentation and add it as a "
        "repository variable named APPFOLIO_LABOR_REPORT (Settings > Secrets and variables > Actions > Variables)."
    )


def next_page_urls(cfg, current_url, nxt):
    """AppFolio's next_page_url may be a full address or a partial one. List the ways to read it, most likely first."""
    if nxt.startswith("http"):
        return [nxt]
    root = f"https://{cfg['subdomain']}.appfolio.com/"
    tail = nxt.lstrip("/")
    tries = [urljoin(current_url, nxt), urljoin(root, nxt), root + "api/v2/" + tail, root + "api/" + tail]
    if tail.startswith("v2/"):
        tries.insert(0, root + "api/" + tail)
    out = []
    for u in tries:
        if u not in out:
            out.append(u)
    return out


def shown(url):
    """A URL's path for the log, without its query values."""
    p = urlsplit(url)
    keys = [kv.split("=")[0] for kv in p.query.split("&") if kv]
    return p.path + ("?" + "&".join(k + "=..." for k in keys) if keys else "")


def fetch_all(cfg, first_response):
    auth = (cfg["client_id"], cfg["client_secret"])
    rows, r, page = [], first_response, 0
    while True:
        page += 1
        data = r.json()
        if isinstance(data, list):
            rows.extend(data)
            break
        rows.extend(data.get("results", []))
        nxt = data.get("next_page_url")
        if not nxt:
            break
        if page == 1:
            log.info("Page 1: %s entries. AppFolio's next page link looks like: %s", len(rows), shown(nxt) if not nxt.startswith("http") else shown(nxt) + " (full address)")
        for url in next_page_urls(cfg, r.url, nxt):
            attempt = requests.get(url, auth=auth, timeout=180)
            if attempt.status_code != 404:
                check(attempt)
                if page == 1:
                    log.info("Reading further pages from %s", shown(url))
                r = attempt
                break
            log.info("Next page not found at %s", shown(url))
        else:
            raise SystemExit("AppFolio's next-page link didn't work in any form tried above. Send these log lines to get it fixed.")
    log.info("AppFolio returned %s labor entries (%s pages)", len(rows), page)
    return rows


def follow(cfg, r, nxt):
    """Get the next page, or None if AppFolio won't serve it."""
    auth = (cfg["client_id"], cfg["client_secret"])
    for url in next_page_urls(cfg, r.url, nxt):
        attempt = requests.get(url, auth=auth, timeout=180)
        if attempt.status_code != 404:
            check(attempt)
            return attempt
    return None


def fetch_range(cfg, report, d1, d2, extra):
    """Pull one date range. If it's too big for one page and AppFolio won't serve the next page, split it in half."""
    body = {"paginate_results": True, **extra,
            "labor_performed_from": d1.isoformat(), "labor_performed_to": d2.isoformat()}
    r = post(cfg, report, body)
    check(r)
    data = r.json()
    if isinstance(data, list):
        return data
    rows, nxt = list(data.get("results", [])), data.get("next_page_url")
    while nxt:
        got = follow(cfg, r, nxt)
        if got is None:
            if d1 < d2:
                mid = d1 + (d2 - d1) // 2
                log.info("  %s to %s is more than one page (%s+ entries); splitting it", d1, d2, len(rows))
                return fetch_range(cfg, report, d1, mid, extra) + fetch_range(cfg, report, mid + timedelta(days=1), d2, extra)
            log.info("  %s has more than %s entries and AppFolio wouldn't send the rest; keeping the first %s", d1, len(rows), len(rows))
            return rows
        r = got
        data = r.json()
        rows.extend(data.get("results", []))
        nxt = data.get("next_page_url")
    return rows


def fetch_by_week(cfg, report, extra):
    """Pull the date range a week at a time, so each request fits on one page."""
    today = datetime.now(TZ).date()
    first = today - timedelta(days=DAYS - 1)
    extra = {k: v for k, v in extra.items() if not k.startswith("labor_performed_")}
    rows, d = [], first
    while d <= today:
        end = min(d + timedelta(days=6), today)
        part = fetch_range(cfg, report, d, end, extra)
        log.info("%s to %s: %s entries", d, end, len(part))
        rows.extend(part)
        d = end + timedelta(days=1)
    log.info("AppFolio returned %s labor entries in total", len(rows))
    return rows


# ---------------------------------------------------------------- parsing
def pick(rec, names):
    for n in names:
        if n in rec and rec[n] not in (None, ""):
            return rec[n]
    return ""


def which(keys, names):
    return next((n for n in names if n in keys), None)


DT_FORMATS = ["%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S",
              "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %I:%M %p", "%m/%d/%Y", "%m/%d/%y"]


def parse_dt(v):
    if not v:
        return None
    s = str(v).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        dt = None
        for f in DT_FORMATS:
            try:
                dt = datetime.strptime(s, f)
                break
            except ValueError:
                pass
        if dt is None:
            return None
    if dt.tzinfo:
        dt = dt.astimezone(TZ).replace(tzinfo=None)
    return dt


def parse_hours(v):
    if v in (None, ""):
        return None
    s = str(v).strip()
    m = re.fullmatch(r"(\d+):(\d{1,2})(?::\d{1,2})?", s)
    if m:
        return int(m[1]) + int(m[2]) / 60
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def clock(dt):
    return dt.strftime("%I:%M %p").lstrip("0") if dt and (dt.hour or dt.minute) else ""


def clean(v, n=80):
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


# ---------------------------------------------------------------- build
def build_entries(rows, base):
    today = datetime.now(TZ).date()
    first = today - timedelta(days=DAYS - 1)
    out, skipped = [], 0
    for rec in rows:
        s, e = parse_dt(pick(rec, FIELD_MAP["start"])), parse_dt(pick(rec, FIELD_MAP["end"]))
        d = parse_dt(pick(rec, FIELD_MAP["date"]))
        d = d.date() if d else (s.date() if s else None)
        h = parse_hours(pick(rec, FIELD_MAP["hours"]))
        if h is None and s and e and e > s:
            h = (e - s).total_seconds() / 3600
        if d is None or h is None:
            skipped += 1
            continue
        if not (first <= d <= today):
            continue
        wo, url = clean(pick(rec, FIELD_MAP["wo_number"]), 40), ""
        m = re.match(r"\[#?(.*?)\]\((.*?)\)", wo)
        if m:
            wo, url = m[1], base + m[2].lstrip("/")
        wo = wo.lstrip("#")
        sr, wid = pick(rec, FIELD_MAP["sr_id"]), pick(rec, FIELD_MAP["wo_id"])
        if not url and sr and wid:
            url = f"{base}maintenance/service_requests/{sr}/work_orders/{wid}"
        out.append({
            "d": d.isoformat(),
            "t": clean(pick(rec, FIELD_MAP["tech"]), 60) or "Unassigned",
            "h": round(h, 2),
            "s": clock(s), "e": clock(e),
            "wo": wo, "url": url,
            "p": clean(pick(rec, FIELD_MAP["property"])),
            "u": clean(pick(rec, FIELD_MAP["unit"]), 40),
            "st": clean(pick(rec, FIELD_MAP["status"]), 30),
            "iss": clean(pick(rec, FIELD_MAP["issue"]), 60),
            "_s": s, "_e": e,
        })
    if skipped:
        log.info("Skipped %s entries with no date or hours", skipped)

    # Mark entries whose start/end times overlap another entry for the same tech that day.
    groups = {}
    for x in out:
        if x["_s"] and x["_e"] and x["_e"] > x["_s"]:
            groups.setdefault((x["t"], x["d"]), []).append(x)
    overlaps = 0
    for g in groups.values():
        g.sort(key=lambda x: x["_s"])
        latest = None
        for x in g:
            if latest and x["_s"] < latest["_e"]:
                x["ov"] = latest["ov"] = 1
            if latest is None or x["_e"] > latest["_e"]:
                latest = x
    for x in out:
        overlaps += x.get("ov", 0)
        del x["_s"], x["_e"]
    out.sort(key=lambda x: (x["t"], x["d"], x["s"]))
    log.info("Kept %s entries from %s to %s (%s overlapping)", len(out), first, today, overlaps)
    return out, first, today


def write_site(entries, first, last, base, out_dir):
    now = datetime.now(TZ)
    data = {
        "generated": now.strftime("%b %-d, %Y at %-I:%M %p").replace("AM", "a.m.").replace("PM", "p.m."),
        "first": first.isoformat(), "last": last.isoformat(),
        "appfolio": base, "entries": entries,
    }
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8")
    if "/*__DATA__*/null" not in html:
        raise SystemExit("hours_template.html is missing its data placeholder.")
    html = html.replace("/*__DATA__*/null", blob, 1)
    dest = Path(out_dir) / "hours"
    dest.mkdir(parents=True, exist_ok=True)
    tmp = dest / "index.html.tmp"
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(dest / "index.html")
    log.info("Wrote %s", dest / "index.html")


def inspect(cfg):
    name, r, _ = find_report(cfg, extra_filters())
    data = r.json()
    rows = data if isinstance(data, list) else data.get("results", [])
    if not rows:
        log.info("The report came back empty, so there are no fields to check.")
        return
    keys = set().union(*[set(x) for x in rows[:50]])
    log.info("Fields AppFolio returns: %s", ", ".join(sorted(keys)))
    for k, names in FIELD_MAP.items():
        log.info("  %-10s -> %s", k, which(keys, names) or "NOT FOUND")
    missing = [k for k in REQUIRED if not which(keys, FIELD_MAP[k])]
    if not which(keys, FIELD_MAP["date"]) and not which(keys, FIELD_MAP["start"]):
        missing.append("date (or start)")
    log.info("Dashboard fields with no match: %s", ", ".join(missing) or "none")
    if not (which(keys, FIELD_MAP["wo_id"]) and which(keys, FIELD_MAP["sr_id"])):
        log.info("No work order / service request IDs, so work order numbers won't be clickable.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="_site")
    ap.add_argument("--inspect", action="store_true")
    a = ap.parse_args()
    log.info("hours_dashboard %s", VERSION)
    cfg = load_cfg()
    if a.inspect:
        inspect(cfg)
        return
    base = f"https://{cfg['subdomain']}.appfolio.com/"
    report, first_resp, used = find_report(cfg, extra_filters())
    if "labor_performed_from" in used:
        rows = fetch_by_week(cfg, report, used)
    else:
        rows = fetch_all(cfg, first_resp)
    entries, first, last = build_entries(rows, base)
    write_site(entries, first, last, base, a.out)


if __name__ == "__main__":
    sys.exit(main())
