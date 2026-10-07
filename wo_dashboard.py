"""
Open Work Orders dashboard builder.

Pulls work orders from the AppFolio Reports API (v2), keeps the open ones, strips
phone numbers / emails / access codes / caller details from descriptions, and
writes the dashboard to _site/index.html for GitHub Pages.

Credentials come from the APPFOLIO_CLIENT_ID and APPFOLIO_CLIENT_SECRET
environment variables (GitHub secrets), or from config.ini when run locally.

Usage:
    python wo_dashboard.py              # build _site/index.html
    python wo_dashboard.py --inspect    # list the field names AppFolio returns, build nothing
"""
import argparse
import configparser
import csv
import io
import json
import logging
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.ini"
TEMPLATE_PATH = HERE / "dashboard_template.html"

# AppFolio's code for each status (from the Work Order report's Status filter).
# The dashboard pulls and shows only these.
STATUS_CODES = {
    "0": "New",
    "1": "Estimate Requested",
    "2": "Estimated",
    "9": "Assigned",
    "3": "Scheduled",
    "6": "Waiting",
}
SHOW_STATUSES = {name.lower() for name in STATUS_CODES.values()}

# Each dashboard column -> AppFolio field names to try, in order. Run --inspect once
# and adjust these if your report uses different names.
FIELD_MAP = {
    "WORK ORDER":      ["work_order_number", "work_order", "wo_number"],
    "PROPERTY":        ["property_name", "property"],
    "UNIT":            ["unit_address", "unit_name", "unit"],
    "PRIORITY":        ["priority"],
    "TYPE":            ["work_order_type", "type"],
    "DESCRIPTION":     ["job_description", "description", "work_order_description"],
    "STATUS":          ["status", "work_order_status"],
    "VENDOR":          ["vendor", "vendor_name"],
    "CREATED":         ["created_at", "created_on", "created"],
    "ASSIGNED TO":     ["assigned_user", "assigned_to", "maintenance_tech"],
    "SCHEDULED START": ["scheduled_start", "scheduled_start_date"],
}
ID_FIELDS = {
    "service_request_id": ["service_request_id"],
    "work_order_id":      ["work_order_id", "id"],
}
COLUMNS = list(FIELD_MAP)

log = logging.getLogger("wo")


# ---------------------------------------------------------------- config / api
def load_config():
    """Read settings from environment variables (GitHub Actions secrets), else config.ini."""
    env = os.environ
    if env.get("APPFOLIO_CLIENT_ID") and env.get("APPFOLIO_CLIENT_SECRET"):
        return {
            "subdomain": env.get("APPFOLIO_SUBDOMAIN") or "guidemanagement",
            "client_id": env["APPFOLIO_CLIENT_ID"].strip(),
            "client_secret": env["APPFOLIO_CLIENT_SECRET"].strip(),
            "report": env.get("APPFOLIO_REPORT") or "work_order",
        }
    if not CONFIG_PATH.exists():
        sys.exit("No AppFolio credentials. Set APPFOLIO_CLIENT_ID and APPFOLIO_CLIENT_SECRET, "
                 "or copy config.example.ini to config.ini.")
    cp = configparser.ConfigParser()
    cp.read(CONFIG_PATH)
    c = cp["appfolio"]
    return {
        "subdomain": c.get("subdomain", "guidemanagement").strip(),
        "client_id": c["client_id"].strip(),
        "client_secret": c["client_secret"].strip(),
        "report": c.get("report", "work_order").strip(),
    }


def fetch_report(cfg, filters):
    """One report request, following pagination."""
    url = f"https://{cfg['subdomain']}.appfolio.com/api/v2/reports/{cfg['report']}.json"
    auth = (cfg["client_id"], cfg["client_secret"])
    body = {"paginate_results": True, **filters}
    rows, page = [], 0
    while url:
        page += 1
        r = (requests.post(url, json=body, auth=auth, timeout=120) if page == 1
             else requests.get(url, auth=auth, timeout=120))
        if r.status_code == 401:
            raise SystemExit("AppFolio rejected the API credentials (401). Check the APPFOLIO_CLIENT_ID / APPFOLIO_CLIENT_SECRET secrets.")
        if r.status_code >= 400:
            raise SystemExit(f"AppFolio returned {r.status_code}: {r.text[:500]}")
        data = r.json()
        if isinstance(data, list):          # unpaginated response
            rows.extend(data)
            break
        rows.extend(data.get("results", []))
        url = data.get("next_page_url")
    return rows


def fetch_work_orders(cfg):
    """AppFolio's API only honors one status code per request, so ask once per status and combine."""
    extra = {}
    raw_extra = os.environ.get("APPFOLIO_FILTERS", "").strip()
    if raw_extra:
        try:
            extra = json.loads(raw_extra)
        except json.JSONDecodeError:
            raise SystemExit(f"APPFOLIO_FILTERS isn't valid JSON: {raw_extra}")
        extra.pop("work_order_statuses", None)   # statuses are handled below
        if extra:
            log.info("Extra report filters: %s", extra)
    # AppFolio's default is work orders created in the last 90 days. Ask for an explicit
    # range instead: created on or after 01/01/2000 through today (same fields as the report page).
    today = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%m/%d/%Y")
    base = {
        "status_date": "0",                     # 0 = Created On
        "float_status_date_range": "0",         # fixed range, not "last 90 days"
        "status_date_range_from": "01/01/2000",
        "status_date_range_to": today,
        **extra,
    }
    combined, seen = [], set()
    for code, name in STATUS_CODES.items():
        part = fetch_report(cfg, {**base, "work_order_statuses": code})
        log.info("%s: %s work orders", name, len(part))
        for rec in part:
            key = pick(rec, ID_FIELDS["work_order_id"]) or pick(rec, FIELD_MAP["WORK ORDER"]) or id(rec)
            if key not in seen:
                seen.add(key)
                combined.append(rec)
    return combined


def pick(rec, names):
    for n in names:
        if n in rec and rec[n] not in (None, ""):
            return rec[n]
    return ""


# ---------------------------------------------------------------- cleaning
PHONE = re.compile(r"(\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
KW = r"(?:code|combo|combination|lockbox|lock box|pin|passcode|password|gate|keypad|key ?pad|alarm|wifi|wi-fi)"
CODE = re.compile(r"(\b" + KW + r"\b[^\n\d]{0,25})([#*]?\d[\d#*\- ]{2,10}\d)", re.I)
PASSWORD = re.compile(r"(\b(?:password|passcode|pw)\b\s*(?:is|:|=)?\s*)(\S+)", re.I)
LABELED = re.compile(r"^(\s*(?:Name|Phone|Caller's Name|Caller's Number|Resident Name|Email|Call Recording Link)\s*:).*$", re.I | re.M)
URL = re.compile(r"https?://\S+")


def redact(text):
    t = text or ""
    t = LABELED.sub(r"\1 [removed]", t)
    t = URL.sub("[link removed]", t)
    t = PHONE.sub("[phone removed]", t)
    t = EMAIL.sub("[email removed]", t)
    t = CODE.sub(lambda m: m.group(1) + "[code removed]", t)
    t = PASSWORD.sub(lambda m: m.group(1) + "[removed]", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def to_date(v):
    if not v:
        return ""
    s = str(v)
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    for fmt in ("%m/%d/%Y", "%m/%d/%Y %I:%M %p", "%m/%d/%y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return s


def normalize(raw):
    out = []
    for rec in raw:
        row = {col: pick(rec, names) for col, names in FIELD_MAP.items()}
        row = {k: ("" if v is None else str(v)) for k, v in row.items()}
        if row["STATUS"].strip().lower() not in SHOW_STATUSES:
            continue
        row["DESCRIPTION"] = redact(row["DESCRIPTION"])
        row["CREATED"] = to_date(row["CREATED"])
        row["SCHEDULED START"] = to_date(row["SCHEDULED START"])
        sr, wo = pick(rec, ID_FIELDS["service_request_id"]), pick(rec, ID_FIELDS["work_order_id"])
        num = row["WORK ORDER"]
        if num and not num.startswith("#"):
            num = "#" + num
        row["_path"] = f"/maintenance/service_requests/{sr}/work_orders/{wo}" if sr and wo else ""
        row["WORK ORDER"] = f"[{num}]({row['_path']})" if row["_path"] else num
        row["_num"] = num
        out.append(row)
    return out


# ---------------------------------------------------------------- output
def build_html(rows, as_of):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLUMNS)
    for r in rows:
        w.writerow([r[c] for c in COLUMNS])
    snap = json.dumps({"asOf": as_of, "csv": buf.getvalue()}).replace("</", "<\\/")
    return TEMPLATE_PATH.read_text(encoding="utf-8").replace("__SNAPSHOT__", snap, 1)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inspect", action="store_true", help="print the fields AppFolio returns and exit")
    ap.add_argument("--out", default="_site", help="folder to write the site into (default: _site)")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler()])
    log.info("wo_dashboard.py version 2026-10-07b (date range fix)")
    cfg = load_config()
    raw = fetch_work_orders(cfg)
    log.info("AppFolio returned %s work orders", len(raw))
    log.info("Statuses from AppFolio: %s", sorted({str(pick(r, FIELD_MAP["STATUS"])) for r in raw}))
    created = sorted(to_date(pick(r, FIELD_MAP["CREATED"])) for r in raw if pick(r, FIELD_MAP["CREATED"]))
    log.info("Oldest work order created: %s", created[0] if created else "no created dates found")

    if a.inspect:
        # Field names and statuses only. No values, so nothing personal lands in the Actions log.
        if not raw:
            print("No rows returned.")
            return
        print("Fields on the work order records:")
        for k in sorted({k for r in raw[:50] for k in r}):
            print("  " + k)
        print("\nStatuses seen:", sorted({str(pick(r, FIELD_MAP["STATUS"])) for r in raw}))
        missing = [col for col, names in FIELD_MAP.items() if not any(n in raw[0] for n in names)]
        print("Dashboard columns with no matching field:", missing or "none")
        if not all(any(n in raw[0] for n in names) for names in ID_FIELDS.values()):
            print("No service request / work order IDs found, so work order numbers won't be clickable.")
        return

    rows = normalize(raw)
    if not rows:
        # Don't overwrite yesterday's dashboard with an empty one.
        raise SystemExit("No open work orders found. Run with --inspect to check field names and statuses. The live dashboard was not changed.")
    as_of = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(build_html(rows, as_of), encoding="utf-8")
    (out / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    log.info("Wrote %s open work orders to %s", len(rows), out / "index.html")

if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0):
            log.error(str(e))
        raise
    except Exception:
        log.exception("Run failed. The live dashboard was not changed.")
        sys.exit(1)
