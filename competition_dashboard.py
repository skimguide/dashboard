"""
Competition dashboards: Work Order Competition 2026 and Renewal Competition 2026.

Pulls from the AppFolio Reports API (v2), applies the competition rules, and writes
two pages for GitHub Pages: _site/workordercomp2026/ and _site/renewalcomp2026/.

Work Order Competition (Oct 8-31, 2026)
  - Counts work orders completed Oct 8-31 with status Completed, Ready to Bill,
    or Completed No Need To Bill (types Unit Turn, Resident, Internal).
  - Excludes work orders whose description says "emergency call", work orders
    created by a maintenance tech, and anything listed in disqualified.txt.
  - Work orders created by Tenant, System, or office staff count.
  - Unit turns (type Unit Turn, or "unit turn" in the description) are worth 3.
  - Shared work orders are split by the worked hours each tech logged
    (Work Order Labor Summary report); if no hours are logged, split evenly and flagged.

Renewal Competition (through Dec 31, 2026)
  - Leases starting Nov 2026 - Feb 2027, statuses Renewed and Pending.
  - A renewal counts if it is 12-month and countersigned Oct 5 - Dec 31, 2026.
  - Like the saved report's column filters, only 12-month leases are included, and
    leases countersigned before Oct 5 are left out entirely.
  - Renewal % = counted renewals / those leases.
  - Each property counts toward its site manager (the property manager on the board).

Usage:
    python competition_dashboard.py --out _site                    # both pages
    python competition_dashboard.py --only workorders --out out    # just one page
    python competition_dashboard.py --only renewals --out out
    python competition_dashboard.py --inspect     # list fields and status codes, build nothing
"""
import argparse
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


# AppFolio answers 429 ("Retry later") when it gets too many requests in a short time.
# Wait and try again instead of failing the whole page.
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

VERSION = "2026-10-09 competition v8 (correct work order codes)"
HERE = Path(__file__).resolve().parent
WO_TEMPLATE = HERE / "workordercomp_template.html"
RN_TEMPLATE = HERE / "renewalcomp_template.html"
WO_FOLDER, RN_FOLDER = "workordercomp2026", "renewalcomp2026"
DQ_PATH = HERE / "disqualified.txt"
TZ = ZoneInfo("America/Los_Angeles")

# ---------------------------------------------------------------- competition rules
WO_START, WO_END = date(2026, 10, 8), date(2026, 10, 31)
WO_PRIZES = ["$500", "$250", "$100"]
WO_COUNT_STATUSES = {"completed", "ready to bill", "completed no need to bill"}
WO_COUNT_TYPES = {"unit turn", "resident", "internal"}
EMERGENCY_TEXT = "emergency call"
UNIT_TURN_POINTS = 3

RN_LEASE_FROM, RN_LEASE_TO = date(2026, 11, 1), date(2027, 2, 28)
RN_SIGNED_FROM, RN_END = date(2026, 10, 5), date(2026, 12, 31)   # countersigned Oct 5 - Dec 31
RN_STATUSES = {"renewed", "pending"}
RN_PRIZE = "$1,000"

# Status codes AppFolio uses on the work order report that we already know are open.
OPEN_STATUS_CODES = {"0", "1", "2", "3", "6", "9"}
# From AppFolio's Work Order report form (filters[work_order_statuses], filters[status_date]):
# statuses 4 = Completed, 7 = Completed No Need To Bill, 12 = Ready to Bill;
# status date 4 = Completed On (0 = Created On, 3 = Scheduled Start, 8 = Work Done On).
COUNT_STATUS_CODES = ["4", "7", "12"]
COMPLETED_ON_CODE = "4"

WO_FIELDS = {
    "number":   ["work_order_number", "work_order", "wo_number"],
    "property": ["property_name", "property"],
    "unit":     ["unit_name", "unit", "unit_address"],
    "type":     ["work_order_type", "type"],
    "desc":     ["job_description", "description", "work_order_description"],
    "status":   ["status", "work_order_status"],
    "created":  ["created_at", "created_on", "created"],
    "completed": ["completed_on", "completed_at", "completed_date", "work_completed_on"],
    "created_by": ["created_by", "created_by_name", "created_by_user"],
    "assigned": ["assigned_user", "assigned_users", "assigned_to", "maintenance_tech"],
    "wo_id":    ["work_order_id"],
    "sr_id":    ["service_request_id"],
}
LABOR_FIELDS = {
    "number": ["work_order_number", "work_order"],
    "tech":   ["maintenance_tech", "maintenance_technician", "technician"],
    "hours":  ["worked_hours", "hours_worked"],      # "hours" is billable hours; don't use it
}
# Renewal Summary's API fields (report columns UnitName, PropertyName, PropertyId, LeaseStart,
# Status, Term, CountersignedDate, TenantTransfer). It has no site manager column; that
# comes from the Property Directory report (SiteManagerName), matched by property.
RN_FIELDS = {
    "unit":      ["unit_name", "unit"],
    "property":  ["property_name", "property"],
    "property_id": ["property_id"],
    "transfer":  ["tenant_transfer"],
    "start":     ["lease_start", "lease_start_date", "start_date"],
    "status":    ["status", "renewal_status"],
    "term":      ["term", "lease_term"],
    "signed":    ["countersigned_date", "countersigned_on", "countersigned_at"],
}
PD_FIELDS = {
    "property_id": ["property_id"],
    "property":    ["property_name", "property"],
    "manager":     ["site_manager_name"],
}

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("competition")


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


def report(c, name, body, soft=False):
    """One report request. Pagination is unreliable on AppFolio, so callers keep each
    request small enough for one page. soft=True returns None on a 400 instead of stopping."""
    url = f"https://{c['subdomain']}.appfolio.com/api/v2/reports/{name}.json"
    r = requests.post(url, json={"paginate_results": True, **body}, auth=c["auth"], timeout=180)
    time.sleep(0.25)
    if soft and r.status_code in (400, 422):
        return None
    check(r)
    data = r.json()
    if isinstance(data, list):
        return data
    if data.get("next_page_url"):
        log.info("  note: %s returned more than one page for %s; only the first page was read", name, body)
    return data.get("results", [])


def pick(rec, names):
    for n in names:
        if n in rec and rec[n] not in (None, ""):
            return rec[n]
    return ""


def clean(v, n=80):
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def norm_name(v):
    return re.sub(r"\s+", " ", str(v or "")).strip()


def to_date(v):
    if not v:
        return None
    s = str(v).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    for fmt in ("%m/%d/%Y", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def hours(v):
    if v in (None, ""):
        return 0.0
    s = str(v).strip()
    m = re.fullmatch(r"(\d+):(\d{1,2})(?::\d{1,2})?", s)
    if m:
        return int(m[1]) + int(m[2]) / 60
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return 0.0


def mdy(d):
    return d.strftime("%m/%d/%Y")


def weeks(d1, d2):
    d = d1
    while d <= d2:
        e = min(d + timedelta(days=6), d2)
        yield d, e
        d = e + timedelta(days=1)


# ---------------------------------------------------------------- work orders: discovery
def wo_body(status_code, date_code, d1, d2):
    return {"work_order_statuses": str(status_code), "status_date": str(date_code),
            "property_corporate_entity_combination": "properties_and_corporate_entities",
            "float_status_date_range": "0",
            "status_date_range_from": mdy(d1), "status_date_range_to": mdy(d2)}


# ---------------------------------------------------------------- work orders: scoring
def fetch_work_orders(c, today):
    codes = COUNT_STATUS_CODES
    date_code = COMPLETED_ON_CODE
    log.info("Status codes %s, completed between %s and %s", ",".join(codes), WO_START, min(WO_END, today))
    rows, seen = [], set()
    end = min(WO_END, today)
    for d1, d2 in weeks(WO_START, end):
        n = 0
        for sc in codes:
            for r in report(c, "work_order", wo_body(sc, date_code, d1, d2)):
                key = pick(r, WO_FIELDS["wo_id"]) or pick(r, WO_FIELDS["number"])
                if key not in seen:
                    seen.add(key)
                    rows.append(r)
                    n += 1
        log.info("Completed %s to %s: %s work orders", d1, d2, n)
    return rows


def fetch_labor(c, numbers, earliest, today):
    """Worked hours per tech for the given work order numbers."""
    out = {}
    if not numbers:
        return out, set()
    techs = set()
    # Hours on competition work orders are logged around when they're completed, so
    # start a week before the competition instead of going back months.
    start = max(earliest, WO_START - timedelta(days=7))
    for d1, d2 in weeks(start, today):
        rows = report(c, "work_order_labor_summary",
                      {"labor_performed_from": d1.isoformat(), "labor_performed_to": d2.isoformat()})
        for r in rows:
            t = norm_name(pick(r, LABOR_FIELDS["tech"]))
            if t:
                techs.add(t)
            num = clean(pick(r, LABOR_FIELDS["number"]), 40).lstrip("#")
            if num in numbers and t:
                out.setdefault(num, {})
                out[num][t] = out[num].get(t, 0) + hours(pick(r, LABOR_FIELDS["hours"]))
    log.info("Labor: hours found for %s of %s qualifying work orders", len(out), len(numbers))
    return out, techs


def split_names(v):
    return [norm_name(x) for x in re.split(r",|;|\n", str(v or "")) if norm_name(x)]


def load_dq():
    if not DQ_PATH.exists():
        return {}
    out = {}
    for line in DQ_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        num, _, why = line.partition(" ")
        out[num.lstrip("#")] = why.strip(" -:") or "Disqualified on review"
    return out


def score_work_orders(c, today, base_url):
    raw = fetch_work_orders(c, today)
    wos = []
    for r in raw:
        num = clean(pick(r, WO_FIELDS["number"]), 40).lstrip("#")
        sr, wid = pick(r, WO_FIELDS["sr_id"]), pick(r, WO_FIELDS["wo_id"])
        typ = clean(pick(r, WO_FIELDS["type"]), 40)
        desc = str(pick(r, WO_FIELDS["desc"]) or "")
        wos.append({
            "num": num,
            "url": f"{base_url}maintenance/service_requests/{sr}/work_orders/{wid}" if sr and wid else "",
            "prop": clean(pick(r, WO_FIELDS["property"]), 60),
            "unit": clean(pick(r, WO_FIELDS["unit"]), 30),
            "type": typ,
            "status": clean(pick(r, WO_FIELDS["status"]), 40),
            "done": to_date(pick(r, WO_FIELDS["completed"])),
            "created": to_date(pick(r, WO_FIELDS["created"])),
            "by": norm_name(pick(r, WO_FIELDS["created_by"])),
            "techs": split_names(pick(r, WO_FIELDS["assigned"])),
            "turn": typ.lower() == "unit turn" or "unit turn" in desc.lower(),
            "emergency": EMERGENCY_TEXT in desc.lower(),
        })

    # Within the competition window and rules on status/type
    wos = [w for w in wos if w["done"] and WO_START <= w["done"] <= WO_END
           and w["status"].lower() in WO_COUNT_STATUSES and w["type"].lower() in WO_COUNT_TYPES]

    # Labor for every qualifying work order, back to the oldest one's created date (max 180 days)
    earliest = min([w["created"] or WO_START for w in wos] or [WO_START])
    labor, labor_techs = fetch_labor(c, {w["num"] for w in wos}, earliest, today)

    # Who counts as maintenance: anyone assigned to a work order or logging labor,
    # plus MAINTENANCE_STAFF, minus NOT_MAINTENANCE (repository variables, comma-separated).
    staff = {t for w in wos for t in w["techs"]} | labor_techs
    staff |= {norm_name(x) for x in os.environ.get("MAINTENANCE_STAFF", "").split(",") if x.strip()}
    staff -= {norm_name(x) for x in os.environ.get("NOT_MAINTENANCE", "").split(",") if x.strip()}
    staff_l = {s.lower() for s in staff}

    dq = load_dq()
    counted, excluded, credits, flags = [], [], {}, []
    for w in sorted(wos, key=lambda w: (w["done"], w["num"])):
        reason = None
        if w["num"] in dq:
            reason = dq[w["num"]]
        elif w["emergency"]:
            reason = "Emergency call"
        elif w["by"].lower() in staff_l:
            reason = f"Created by maintenance ({w['by']})"
        elif not w["techs"] and not labor.get(w["num"]):
            reason = "No technician assigned"
        if reason:
            excluded.append({**w, "why": reason})
            continue
        pts = UNIT_TURN_POINTS if w["turn"] else 1
        logged = {t: h for t, h in labor.get(w["num"], {}).items() if h > 0}
        total = sum(logged.values())
        if len(w["techs"]) > 1 or (logged and set(logged) != set(w["techs"])):
            if total > 0:
                shares = {t: h / total for t, h in logged.items()}
                basis = "hours"
            else:
                shares = {t: 1 / len(w["techs"]) for t in w["techs"]}
                basis = "even"
                flags.append(w["num"])
        else:
            shares = {(w["techs"] or list(logged))[0]: 1.0}
            basis = "single"
        split = []
        for t, s in shares.items():
            pts_t = pts * s
            credits.setdefault(t, {"tech": t, "score": 0.0, "count": 0, "turns": 0, "wos": []})
            cr = credits[t]
            cr["score"] += pts_t
            cr["count"] += 1
            cr["turns"] += 1 if w["turn"] else 0
            cr["wos"].append(w["num"])
            split.append({"t": t, "pts": round(pts_t, 2), "h": round(logged.get(t, 0), 2)})
        counted.append({**w, "pts": pts, "basis": basis, "split": split})

    board = sorted(credits.values(), key=lambda x: (-x["score"], x["tech"]))
    for x in board:
        x["score"] = round(x["score"], 2)
    log.info("Work orders: %s counted, %s excluded, %s techs on the board, %s shared with no hours",
             len(counted), len(excluded), len(board), len(flags))
    return {"board": board, "counted": counted, "excluded": excluded, "staff": sorted(staff)}


# ---------------------------------------------------------------- renewals
def fetch_renewals(c):
    """Renewal Summary with the same filters as the saved report: active units, leases starting
    Nov 2026 - Feb 2027, no tenant transfers, no non-revenue units. AppFolio's API doesn't say
    which month format it wants for the lease range, so try each and keep the one it honors.
    Rows are filtered again here either way."""
    base = {"unit_visibility": "active", "float_start_on": "0",
            "include_tenant_transfers": "false", "non_revenue_units": "false"}
    forced = os.environ.get("APPFOLIO_RENEWAL_FILTERS", "").strip()
    ranges = [json.loads(forced)] if forced else [
        {"start_on_from": "11/2026", "start_on_to": "02/2027"},
        {"start_on_from": "Nov 2026", "start_on_to": "Feb 2027"},
        {"start_on_from": "2026-11", "start_on_to": "2027-02"},
        {"start_on_from": "11/01/2026", "start_on_to": "02/28/2027"},
    ]
    in_range = lambda r: (d := to_date(pick(r, RN_FIELDS["start"]))) and RN_LEASE_FROM <= d <= RN_LEASE_TO
    best = None
    for rng in ranges:
        rows = report(c, "renewal_summary", {**base, **rng}, soft=True)
        if rows is None:
            log.info("Renewal filters %s: not accepted", rng)
            continue
        hits = sum(1 for r in rows if in_range(r))
        log.info("Renewal filters %s: %s rows, %s starting Nov-Feb", rng, len(rows), hits)
        if best is None or (hits and len(rows) < len(best)):
            best = rows
        if rows and hits == len(rows):
            log.info("Lease range format works: set repository variable APPFOLIO_RENEWAL_FILTERS=%s to skip this check",
                     json.dumps(rng))
            break
    return best or []


def fetch_site_managers(c):
    """Property -> site manager, from the Property Directory report."""
    rows = report(c, "property_directory", {"property_visibility": "active"})
    by_id, by_name = {}, {}
    for r in rows:
        mgr = norm_name(pick(r, PD_FIELDS["manager"]))
        pid = str(pick(r, PD_FIELDS["property_id"]) or "")
        name = norm_name(pick(r, PD_FIELDS["property"])).lower()
        if pid:
            by_id[pid] = mgr
        if name:
            by_name[name] = mgr
    log.info("Property Directory: %s properties, %s with a site manager",
             len(rows), sum(1 for v in by_id.values() if v) or sum(1 for v in by_name.values() if v))
    return by_id, by_name


def score_renewals(c):
    raw = fetch_renewals(c)
    by_id, by_name = fetch_site_managers(c)
    managers, statuses, transfers = {}, {}, 0
    skipped = {"not 12-month": 0, "countersigned before Oct 5": 0}
    for r in raw:
        start = to_date(pick(r, RN_FIELDS["start"]))
        status = clean(pick(r, RN_FIELDS["status"]), 30)
        statuses[status] = statuses.get(status, 0) + 1
        if not (start and RN_LEASE_FROM <= start <= RN_LEASE_TO) or status.lower() not in RN_STATUSES:
            continue
        if str(pick(r, RN_FIELDS["transfer"])).strip().lower() in ("yes", "true", "1"):
            transfers += 1
            continue
        term = clean(pick(r, RN_FIELDS["term"]), 30)
        signed = to_date(pick(r, RN_FIELDS["signed"]))
        # Same as the saved report's column filters: Term contains "12 month", and
        # Countersigned Date after the start or blank.
        if "12 month" not in term.lower():
            skipped["not 12-month"] += 1
            continue
        if signed is not None and signed < RN_SIGNED_FROM:
            skipped["countersigned before Oct 5"] += 1
            continue
        prop = clean(pick(r, RN_FIELDS["property"]), 60)
        pid = str(pick(r, RN_FIELDS["property_id"]) or "")
        mgr = by_id.get(pid) or by_name.get(norm_name(prop).lower()) or "No site manager listed"
        t = managers.setdefault(mgr, {"manager": mgr, "props": set(), "total": 0, "won": 0, "renewals": []})
        t["props"].add(prop)
        t["total"] += 1
        counts = status.lower() == "renewed" and signed is not None and signed <= RN_END
        if status.lower() == "renewed":
            t["renewals"].append({"unit": clean(pick(r, RN_FIELDS["unit"]), 20), "prop": prop,
                                  "start": start.isoformat(), "signed": signed.isoformat() if signed else "",
                                  "term": term, "counts": counts})
        if counts:
            t["won"] += 1
    log.info("Renewal statuses from AppFolio: %s", statuses)
    if transfers:
        log.info("Skipped %s tenant transfers", transfers)
    log.info("Left out (saved report's column filters): %s", skipped)
    out = []
    for t in managers.values():
        t["props"] = sorted(t["props"])
        t["pct"] = round(100 * t["won"] / t["total"], 1) if t["total"] else 0
        t["renewals"].sort(key=lambda x: x["signed"])
        out.append(t)
    log.info("Renewals: %s property managers, %s leases, %s counted renewals",
             len(out), sum(t["total"] for t in out), sum(t["won"] for t in out))
    return {"managers": out}


# ---------------------------------------------------------------- output
def jsonable(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, set):
        return sorted(o)
    raise TypeError(type(o))


def write_page(template, folder, data, out_dir):
    page = template.read_text().replace(
        "/*__DATA__*/null", json.dumps(data, default=jsonable).replace("</", "<\\/"))
    dest = Path(out_dir) / folder
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "index.html").write_text(page)
    log.info("Wrote %s", dest / "index.html")


def inspect(c, today):
    for name, body in [("work_order", wo_body("5", 0, today - timedelta(days=7), today)),
                       ("work_order_labor_summary", {"labor_performed_from": (today - timedelta(days=3)).isoformat(),
                                                     "labor_performed_to": today.isoformat()}),
                       ("renewal_summary", {})]:
        rows = report(c, name, body, soft=True) or []
        log.info("%s: %s rows; fields: %s", name, len(rows), sorted(rows[0]) if rows else "-")
    log.info("Work order status codes:")
    for code in COUNT_STATUS_CODES:
        rows = report(c, "work_order", wo_body(code, 0, today - timedelta(days=21), today), soft=True)
        log.info("  %s -> %s", code, sorted({clean(pick(r, WO_FIELDS["status"])) for r in rows or []}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="_site")
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--only", choices=["workorders", "renewals"], help="build just one page")
    a = ap.parse_args()
    log.info("competition_dashboard %s", VERSION)
    c = cfg()
    now = datetime.now(TZ)
    today = now.date()
    if a.inspect:
        inspect(c, today)
        return
    base_url = f"https://{c['subdomain']}.appfolio.com/"
    stamp = {"generated": now.strftime("%b %-d at %-I:%M %p").replace("AM", "a.m.").replace("PM", "p.m."),
             "today": today.isoformat()}
    failed = []
    # Build each page on its own, so a problem with one doesn't stop the other.
    if a.only != "renewals":
      try:
        wo = score_work_orders(c, today, base_url)
        wo.pop("staff", None)
        write_page(WO_TEMPLATE, WO_FOLDER, {**stamp, "wo": {**wo, "start": WO_START.isoformat(),
                   "end": WO_END.isoformat(), "prizes": WO_PRIZES}}, a.out)
      except (Exception, SystemExit) as e:
        log.error("Work order competition page failed: %s", e)
        failed.append("work orders")
    if a.only != "workorders":
      try:
        rn = score_renewals(c)
        write_page(RN_TEMPLATE, RN_FOLDER, {**stamp, "rn": {**rn, "end": RN_END.isoformat(), "prize": RN_PRIZE}}, a.out)
      except (Exception, SystemExit) as e:
        log.error("Renewal competition page failed: %s", e)
        failed.append("renewals")
    if failed:
        raise SystemExit("Failed: " + ", ".join(failed))

if __name__ == "__main__":
    main()
