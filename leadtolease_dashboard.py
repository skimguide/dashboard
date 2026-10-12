"""
Lead to Lease dashboard (replaces Lead_to_Lease_Report.xlsm).

Pulls seven AppFolio reports (Showings twice: past and booked) through the Reports API (v2), counts them per property per
week, and writes _site/leadtolease/index.html for GitHub Pages. Only counts are published:
no prospect names, emails or phone numbers leave this script.

  Leads     Guest Card Inquiries, by Inquiry Received date (all sources and statuses)
  Showings  Showings with status Completed, by Showing Time
  Leases    Lease History, by Countersigned Date; renewals left out (COUNT_RENEWALS)
  Vacant    Unit Vacancy Detail, units listed right now
  Property  Property Directory (active properties)
  Groups    Property Group Directory: AppFolio property groups, for the group filter
  Agents    Leasing Funnel Performance: each leasing agent's AppFolio ID, for the links to their
            showing schedule (leasing_agents.csv, if present, adds or corrects names and IDs)
  Schedule  Showings booked from today through the next SCHEDULE_DAYS days, by agent
            (time, property, unit, type, status only; no prospect details)

  Rent      Historical Advertised Rent, every rent change by unit (rent with no lease term)

Weeks run Monday to Sunday: the last WEEKS full weeks, plus this week through yesterday
(nothing from today, so every day shown is complete). The page
lets you pick any start and end date inside that history.

Targets (same as the workbook): 6 leads to 1 showing, 5 showings to 1 lease, 30 leads to
1 lease, and at least 2 leads a day.

Usage:
    python leadtolease_dashboard.py --out out
    python leadtolease_dashboard.py --inspect      # list the fields AppFolio returns, build nothing
"""
import argparse
import csv
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests


# AppFolio answers 429 ("Retry later") when it gets too many requests in a short time.
def _with_retry(fn):
    def call(*args, **kwargs):
        waits = [15, 30, 60, 120, 240]
        while True:
            r = fn(*args, **kwargs)
            if r.status_code != 429 or not waits:
                return r
            ra = r.headers.get("Retry-After", "")
            wait = min(int(ra), 300) if ra.isdigit() else waits[0]
            waits.pop(0)
            logging.getLogger("appfolio").info("AppFolio is rate limiting (429); waiting %ss and retrying", wait)
            time.sleep(wait)
    return call


requests.post = _with_retry(requests.post)
requests.get = _with_retry(requests.get)

VERSION = "2026-10-09 lead to lease v6 (agent schedule links)"
HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "leadtolease_template.html"
AGENTS_CSV = HERE / "leasing_agents.csv"      # optional: name,leasing_id rows the report misses
FOLDER = "leadtolease"
TZ = ZoneInfo("America/Los_Angeles")

# ---------------------------------------------------------------- settings
WEEKS = int(os.environ.get("LEADTOLEASE_WEEKS", "26"))   # weeks of history the date picker can reach
TARGETS = {"leads_per_showing": 6, "showings_per_lease": 5, "leads_per_lease": 30, "leads_per_day": 2}
SCHEDULE_DAYS = 7              # days of booked showings on the Showing schedules tab
BOOKED = {"scheduled", "prospect confirmed", "confirmation pending"}   # still on the calendar
COUNT_RENEWALS = False         # the workbook counted renewals as leases; a renewal isn't from a lead

FIELDS = {
    "property":    ["property", "property_name"],
    "property_id": ["property_id"],
    "received":    ["inquiry_received", "received_on", "received", "inquiry_received_on", "inquiry_date", "created_at"],
    "source":      ["source", "lead_source", "inquiry_source"],
    "shown":       ["showing_time", "showing_date", "showing_at", "starts_at"],
    "status":      ["status", "showing_status"],
    "agent":       ["assigned_user", "assigned_to", "agent"],
    "show_unit":   ["showing_unit", "unit", "unit_name"],
    "tour_type":   ["type", "showing_type"],
    "state":       ["property_state", "state"],
    "signed":      ["countersigned_date", "countersigned_on", "countersigned_at"],
    "renewal":     ["renewal", "is_renewal"],
    "unit_status": ["unit_status", "status"],
    "short_name":  ["property_name", "name"],
    "unit":        ["unit", "unit_name"],
    "unit_id":     ["unit_id"],
    "rent":        ["advertised_rent_with_no_lease_term", "advertised_rent"],
    "approved":    ["advertised_rents_approved_at", "approved_at"],
    "expired":     ["advertised_rents_expired_at", "expired_at"],
}
RENT_REPORTS = ["historical_advertised_rent", "historical_advertised_rents", "advertised_rent_history"]

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("leadtolease")


# ---------------------------------------------------------------- API
def cfg():
    cid, sec = os.environ.get("APPFOLIO_CLIENT_ID"), os.environ.get("APPFOLIO_CLIENT_SECRET")
    if not (cid and sec):
        raise SystemExit("No AppFolio credentials. Set the APPFOLIO_CLIENT_ID / APPFOLIO_CLIENT_SECRET secrets.")
    return {"subdomain": os.environ.get("APPFOLIO_SUBDOMAIN", "guidemanagement").strip(),
            "auth": (cid.strip(), sec.strip())}


def check(r):
    if r.status_code == 401:
        raise SystemExit("AppFolio rejected the API credentials (401). Check the APPFOLIO_CLIENT_ID / APPFOLIO_CLIENT_SECRET secrets.")
    if r.status_code >= 400:
        raise SystemExit(f"AppFolio returned {r.status_code}: {r.text[:500]}")


def post(c, name, body):
    url = f"https://{c['subdomain']}.appfolio.com/api/v2/reports/{name}.json"
    r = requests.post(url, json={"paginate_results": True, **body}, auth=c["auth"], timeout=180)
    time.sleep(0.25)
    return r


def next_page_urls(c, current_url, nxt):
    if nxt.startswith("http"):
        return [nxt]
    root = f"https://{c['subdomain']}.appfolio.com/"
    tail = nxt.lstrip("/")
    tries = [urljoin(current_url, nxt), urljoin(root, nxt), root + "api/v2/" + tail, root + "api/" + tail]
    if tail.startswith("v2/"):
        tries.insert(0, root + "api/" + tail)
    return list(dict.fromkeys(tries))


def follow(c, r, nxt):
    for url in next_page_urls(c, r.url, nxt):
        attempt = requests.get(url, auth=c["auth"], timeout=180)
        time.sleep(0.25)
        if attempt.status_code != 404:
            check(attempt)
            return attempt
    return None


def read_all(c, r):
    """Rows from a response, following next pages. Returns (rows, complete)."""
    data = r.json()
    if isinstance(data, list):
        return data, True
    rows, nxt = list(data.get("results", [])), data.get("next_page_url")
    while nxt:
        got = follow(c, r, nxt)
        if got is None:
            return rows, False
        r, data = got, got.json()
        rows.extend(data.get("results", []))
        nxt = data.get("next_page_url")
    return rows, True


def fetch(c, name, body, soft=False):
    r = post(c, name, body)
    if soft and r.status_code in (400, 404, 422):
        return None
    check(r)
    rows, complete = read_all(c, r)
    if not complete:
        log.info("  note: AppFolio wouldn't send every page of %s; kept the first %s rows", name, len(rows))
    return rows


def fetch_dated(c, name, key, d1, d2, extra=None):
    """One report over a date range (filters {key}_from / {key}_to), split in half
    if AppFolio won't serve every page."""
    body = {**(extra or {}), f"{key}_from": d1.isoformat(), f"{key}_to": d2.isoformat()}
    r = post(c, name, body)
    check(r)
    rows, complete = read_all(c, r)
    if complete:
        return rows
    if d1 < d2:
        mid = d1 + (d2 - d1) // 2
        log.info("  %s %s to %s is more than AppFolio will page through; splitting it", name, d1, d2)
        return fetch_dated(c, name, key, d1, mid, extra) + fetch_dated(c, name, key, mid + timedelta(days=1), d2, extra)
    log.info("  %s on %s: AppFolio wouldn't send every page; kept the first %s rows", name, d1, len(rows))
    return rows


def by_week(c, name, key, first, last, extra=None):
    rows = []
    d = first
    while d <= last:
        e = min(d + timedelta(days=6), last)
        got = fetch_dated(c, name, key, d, e, extra)
        log.info("  %s %s to %s: %s rows", name, d, e, len(got))
        rows.extend(got)
        d = e + timedelta(days=1)
    return rows


# ---------------------------------------------------------------- helpers
def pick(rec, names):
    for n in names:
        if n in rec and rec[n] not in (None, ""):
            return rec[n]
    return ""


def norm(v):
    return re.sub(r"\s+", " ", str(v or "")).strip()


def to_date(v):
    """AppFolio sends dates as 2026-09-28, 09/28/2026, or '09/28/2026 at 08:28 AM'."""
    if not v:
        return None
    s = str(v).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if m:
        y = int(m[3])
        return date(y + 2000 if y < 100 else y, int(m[1]), int(m[2]))
    return None


def month_text(d):
    return d.strftime("%b %Y")


# ---------------------------------------------------------------- properties
class Properties:
    """Active properties, matched by property ID first and by name second."""

    def __init__(self, rows):
        self.props, self.by_name = {}, {}
        for r in rows:
            pid = str(pick(r, FIELDS["property_id"]) or "").strip()
            if not pid:
                continue
            full = norm(pick(r, ["property"]))
            short = norm(pick(r, FIELDS["short_name"])) or full.split(" - ")[0]
            state = norm(pick(r, FIELDS["state"])).upper()
            if not state:
                m = re.search(r",\s*([A-Z]{2})\s+\d{5}", full)
                state = m[1] if m else ""
            self.props[pid] = {"id": pid, "name": short, "state": state}
            for n in (full, short):
                if n:
                    self.by_name[n.lower()] = pid

    def find(self, rec):
        pid = str(pick(rec, FIELDS["property_id"]) or "").strip()
        if pid in self.props:
            return pid
        for n in FIELDS["property"]:
            v = norm(rec.get(n, "")).lower()
            if v and v in self.by_name:
                return self.by_name[v]
            if v and " - " in v and v.split(" - ")[0] in self.by_name:
                return self.by_name[v.split(" - ")[0]]
        return None


# ---------------------------------------------------------------- build
def week_list(end):
    """Monday-Sunday weeks through `end` (yesterday). The last one is partial unless end is a Sunday."""
    this_monday = end - timedelta(days=end.weekday())
    first = this_monday - timedelta(weeks=WEEKS)
    out = [(first + timedelta(weeks=i), first + timedelta(weeks=i, days=6)) for i in range(WEEKS)]
    out.append((this_monday, end))   # this week through yesterday
    return out


def week_index(weeks, d):
    if not d:
        return None
    for i, (a, b) in enumerate(weeks):
        if a <= d <= b:
            return i
    return None


def build(c, today):
    # Everything runs through yesterday, so every day shown is complete.
    last = today - timedelta(days=1)
    weeks = week_list(last)
    first = weeks[0][0]

    log.info("Property Directory")
    props = Properties(fetch(c, "property_directory", {"property_visibility": "active"}))
    log.info("  %s active properties", len(props.props))

    counts = {pid: {"leads": [0] * len(weeks), "showings": [0] * len(weeks), "leases": [0] * len(weeks),
                    "vacant": 0, "unrented": 0, "sources": {}, "vacant_units": []} for pid in props.props}
    unmatched = {"leads": 0, "showings": 0, "leases": 0, "vacant": 0}
    outside = {"leads": 0, "showings": 0, "leases": 0}

    # Leads
    log.info("Guest Card Inquiries (leads)")
    for r in by_week(c, "guest_card_inquiries", "received_on", first, last):
        i = week_index(weeks, to_date(pick(r, FIELDS["received"])))
        if i is None:
            outside["leads"] += 1
            continue
        pid = props.find(r)
        if not pid:
            unmatched["leads"] += 1
            continue
        counts[pid]["leads"][i] += 1
        src = norm(pick(r, FIELDS["source"]))[:40]
        if src and i < WEEKS:
            counts[pid]["sources"][src] = counts[pid]["sources"].get(src, 0) + 1

    # Showings (completed only), and showings per agent
    log.info("Showings")
    agents, statuses = {}, {}
    for r in by_week(c, "showings", "showing_date", first, last):
        status = norm(pick(r, FIELDS["status"]))
        statuses[status] = statuses.get(status, 0) + 1
        if status.lower() != "completed":
            continue
        i = week_index(weeks, to_date(pick(r, FIELDS["shown"])))
        if i is None:
            outside["showings"] += 1
            continue
        agent = norm(pick(r, FIELDS["agent"]))
        if agent:
            agents.setdefault(agent, [0] * len(weeks))[i] += 1
        pid = props.find(r)
        if not pid:
            unmatched["showings"] += 1
            continue
        counts[pid]["showings"][i] += 1
    log.info("  showing statuses: %s", statuses)

    # Leases, by countersigned date. Lease History filters by lease start month, so pull
    # leases starting from a few months before the first week to a year out.
    log.info("Lease History (leases)")
    lease_body = {"start_on_from": month_text(first - timedelta(days=120)),
                  "start_on_to": month_text(today + timedelta(days=365))}
    forced = os.environ.get("APPFOLIO_LEASE_FILTERS", "").strip()
    if forced:
        lease_body = json.loads(forced)
    leases = fetch(c, "lease_history", lease_body, soft=True)
    if leases is None:
        log.info("  lease_history didn't accept %s; pulling without a date range", lease_body)
        leases = fetch(c, "lease_history", {})
    renewals = 0
    for r in leases:
        i = week_index(weeks, to_date(pick(r, FIELDS["signed"])))
        if i is None:
            outside["leases"] += 1
            continue
        if not COUNT_RENEWALS and str(pick(r, FIELDS["renewal"])).strip().lower() in ("yes", "true", "1"):
            renewals += 1
            continue
        pid = props.find(r)
        if not pid:
            unmatched["leases"] += 1
            continue
        counts[pid]["leases"][i] += 1
    log.info("  %s rows; %s renewals in range left out", len(leases), renewals)

    # Vacancies right now
    log.info("Unit Vacancy Detail")
    for r in fetch(c, "unit_vacancy", {"unit_visibility": "active", "non_revenue_units": "false"}):
        pid = props.find(r)
        if not pid:
            unmatched["vacant"] += 1
            continue
        counts[pid]["vacant"] += 1
        counts[pid]["vacant_units"].append(norm(pick(r, FIELDS["unit"])))
        if "unrented" in norm(pick(r, FIELDS["unit_status"])).lower():
            counts[pid]["unrented"] += 1

    rents = rent_history(c, props, weeks)
    groups = property_groups(c, props)

    log.info("Not matched to an active property: %s", unmatched)
    log.info("Outside the %s weeks (ignored): %s", WEEKS, outside)

    out_props = []
    for pid, p in props.props.items():
        k = counts[pid]
        top = sorted(k["sources"].items(), key=lambda x: -x[1])[:5]
        out_props.append({"id": pid, "name": p["name"], "vacant": k["vacant"], "unrented": k["unrented"],
                          "leads": k["leads"], "showings": k["showings"], "leases": k["leases"],
                          "sources": top, "vacant_units": sorted(k["vacant_units"]),
                          "rent": rents.get(pid, {}).get("avg", [None] * len(weeks)),
                          "units": rents.get(pid, {}).get("units", [])})
    out_props.sort(key=lambda p: p["name"].lower())

    schedule = booked_showings(c, props, today)
    ids = agent_ids(c, today)
    # "(Hidden)" is how AppFolio marks deactivated users; leave them off unless they still have bookings
    booked_names = {x[0] for x in schedule["items"] if x[0]}
    names = {n for n in set(agents) | set(ids) if "(hidden)" not in n.lower()} | booked_names
    agent_list = [{"name": n, "id": ids.get(n, ""), "showings": agents.get(n, [0] * len(weeks))}
                  for n in sorted(names, key=str.lower)]
    log.info("Agents: %s, %s with an AppFolio ID for the schedule link", len(agent_list), sum(1 for a in agent_list if a["id"]))
    partial = (weeks[-1][1] - weeks[-1][0]).days < 6
    return {"weeks": [[a.isoformat(), b.isoformat()] for a, b in weeks],
            "full_weeks": len(weeks) - 1 if partial else len(weeks), "partial": partial,
            "targets": TARGETS, "renewals_counted": COUNT_RENEWALS,
            "properties": out_props, "agents": agent_list, "groups": groups,
            "schedule": schedule}


def rent_history(c, props, weeks):
    """Per property: the average advertised rent at the end of each week (every unit with an
    advertised rent then), and each unit's rent changes during the history."""
    rows = None
    for name in RENT_REPORTS:
        rows = fetch(c, name, {"unit_visibility": "active"}, soft=True)
        if rows is not None:
            log.info("Historical Advertised Rent (%s): %s rows", name, len(rows))
            break
    if rows is None:
        log.info("Historical Advertised Rent: no report name worked (%s); rent left blank", RENT_REPORTS)
        return {}
    first = weeks[0][0]
    by_unit = {}
    for r in rows:
        pid = props.find(r)
        rent = pick(r, FIELDS["rent"])
        try:
            rent = float(str(rent).replace(",", "").replace("$", ""))
        except ValueError:
            continue
        a = to_date(pick(r, FIELDS["approved"]))
        if not pid or rent <= 0 or not a:
            continue
        uid = str(pick(r, FIELDS["unit_id"]) or pick(r, FIELDS["unit"]))
        by_unit.setdefault((pid, uid), {"unit": norm(pick(r, FIELDS["unit"])), "recs": []})["recs"].append(
            (a, to_date(pick(r, FIELDS["expired"])), rent))
    out = {}
    for (pid, _), u in by_unit.items():
        recs = sorted(u["recs"], key=lambda x: x[0])
        def at(d):
            live = [x for x in recs if x[0] <= d and (x[1] is None or x[1] > d)]
            return live[-1][2] if live else None
        p = out.setdefault(pid, {"sums": [[0.0, 0] for _ in weeks], "units": []})
        for i, (_, end) in enumerate(weeks):
            v = at(end)
            if v is not None:
                p["sums"][i][0] += v
                p["sums"][i][1] += 1
        # Rent in effect at the start of the history, then each change after it
        # (re-approvals at the same rent aren't changes, so they're dropped)
        start = at(first - timedelta(days=1))
        hist, prev = [], start
        for x in recs:
            if x[0] >= first and x[2] != prev:
                hist.append([x[0].isoformat(), x[2]])
            if x[0] >= first:
                prev = x[2]
        if hist or start is not None:
            p["units"].append({"u": u["unit"] or "(no unit)", "start": start, "changes": hist})
    for p in out.values():
        p["avg"] = [round(s / n) if n else None for s, n in p["sums"]]
        p["units"].sort(key=lambda x: x["u"])
        del p["sums"]
    log.info("  rent history for %s properties", len(out))
    return out


def property_groups(c, props):
    """AppFolio property groups -> active property IDs. Groups of one property are left out,
    since searching for the property does the same thing."""
    rows = fetch(c, "property_group_directory", {"property_visibility": "active"}, soft=True)
    if rows is None:
        log.info("Property Group Directory: not available; no group filter")
        return []
    groups = {}
    for r in rows:
        name = norm(pick(r, ["property_group_name", "property_group", "group_name"]))
        pid = props.find(r)
        if name and pid:
            groups.setdefault(name, set()).add(pid)
    out = sorted([[n, sorted(ids)] for n, ids in groups.items() if len(ids) > 1], key=lambda g: g[0].lower())
    log.info("Property Group Directory: %s groups with more than one active property", len(out))
    return out


def local_time(v, state):
    """Showing time in the property's own time zone. AppFolio sends UTC (2026-10-12T21:00:00Z)
    or local text (10/12/2026 at 02:00 PM)."""
    s = str(v or "").strip()
    tz = ZoneInfo("America/Boise") if state == "ID" else TZ
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(:\d{2})?(Z|[+-]\d{2}:?\d{2})?", s)
    if m:
        d = datetime.fromisoformat(f"{m[1]}T{m[2]}:00" + ("+00:00" if m[4] == "Z" else (m[4] or "")))
        if d.tzinfo:
            d = d.astimezone(tz)
        return d.strftime("%Y-%m-%dT%H:%M")
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+at)?\s+(\d{1,2}):(\d{2})\s*([AP]M)", s, re.I)
    if m:
        h = int(m[4]) % 12 + (12 if m[6].upper() == "PM" else 0)
        return f"{int(m[3]):04d}-{int(m[1]):02d}-{int(m[2]):02d}T{h:02d}:{m[5]}"
    return None


def agent_ids(c, today):
    """Leasing agent name -> AppFolio user ID, from the Leasing Funnel Performance report
    (Assigned Inquiry Owner and its ID), so the schedule links keep up with staff changes."""
    out = {}
    d1 = today - timedelta(days=90)
    for body in ({"inquiry_date_from": d1.isoformat(), "inquiry_date_to": today.isoformat()}, {}):
        rows = fetch(c, "leasing_funnel_performance", body, soft=True)
        if rows:
            break
    for r in rows or []:
        name = norm(pick(r, ["assigned_inquiry_owner", "assigned_user", "leasing_agent"]))
        aid = str(pick(r, ["assigned_inquiry_owner_id", "assigned_user_id"]) or "").strip()
        if aid.endswith(".0"):
            aid = aid[:-2]
        if name and aid and name.lower() != "unassigned":
            out[name] = aid
    log.info("Leasing Funnel Performance: %s", f"{len(out)} agents with IDs" if rows else "not available")
    if AGENTS_CSV.exists():
        with AGENTS_CSV.open(newline="") as f:
            for row in csv.DictReader(f):
                name, aid = norm(row.get("name")), norm(row.get("leasing_id"))
                if name and aid:
                    out[name] = aid
    return out


def booked_showings(c, props, today):
    """Showings still on the calendar from today through SCHEDULE_DAYS days out."""
    end = today + timedelta(days=SCHEDULE_DAYS - 1)
    log.info("Showings booked %s to %s", today, end)
    items, statuses = [], {}
    for r in fetch_dated(c, "showings", "showing_date", today, end):
        status = norm(pick(r, FIELDS["status"]))
        statuses[status] = statuses.get(status, 0) + 1
        if status.lower() not in BOOKED:
            continue
        pid = props.find(r)
        when = local_time(pick(r, FIELDS["shown"]), props.props.get(pid, {}).get("state", ""))
        if not pid or not when or not (today.isoformat() <= when[:10] <= end.isoformat()):
            continue
        items.append([norm(pick(r, FIELDS["agent"])), pid, norm(pick(r, FIELDS["show_unit"]))[:20],
                      when, norm(pick(r, FIELDS["tour_type"]))[:20], status])
    items.sort(key=lambda x: (x[3], x[0]))
    log.info("  statuses: %s; %s still booked", statuses, len(items))
    return {"from": today.isoformat(), "days": SCHEDULE_DAYS, "items": items}


# ---------------------------------------------------------------- output
def write_page(data, out_dir):
    page = TEMPLATE.read_text().replace("/*__DATA__*/null", json.dumps(data).replace("</", "<\\/"))
    dest = Path(out_dir) / FOLDER
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "index.html").write_text(page)
    log.info("Wrote %s", dest / "index.html")


def inspect(c, today):
    d1 = today - timedelta(days=6)
    tries = [
        ("property_directory", {"property_visibility": "active"}),
        ("guest_card_inquiries", {"received_on_from": d1.isoformat(), "received_on_to": today.isoformat()}),
        ("showings", {"showing_date_from": d1.isoformat(), "showing_date_to": today.isoformat()}),
        ("lease_history", {"start_on_from": month_text(today - timedelta(days=60)), "start_on_to": month_text(today)}),
        ("unit_vacancy", {"unit_visibility": "active"}),
        ("property_group_directory", {"property_visibility": "active"}),
        ("leasing_funnel_performance", {}),
    ] + [(n, {"unit_visibility": "active"}) for n in RENT_REPORTS]
    for name, body in tries:
        rows = fetch(c, name, body, soft=True)
        if rows is None:
            log.info("%s: not accepted with %s", name, body)
            continue
        log.info("%s: %s rows; fields: %s", name, len(rows), sorted(rows[0]) if rows else "-")
        if rows:
            sample = {k: rows[0][k] for k in rows[0] if any(x in k for x in ("date", "time", "received", "_on", "status", "renewal", "source", "property"))}
            log.info("  sample: %s", sample)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="_site")
    ap.add_argument("--inspect", action="store_true")
    a = ap.parse_args()
    log.info("leadtolease_dashboard %s", VERSION)
    c = cfg()
    now = datetime.now(TZ)
    if a.inspect:
        inspect(c, now.date())
        return
    data = build(c, now.date())
    data.update({"generated": now.strftime("%b %-d at %-I:%M %p").replace("AM", "a.m.").replace("PM", "p.m."),
                 "today": now.date().isoformat(),
                 "base_url": f"https://{c['subdomain']}.appfolio.com/"})
    write_page(data, a.out)


if __name__ == "__main__":
    main()
