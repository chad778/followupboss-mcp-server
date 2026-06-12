#!/usr/bin/env python3
"""
Follow Up Boss -> Google Sheets activity & pipeline sync.

Pulls Follow Up Boss (FUB) data by agent and writes a daily activity /
pipeline dashboard into a single Google Spreadsheet. Designed to run headless
on GitHub Actions: a one-time --mode backfill (trailing N days) plus an
automatic --mode daily run locked to 10pm America/New_York.

Auth:
  FUB uses HTTP Basic auth -> API key as username, blank password.
  Google uses a service-account JSON (Sheets API).

Secrets (read from env, set as GitHub repo secrets):
  FUB_API_KEY                 - Follow Up Boss API key
  GOOGLE_SERVICE_ACCOUNT_JSON - service-account credentials, as a raw JSON string
  SHEET_ID                    - target spreadsheet ID

Run:
  python fub_sheets_sync.py --mode backfill --days 30
  python fub_sheets_sync.py --mode daily

All account configuration (users, pipelines/stages, appointment outcomes,
ponds) is discovered at runtime and never hardcoded. The resolved mapping is
written to the Config tab so the operator can audit it.

IMPORTANT field-mapping notes verified against the live account (see README
"Field mappings to confirm"):
  - Deals have NO assignedUserId / closeDate / personIds / side field.
    Agent attribution comes from deal.users[], side comes from the pipeline
    (1="Sale"=buyer, 2="Listing"=seller), and the "close date" is the
    timestamp the deal entered its Closed stage (deal.enteredStageAt).
  - /textMessages cannot be swept account-wide on this account (the API
    requires a person/thread/phone filter). The script attempts it and
    degrades gracefully, writing the texts column blank and flagging it.
  - /notes list payloads omit the author, so notes are counted per agent by
    querying /notes?userId=<id>.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

import pytz
import requests
from dateutil import parser as date_parser
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---------------------------------------------------------------------------
# Constants / tunables
# ---------------------------------------------------------------------------

FUB_BASE_URL = "https://api.followupboss.com/v1"
EASTERN = pytz.timezone("America/New_York")

# A "Conversation" is a logged call lasting at least this many seconds.
CONVO_MIN_SECONDS = 120

# Scheduled runs only proceed when the Eastern hour equals this. GitHub cron is
# UTC and shifts with daylight saving, so the workflow fires at both 02:00 and
# 03:00 UTC and the script gates on the wall-clock Eastern hour. 22 == 10pm ET.
# NOTE: the brief's prose says "8pm" in one place and "10pm" in the detailed
# scheduling step; this is set to the detailed spec (10pm). Change here + the
# workflow cron together if 8pm is intended.
RUN_HOUR_ET = 22

# Leaderboard pace targets.
TARGET_CONVOS_PER_WEEK = 20
TARGET_APPTS_SET_PER_WEEK = 5
TARGET_SHOW_RATE_LOW = 0.50  # 50%
TARGET_SHOW_RATE_HIGH = 0.60  # 60% (upper bound of the healthy band)

# Appointment outcome names that mean the client did NOT show. Anything else
# with a non-null outcome is treated as "met/showed". Matched case-insensitively
# as a substring so "No Show", "no-show", "Cancelled" all catch.
NO_SHOW_OUTCOME_PATTERNS = ["no show", "no-show", "cancel", "reschedul"]

# Deal stage classification (applied to the stage NAME when closedStage is
# false). closedStage==true always wins as "Closed".
UNDER_CONTRACT_PATTERNS = ["under contract", "pending", "escrow", "accepted", "mutual"]
DEAD_STAGE_PATTERNS = ["fall through", "expired", "dead", "lost", "trash", "cancel", "withdrawn"]

# Pipeline-name -> deal side. Anything matching listing/seller is the listing
# side; sale/buyer is the buyer side; everything else is Unknown (surfaced, not
# dropped).
LISTING_SIDE_PATTERNS = ["listing", "seller", "sell"]
BUYER_SIDE_PATTERNS = ["sale", "buyer", "buy", "purchase"]

# Identify this integration to FUB for higher rate limits (see the X-System
# notice in API responses). Override via env if you register a system key.
X_SYSTEM = os.environ.get("FUB_X_SYSTEM", "TeamDashboardSync")
X_SYSTEM_KEY = os.environ.get("FUB_X_SYSTEM_KEY", "")

# Sheet tab names.
TAB_ACTIVITY = "Daily Activity"
TAB_PIPELINE = "Pipeline Snapshot"
TAB_CLOSINGS = "Closings"
TAB_LEADERBOARD = "Leaderboard"
TAB_CONFIG = "Config"

# Column headers per tab. Order here IS the sheet column order.
HEADERS = {
    TAB_ACTIVITY: [
        "date", "agent", "dials total", "outbound dials", "conversations",
        "texts sent", "appts set", "appts met", "new leads assigned", "notes",
    ],
    TAB_PIPELINE: [
        "snapshot date", "agent", "active deals", "active value",
        "under contract count", "under contract value", "listings count",
        "listings value", "projected closings this month",
    ],
    TAB_CLOSINGS: ["close date", "agent", "deal name", "price", "side"],
    TAB_LEADERBOARD: [
        "agent", "t7 conversations", "t7 appts set", "t30 conversations",
        "t30 closings", "convo->appt rate", "appt show rate", "status",
    ],
    TAB_CONFIG: ["key", "value"],
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    """One-line, timestamped, flushed log to the Actions console."""
    print(f"[{datetime.now(EASTERN):%Y-%m-%d %H:%M:%S %Z}] {msg}", flush=True)


def die(msg: str, code: int = 1) -> None:
    """Log an error and exit non-zero so the Actions run shows red."""
    log(f"FATAL: {msg}")
    sys.exit(code)


def parse_ts(value) -> datetime | None:
    """Parse a FUB ISO timestamp (UTC) into an aware UTC datetime."""
    if not value:
        return None
    try:
        dt = date_parser.parse(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    return dt.astimezone(pytz.utc)


def to_eastern_date(value) -> str | None:
    """FUB UTC timestamp -> 'YYYY-MM-DD' in Eastern (the activity's local day)."""
    dt = parse_ts(value)
    if dt is None:
        return None
    return dt.astimezone(EASTERN).strftime("%Y-%m-%d")


def matches_any(name: str, patterns: list[str]) -> bool:
    low = (name or "").lower()
    return any(p in low for p in patterns)


def num(value) -> float:
    """Coerce a possibly-null price/value field to a float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Follow Up Boss API client
# ---------------------------------------------------------------------------

class FUBError(Exception):
    pass


class FUBClient:
    """Thin FUB REST client: Basic auth, rate-limit aware, cursor pagination."""

    def __init__(self, api_key: str):
        self.session = requests.Session()
        self.session.auth = (api_key, "")  # API key as username, blank password
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if X_SYSTEM:
            headers["X-System"] = X_SYSTEM
        if X_SYSTEM_KEY:
            headers["X-System-Key"] = X_SYSTEM_KEY
        self.session.headers.update(headers)
        # Endpoints that returned errors during the run, for the reliability summary.
        self.endpoint_errors: list[str] = []

    def _request(self, url: str, params: dict | None = None) -> dict:
        """GET with 429 backoff, proactive rate-limit throttle, and 5xx retry."""
        max_retries = 5
        for attempt in range(max_retries + 1):
            resp = self.session.get(url, params=params, timeout=60)

            # Proactive throttle: if we're nearly out of budget, pause until reset.
            remaining = resp.headers.get("X-RateLimit-Remaining")
            if remaining is not None:
                try:
                    if int(remaining) <= 2:
                        reset = resp.headers.get("X-RateLimit-Reset")
                        wait = min(int(reset), 15) if reset and reset.isdigit() else 5
                        log(f"Rate budget low (remaining={remaining}); sleeping {wait}s")
                        time.sleep(wait)
                except ValueError:
                    pass

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                base = int(retry_after) if retry_after and retry_after.isdigit() else 2
                wait = min(base * (2 ** attempt), 30)
                log(f"HTTP 429 on {url}; backing off {wait}s (attempt {attempt + 1})")
                time.sleep(wait)
                continue

            if 500 <= resp.status_code < 600 and attempt < max_retries:
                wait = min(2 ** attempt, 30)
                log(f"HTTP {resp.status_code} on {url}; retrying in {wait}s")
                time.sleep(wait)
                continue

            if resp.status_code >= 400:
                raise FUBError(f"HTTP {resp.status_code} on {url}: {resp.text[:300]}")

            return resp.json()

        raise FUBError(f"Exhausted retries on {url}")

    def get_one(self, path: str, params: dict | None = None) -> dict:
        return self._request(FUB_BASE_URL + path, params=params)

    def iter_collection(self, path: str, collection_key: str, params: dict | None = None):
        """
        Yield items from a paginated list endpoint, following the opaque
        `_metadata.nextLink` cursor. Caller breaks the loop when records pass
        the date cutoff -- the generator then stops requesting further pages.

        FUB returns newest-first by default (sinceId descending); for endpoints
        that accept it we additionally request sort=-created (the caller passes
        it in params) to make the ordering explicit.
        """
        params = dict(params or {})
        params.setdefault("limit", 100)
        url = FUB_BASE_URL + path
        page_params: dict | None = params
        pages = 0
        while url:
            data = self._request(url, params=page_params)
            for item in data.get(collection_key, []) or []:
                yield item
            meta = data.get("_metadata") or {}
            next_link = meta.get("nextLink")
            if not next_link:
                break
            # nextLink is a fully-formed URL with the cursor embedded; don't
            # re-send our params or we'd fight the cursor and risk a 400.
            url = next_link
            page_params = None
            pages += 1
            if pages > 5000:  # hard safety valve against an infinite cursor
                log(f"Pagination safety stop on {path} after {pages} pages")
                break

    def try_get(self, path: str, collection_key: str, params: dict | None = None):
        """Like iter_collection but records (not raises) errors; returns a list or None."""
        try:
            return list(self.iter_collection(path, collection_key, params))
        except FUBError as e:
            self.endpoint_errors.append(f"{path}: {e}")
            log(f"ENDPOINT ERROR {path}: {e}")
            return None


# ---------------------------------------------------------------------------
# Step 1: discover account config (dynamic, never hardcoded)
# ---------------------------------------------------------------------------

class AccountConfig:
    """Resolved mappings: agents, pipeline stages, outcomes, ponds."""

    def __init__(self, fub: FUBClient):
        self.fub = fub
        self.users: dict[int, dict] = {}
        self.active_agents: dict[int, str] = {}  # userId -> name, active only
        self.stage_class: dict[int, str] = {}    # stageId -> Active/Under Contract/Closed/Dead
        self.stage_names: dict[int, str] = {}     # stageId -> name
        self.closed_stage_ids: set[int] = set()
        self.pipeline_side: dict[int, str] = {}   # pipelineId -> Listing/Buyer/Unknown
        self.pipeline_names: dict[int, str] = {}
        self.outcomes: dict[int, str] = {}        # outcomeId -> name
        self.ponds: dict[int, str] = {}           # pondId -> name
        self.notes: list[str] = []                # human-readable resolution notes

    def discover(self) -> None:
        self._discover_users()
        self._discover_pipelines()
        self._discover_outcomes()
        self._discover_ponds()

    def _discover_users(self) -> None:
        users = self.fub.try_get("/users", "users", {"limit": 100}) or []
        for u in users:
            uid = u.get("id")
            self.users[uid] = u
            # status is capitalized ("Active"/"Inactive"); filter to active only.
            if str(u.get("status", "")).lower() == "active":
                self.active_agents[uid] = u.get("name") or f"user_{uid}"
        log(f"Users: {len(self.users)} total, {len(self.active_agents)} active")
        self.notes.append(f"Active agents resolved: {len(self.active_agents)}")

    def _discover_pipelines(self) -> None:
        # Deal pipeline stages are embedded inside each pipeline (NOT in /stages,
        # which only returns person-lifecycle stages with pipelineId=null).
        pipelines = self.fub.try_get("/pipelines", "pipelines", {"limit": 100}) or []
        for p in pipelines:
            pid = p.get("id")
            pname = p.get("name") or ""
            self.pipeline_names[pid] = pname
            if matches_any(pname, LISTING_SIDE_PATTERNS):
                self.pipeline_side[pid] = "Listing"
            elif matches_any(pname, BUYER_SIDE_PATTERNS):
                self.pipeline_side[pid] = "Buyer"
            else:
                self.pipeline_side[pid] = "Unknown"
                self.notes.append(f"Pipeline '{pname}' (id {pid}) side UNRESOLVED -> Unknown")
            for s in p.get("stages", []) or []:
                sid = s.get("id")
                sname = s.get("name") or ""
                self.stage_names[sid] = sname
                if s.get("closedStage"):
                    self.stage_class[sid] = "Closed"
                    self.closed_stage_ids.add(sid)
                elif matches_any(sname, UNDER_CONTRACT_PATTERNS):
                    self.stage_class[sid] = "Under Contract"
                elif matches_any(sname, DEAD_STAGE_PATTERNS):
                    self.stage_class[sid] = "Dead"
                else:
                    self.stage_class[sid] = "Active"
        log(f"Pipelines: {len(self.pipeline_names)}; deal stages classified: {len(self.stage_class)}")

    def _discover_outcomes(self) -> None:
        outcomes = self.fub.try_get("/appointmentOutcomes", "appointmentOutcomes", {"limit": 100})
        if not outcomes:
            self.notes.append("appointmentOutcomes returned no list body; 'met' uses name heuristic")
        for o in outcomes or []:
            self.outcomes[o.get("id")] = o.get("name") or ""
        log(f"Appointment outcomes: {len(self.outcomes)}")

    def _discover_ponds(self) -> None:
        ponds = self.fub.try_get("/ponds", "ponds", {"limit": 100}) or []
        for p in ponds:
            self.ponds[p.get("id")] = p.get("name") or ""
        log(f"Ponds: {len(self.ponds)} (unassigned-lead buckets)")

    # --- classification helpers used by the metrics step -------------------

    def is_met_outcome(self, outcome_id, outcome_name) -> bool:
        """An appointment was 'met/showed' if it has an outcome that isn't a no-show."""
        name = outcome_name or self.outcomes.get(outcome_id, "")
        if not name:
            return False  # no outcome recorded -> not counted as met
        return not matches_any(name, NO_SHOW_OUTCOME_PATTERNS)

    def deal_side(self, pipeline_id) -> str:
        return self.pipeline_side.get(pipeline_id, "Unknown")


# ---------------------------------------------------------------------------
# Step 2: metrics by agent
# ---------------------------------------------------------------------------

class Metrics:
    """Pulls FUB activity/pipeline data and aggregates per agent."""

    def __init__(self, fub: FUBClient, cfg: AccountConfig):
        self.fub = fub
        self.cfg = cfg
        self.texts_supported = True  # flipped off if /textMessages can't be swept

    # --- activity (per agent per day) --------------------------------------

    def activity(self, start_utc: datetime, end_utc: datetime) -> dict:
        """
        Returns {(date_str, userId): {metric: count}} for the window.
        date_str is the Eastern calendar day of the activity.
        """
        rows: dict = defaultdict(lambda: defaultdict(int))

        def bucket(date_str, uid, field, inc=1):
            if date_str is None or uid is None:
                return
            rows[(date_str, uid)][field] += inc

        self._collect_calls(start_utc, end_utc, bucket)
        self._collect_texts(start_utc, end_utc, bucket)
        self._collect_appointments(start_utc, end_utc, bucket)
        self._collect_new_leads(start_utc, end_utc, bucket)
        self._collect_notes(start_utc, end_utc, bucket)
        return rows

    def _within(self, ts, start_utc, end_utc) -> bool:
        return ts is not None and start_utc <= ts <= end_utc

    def _collect_calls(self, start_utc, end_utc, bucket) -> None:
        # /calls fields -> Daily Activity columns:
        #   userId        -> agent
        #   (count)       -> dials total
        #   isIncoming==False -> outbound dials
        #   duration>=120 -> conversations
        count = 0
        for c in self.fub.iter_collection("/calls", "calls", {"limit": 100}):
            ts = parse_ts(c.get("created") or c.get("startedAt"))
            if ts is None:
                continue
            if ts < start_utc:
                break  # newest-first: everything past here is older than the window
            if ts > end_utc:
                continue
            uid = c.get("userId")
            if uid not in self.cfg.active_agents:
                continue
            d = ts.astimezone(EASTERN).strftime("%Y-%m-%d")
            bucket(d, uid, "dials total")
            if c.get("isIncoming") is False:
                bucket(d, uid, "outbound dials")
            if num(c.get("duration")) >= CONVO_MIN_SECONDS:
                bucket(d, uid, "conversations")
            count += 1
        log(f"Calls in window: {count}")

    def _collect_texts(self, start_utc, end_utc, bucket) -> None:
        # /textMessages -> "texts sent" (outbound, isIncoming==False), per userId.
        # This account's API rejects an account-wide sweep (requires a filter),
        # so we attempt it and degrade gracefully if unsupported.
        try:
            count = 0
            for t in self.fub.iter_collection("/textMessages", "textMessages", {"limit": 100}):
                ts = parse_ts(t.get("created"))
                if ts is None:
                    continue
                if ts < start_utc:
                    break
                if ts > end_utc:
                    continue
                if t.get("isIncoming") is True:
                    continue  # outbound only
                uid = t.get("userId")
                if uid not in self.cfg.active_agents:
                    continue
                d = ts.astimezone(EASTERN).strftime("%Y-%m-%d")
                bucket(d, uid, "texts sent")
                count += 1
            log(f"Texts (outbound) in window: {count}")
        except FUBError as e:
            self.texts_supported = False
            self.fub.endpoint_errors.append(f"/textMessages: {e}")
            log(f"ENDPOINT ERROR /textMessages (texts will be blank): {e}")

    def _collect_appointments(self, start_utc, end_utc, bucket) -> None:
        # /appointments fields -> Daily Activity columns:
        #   created in window           -> appts set, attributed to createdById
        #   outcome present & not no-show -> appts met
        count = 0
        for a in self.fub.iter_collection("/appointments", "appointments", {"limit": 100}):
            ts = parse_ts(a.get("created"))
            if ts is None:
                continue
            if ts < start_utc:
                break
            if ts > end_utc:
                continue
            uid = a.get("createdById")
            if uid not in self.cfg.active_agents:
                continue
            d = ts.astimezone(EASTERN).strftime("%Y-%m-%d")
            bucket(d, uid, "appts set")
            if self.cfg.is_met_outcome(a.get("outcomeId"), a.get("outcome")):
                bucket(d, uid, "appts met")
            count += 1
        log(f"Appointments set in window: {count}")

    def _collect_new_leads(self, start_utc, end_utc, bucket) -> None:
        # /people fields -> "new leads assigned":
        #   created in window, attributed to assignedUserId
        count = 0
        for p in self.fub.iter_collection("/people", "people", {"limit": 100, "sort": "-created"}):
            ts = parse_ts(p.get("created"))
            if ts is None:
                continue
            if ts < start_utc:
                break
            if ts > end_utc:
                continue
            uid = p.get("assignedUserId")
            if uid not in self.cfg.active_agents:
                continue
            d = ts.astimezone(EASTERN).strftime("%Y-%m-%d")
            bucket(d, uid, "new leads assigned")
            count += 1
        log(f"New leads assigned in window: {count}")

    def _collect_notes(self, start_utc, end_utc, bucket) -> None:
        # /notes list payloads omit the author, so query per agent with userId
        # filter. /notes fields -> "notes":
        #   created in window, one row per note authored by the agent.
        total = 0
        for uid in self.cfg.active_agents:
            for n in self.fub.iter_collection(
                "/notes", "notes", {"limit": 100, "userId": uid, "sort": "-created"}
            ):
                ts = parse_ts(n.get("created"))
                if ts is None:
                    continue
                if ts < start_utc:
                    break
                if ts > end_utc:
                    continue
                d = ts.astimezone(EASTERN).strftime("%Y-%m-%d")
                bucket(d, uid, "notes")
                total += 1
        log(f"Notes in window: {total}")

    # --- pipeline snapshot + closings --------------------------------------

    def _all_deals(self) -> list[dict]:
        # Raw /deals returns full objects (users[], people[], pipelineId,
        # stageId, stageName, price, projectedCloseDate, enteredStageAt, status).
        # Small collection (tens of deals), so just pull them all.
        deals = self.fub.try_get("/deals", "deals", {"limit": 100}) or []
        log(f"Deals fetched: {len(deals)}")
        return deals

    def _deal_agents(self, deal: dict) -> list[tuple[int, str]]:
        """Agent attribution from deal.users[] (FUB has no assignedUserId on deals)."""
        out = []
        for u in deal.get("users", []) or []:
            uid = u.get("id")
            if uid in self.cfg.active_agents:
                out.append((uid, self.cfg.active_agents[uid]))
        return out

    def pipeline_snapshot(self, deals: list[dict], snapshot_date: str) -> dict:
        """
        Returns {(snapshot_date, userId): {col: value}} aggregating each agent's
        live pipeline. A deal with multiple assigned agents is counted for each
        (documented; rare on a small team).
        """
        month_prefix = datetime.now(EASTERN).strftime("%Y-%m")
        snap: dict = defaultdict(lambda: defaultdict(float))

        for deal in deals:
            sid = deal.get("stageId")
            cls = self.cfg.stage_class.get(sid, "Active")
            price = num(deal.get("price"))
            side = self.cfg.deal_side(deal.get("pipelineId"))
            agents = self._deal_agents(deal) or [(None, "Unassigned")]
            proj = deal.get("projectedCloseDate")
            proj_in_month = bool(proj and str(proj).startswith(month_prefix))

            for uid, _name in agents:
                key = (snapshot_date, uid)
                if cls == "Closed" or cls == "Dead":
                    pass  # closed/dead deals are not part of the live pipeline counts
                elif cls == "Under Contract":
                    snap[key]["under contract count"] += 1
                    snap[key]["under contract value"] += price
                else:  # Active
                    snap[key]["active deals"] += 1
                    snap[key]["active value"] += price
                # Listings = active (non-closed/dead) deals on the listing side.
                if cls not in ("Closed", "Dead") and side == "Listing":
                    snap[key]["listings count"] += 1
                    snap[key]["listings value"] += price
                # Projected closings this month: open deal w/ projected date in month.
                if cls not in ("Closed", "Dead") and proj_in_month:
                    snap[key]["projected closings this month"] += 1
        return snap

    def closings(self, deals: list[dict], start_utc: datetime, end_utc: datetime) -> list[list]:
        """
        Closings = deals in a Closed stage whose close timestamp falls in the
        window. FUB has no closeDate field, so we use enteredStageAt (when the
        deal entered the Closed stage). Returns rows: [close date, agent, name, price, side].
        """
        rows = []
        for deal in deals:
            sid = deal.get("stageId")
            if sid not in self.cfg.closed_stage_ids:
                continue
            close_ts = parse_ts(deal.get("enteredStageAt")) or parse_ts(deal.get("projectedCloseDate"))
            if not self._within(close_ts, start_utc, end_utc):
                continue
            close_date = close_ts.astimezone(EASTERN).strftime("%Y-%m-%d")
            side = self.cfg.deal_side(deal.get("pipelineId"))
            name = deal.get("name") or ""
            price = num(deal.get("price"))
            agents = self._deal_agents(deal) or [(None, "Unassigned")]
            agent_label = ", ".join(a[1] for a in agents)
            rows.append([close_date, agent_label, name, price, side])
        log(f"Closings in window: {len(rows)}")
        return rows


# ---------------------------------------------------------------------------
# Google Sheets writer
# ---------------------------------------------------------------------------

class SheetWriter:
    """Idempotent Google Sheets writes keyed per tab."""

    def __init__(self, sheet_id: str, creds_json: str):
        try:
            info = json.loads(creds_json)
        except json.JSONDecodeError as e:
            die(f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {e}")
        try:
            creds = Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
            self.svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        except Exception as e:  # auth failure must turn the run red
            die(f"Google auth failed: {e}")
        self.sheet_id = sheet_id
        self._ensure_tabs()

    def _ensure_tabs(self) -> None:
        meta = self.svc.spreadsheets().get(spreadsheetId=self.sheet_id).execute()
        existing = {s["properties"]["title"] for s in meta.get("sheets", [])}
        requests_body = []
        for title in HEADERS:
            if title not in existing:
                requests_body.append({"addSheet": {"properties": {"title": title}}})
        if requests_body:
            self.svc.spreadsheets().batchUpdate(
                spreadsheetId=self.sheet_id, body={"requests": requests_body}
            ).execute()
            log(f"Created tabs: {[r['addSheet']['properties']['title'] for r in requests_body]}")
        # Make sure every tab has its header row.
        for title, header in HEADERS.items():
            first = self._read(title)
            if not first or first[0] != header:
                self._write(title, [header], start_row=1)

    def _read(self, tab: str) -> list[list]:
        resp = self.svc.spreadsheets().values().get(
            spreadsheetId=self.sheet_id, range=f"'{tab}'"
        ).execute()
        return resp.get("values", [])

    def _write(self, tab: str, values: list[list], start_row: int = 1) -> None:
        self.svc.spreadsheets().values().update(
            spreadsheetId=self.sheet_id,
            range=f"'{tab}'!A{start_row}",
            valueInputOption="RAW",
            body={"values": values},
        ).execute()

    def _clear(self, tab: str) -> None:
        self.svc.spreadsheets().values().clear(
            spreadsheetId=self.sheet_id, range=f"'{tab}'"
        ).execute()

    def _rewrite(self, tab: str, data_rows: list[list]) -> None:
        """Clear the tab and rewrite header + data in one shot."""
        self._clear(tab)
        self._write(tab, [HEADERS[tab]] + data_rows, start_row=1)

    def upsert_keyed(self, tab: str, new_rows: list[list], key_cols: int) -> int:
        """
        Replace rows whose first `key_cols` columns match an incoming row; append
        the rest. Makes re-runs idempotent (no duplicate date+agent rows).
        """
        existing = self._read(tab)
        body = existing[1:] if existing else []  # drop header

        def norm_key(row):
            return tuple(str(c) for c in row[:key_cols])

        ordered: list[list] = list(body)
        pos_by_key = {norm_key(r): i for i, r in enumerate(ordered)}
        for row in new_rows:
            key = norm_key(row)
            if key in pos_by_key:
                ordered[pos_by_key[key]] = row  # replace -> no duplicate
            else:
                pos_by_key[key] = len(ordered)
                ordered.append(row)
        ordered.sort(key=lambda r: [str(c) for c in r[:key_cols]])
        self._rewrite(tab, ordered)
        return len(new_rows)

    def append_new(self, tab: str, new_rows: list[list], dedupe_cols: int) -> int:
        """Append rows not already present (dedupe on first `dedupe_cols` columns)."""
        existing = self._read(tab)
        seen = {tuple(str(c) for c in r[:dedupe_cols]) for r in existing[1:]} if existing else set()
        to_add = [r for r in new_rows if tuple(str(c) for c in r[:dedupe_cols]) not in seen]
        if to_add:
            self.svc.spreadsheets().values().append(
                spreadsheetId=self.sheet_id,
                range=f"'{tab}'!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": to_add},
            ).execute()
        return len(to_add)

    def replace_all(self, tab: str, data_rows: list[list]) -> None:
        self._rewrite(tab, data_rows)

    def read_data(self, tab: str) -> list[list]:
        rows = self._read(tab)
        return rows[1:] if rows else []


# ---------------------------------------------------------------------------
# Leaderboard (recomputed every run from the sheet's own data)
# ---------------------------------------------------------------------------

def build_leaderboard(activity_rows: list[list], closing_rows: list[list],
                      active_agents: dict[int, str]) -> list[list]:
    """
    Derive the leaderboard from what's already in the sheet so it stays
    consistent with the visible data. activity_rows columns match
    HEADERS[TAB_ACTIVITY]; closing_rows match HEADERS[TAB_CLOSINGS].
    """
    today = datetime.now(EASTERN).date()
    d7 = today - timedelta(days=7)
    d30 = today - timedelta(days=30)

    # Per-agent accumulators over trailing windows.
    agg = defaultdict(lambda: {
        "c7": 0, "set7": 0, "c30": 0, "set30": 0, "met30": 0, "close30": 0,
    })

    for row in activity_rows:
        if len(row) < 8:
            continue
        try:
            d = datetime.strptime(row[0], "%Y-%m-%d").date()
        except (ValueError, IndexError):
            continue
        agent = row[1]
        convos = int(num(row[4]))      # conversations
        appts_set = int(num(row[6]))   # appts set
        appts_met = int(num(row[7]))   # appts met
        if d >= d7:
            agg[agent]["c7"] += convos
            agg[agent]["set7"] += appts_set
        if d >= d30:
            agg[agent]["c30"] += convos
            agg[agent]["set30"] += appts_set
            agg[agent]["met30"] += appts_met

    for row in closing_rows:
        if not row:
            continue
        try:
            d = datetime.strptime(row[0], "%Y-%m-%d").date()
        except (ValueError, IndexError):
            continue
        # A closing row may list multiple co-agents joined by ", ".
        for agent in str(row[1]).split(", "):
            if d >= d30:
                agg[agent.strip()]["close30"] += 1

    # Ensure every active agent appears even with zero activity.
    for name in active_agents.values():
        _ = agg[name]

    out = []
    for agent in sorted(agg):
        a = agg[agent]
        convo_appt_rate = (a["set30"] / a["c30"]) if a["c30"] else 0.0
        show_rate = (a["met30"] / a["set30"]) if a["set30"] else 0.0
        flags = []
        if a["c7"] < TARGET_CONVOS_PER_WEEK:
            flags.append(f"convos<{TARGET_CONVOS_PER_WEEK}")
        if a["set7"] < TARGET_APPTS_SET_PER_WEEK:
            flags.append(f"appts<{TARGET_APPTS_SET_PER_WEEK}")
        if a["set30"] and show_rate < TARGET_SHOW_RATE_LOW:
            flags.append("show<50%")
        status = "ON TRACK" if not flags else "BELOW: " + ", ".join(flags)
        out.append([
            agent, a["c7"], a["set7"], a["c30"], a["close30"],
            f"{convo_appt_rate * 100:.0f}%", f"{show_rate * 100:.0f}%", status,
        ])
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def compute_window(mode: str, days: int):
    """Return (start_utc, end_utc, snapshot_date, activity_dates_hint)."""
    now_et = datetime.now(EASTERN)
    if mode == "backfill":
        start_et = (now_et - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        end_et = now_et
    else:  # daily -> yesterday in Eastern
        yesterday = (now_et - timedelta(days=1)).date()
        start_et = EASTERN.localize(datetime(yesterday.year, yesterday.month, yesterday.day, 0, 0, 0))
        end_et = EASTERN.localize(datetime(yesterday.year, yesterday.month, yesterday.day, 23, 59, 59))
    snapshot_date = now_et.strftime("%Y-%m-%d")
    return start_et.astimezone(pytz.utc), end_et.astimezone(pytz.utc), snapshot_date


def activity_rows_from_agg(agg: dict, active_agents: dict[int, str],
                           mode: str, start_utc, end_utc) -> tuple[list[list], int]:
    """
    Convert the per-(date,user) aggregate into sheet rows. In backfill mode we
    emit a row for every agent for every day in the window (zeros included) so
    the leaderboard's trailing windows are dense. In daily mode we emit one row
    per active agent for the single day.
    """
    cols = HEADERS[TAB_ACTIVITY][2:]  # metric columns after date+agent
    rows = []

    # Enumerate the set of Eastern dates the window spans.
    start_d = start_utc.astimezone(EASTERN).date()
    end_d = end_utc.astimezone(EASTERN).date()
    if mode == "daily":
        date_list = [start_d]  # the single yesterday
    else:
        date_list = []
        d = start_d
        # Only complete days (exclude today's partial day from backfill rows).
        last = end_d - timedelta(days=1) if end_d == datetime.now(EASTERN).date() else end_d
        while d <= last:
            date_list.append(d)
            d += timedelta(days=1)

    zero_activity_agents = set(active_agents.values())
    for date in date_list:
        ds = date.strftime("%Y-%m-%d")
        for uid, name in active_agents.items():
            m = agg.get((ds, uid), {})
            row = [ds, name] + [int(m.get(c, 0)) for c in cols]
            if any(row[2:]):
                zero_activity_agents.discard(name)
            # Blank out texts column if the endpoint was unsupported this run.
            rows.append(row)
    return rows, len(zero_activity_agents)


def run(mode: str, days: int) -> None:
    fub_key = os.environ.get("FUB_API_KEY")
    sheet_id = os.environ.get("SHEET_ID")
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not fub_key:
        die("FUB_API_KEY is not set")
    if not sheet_id:
        die("SHEET_ID is not set")
    if not creds_json:
        die("GOOGLE_SERVICE_ACCOUNT_JSON is not set")

    fub = FUBClient(fub_key)

    # Fail fast (red run) if the FUB key is bad.
    try:
        fub.get_one("/identity")
    except FUBError as e:
        die(f"FUB authentication failed: {e}")

    cfg = AccountConfig(fub)
    cfg.discover()
    if not cfg.active_agents:
        die("No active agents resolved from /users; aborting")

    start_utc, end_utc, snapshot_date = compute_window(mode, days)
    log(f"Mode={mode} window {start_utc.isoformat()} -> {end_utc.isoformat()} (snapshot {snapshot_date})")

    metrics = Metrics(fub, cfg)
    agg = metrics.activity(start_utc, end_utc)
    deals = metrics._all_deals()
    snap = metrics.pipeline_snapshot(deals, snapshot_date)
    closing_rows = metrics.closings(deals, start_utc, end_utc)

    activity_rows, zero_count = activity_rows_from_agg(agg, cfg.active_agents, mode, start_utc, end_utc)

    # If texts were unsupported, blank that column (index 5) so we don't imply 0.
    if not metrics.texts_supported:
        for r in activity_rows:
            r[5] = ""

    # Pipeline snapshot rows: one per agent (active agents that have any deal,
    # plus zeros for the rest so the operator sees the whole roster).
    pipe_cols = HEADERS[TAB_PIPELINE][2:]
    pipeline_rows = []
    for uid, name in cfg.active_agents.items():
        m = snap.get((snapshot_date, uid), {})
        row = [snapshot_date, name] + [
            (int(m.get(c, 0)) if "count" in c or "deals" in c or "closings" in c else round(m.get(c, 0.0), 2))
            for c in pipe_cols
        ]
        pipeline_rows.append(row)

    # --- write to the sheet ----------------------------------------------
    writer = SheetWriter(sheet_id, creds_json)

    if mode == "backfill":
        # Build all tabs from scratch.
        writer.replace_all(TAB_ACTIVITY, sorted(activity_rows, key=lambda r: (r[0], r[1])))
        writer.replace_all(TAB_PIPELINE, pipeline_rows)
        writer.replace_all(TAB_CLOSINGS, sorted(closing_rows, key=lambda r: r[0]))
        activity_written = len(activity_rows)
        pipeline_written = len(pipeline_rows)
        closings_written = len(closing_rows)
    else:
        # Daily: idempotent upserts + append.
        activity_written = writer.upsert_keyed(TAB_ACTIVITY, activity_rows, key_cols=2)
        pipeline_written = writer.upsert_keyed(TAB_PIPELINE, pipeline_rows, key_cols=2)
        closings_written = writer.append_new(TAB_CLOSINGS, closing_rows, dedupe_cols=5)

    # Leaderboard recomputed every run from the sheet's own (now-updated) data.
    all_activity = writer.read_data(TAB_ACTIVITY)
    all_closings = writer.read_data(TAB_CLOSINGS)
    leaderboard = build_leaderboard(all_activity, all_closings, cfg.active_agents)
    writer.replace_all(TAB_LEADERBOARD, leaderboard)

    # --- Step 6: reliability summary -> Actions log + Config tab ----------
    rows_written = activity_written + pipeline_written + closings_written + len(leaderboard)
    summary = (
        f"mode={mode} | agents={len(cfg.active_agents)} | "
        f"activity_rows={activity_written} | pipeline_rows={pipeline_written} | "
        f"closings_added={closings_written} | leaderboard_rows={len(leaderboard)} | "
        f"zero_activity_agents={zero_count} | "
        f"endpoint_errors={len(fub.endpoint_errors)} | "
        f"texts_supported={metrics.texts_supported}"
    )
    log("RUN SUMMARY: " + summary)

    write_config_tab(writer, cfg, mode, snapshot_date, rows_written, summary,
                     fub.endpoint_errors, metrics.texts_supported)

    # If any endpoint errored, surface it but don't fail the whole run unless
    # it was an auth failure (already handled above).
    if fub.endpoint_errors:
        log(f"Completed with {len(fub.endpoint_errors)} endpoint error(s) -- see Config tab.")


def write_config_tab(writer: SheetWriter, cfg: AccountConfig, mode: str,
                     snapshot_date: str, rows_written: int, summary: str,
                     endpoint_errors: list[str], texts_supported: bool) -> None:
    """Document every resolved mapping + the run summary so it can be audited."""
    rows: list[list] = []
    rows.append(["last run (ET)", datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S %Z")])
    rows.append(["mode", mode])
    rows.append(["snapshot date", snapshot_date])
    rows.append(["rows written (this run)", rows_written])
    rows.append(["run summary", summary])
    rows.append(["conversation threshold (s)", CONVO_MIN_SECONDS])
    rows.append(["texts endpoint supported", str(texts_supported)])
    rows.append(["--- ACTIVE AGENTS (userId -> name) ---", ""])
    for uid, name in sorted(cfg.active_agents.items()):
        rows.append([f"user {uid}", name])
    rows.append(["--- PIPELINES (id -> name -> side) ---", ""])
    for pid, name in sorted(cfg.pipeline_names.items()):
        rows.append([f"pipeline {pid}", f"{name} -> {cfg.pipeline_side.get(pid)}"])
    rows.append(["--- DEAL STAGES (id -> name -> class) ---", ""])
    for sid, name in sorted(cfg.stage_names.items()):
        rows.append([f"stage {sid}", f"{name} -> {cfg.stage_class.get(sid)}"])
    rows.append(["--- APPOINTMENT OUTCOMES (id -> name) ---", ""])
    for oid, name in sorted(cfg.outcomes.items()):
        met = "MET" if cfg.is_met_outcome(oid, name) else "no-show/none"
        rows.append([f"outcome {oid}", f"{name} -> {met}"])
    rows.append(["--- PONDS (id -> name, unassigned-lead buckets) ---", ""])
    for pid, name in sorted(cfg.ponds.items()):
        rows.append([f"pond {pid}", name])
    rows.append(["--- RESOLUTION NOTES ---", ""])
    for n in cfg.notes:
        rows.append(["note", n])
    if endpoint_errors:
        rows.append(["--- ENDPOINT ERRORS ---", ""])
        for e in endpoint_errors:
            rows.append(["error", e])
    writer.replace_all(TAB_CONFIG, rows)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def maybe_gate_eastern_hour(force: bool) -> None:
    """
    Scheduled runs fire at both 02:00 and 03:00 UTC; only the one landing on
    22:00 Eastern should proceed. workflow_dispatch and --force bypass the gate.
    """
    if force:
        return
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return  # manual / dispatch runs always proceed
    hour = datetime.now(EASTERN).hour
    if hour != RUN_HOUR_ET:
        log(f"Scheduled run at Eastern hour {hour:02d}:00 != {RUN_HOUR_ET}:00; skipping.")
        sys.exit(0)


def main() -> None:
    ap = argparse.ArgumentParser(description="Follow Up Boss -> Google Sheets sync")
    ap.add_argument("--mode", choices=["backfill", "daily"], required=True)
    ap.add_argument("--days", type=int, default=30,
                    help="Backfill window length in days (backfill mode only)")
    ap.add_argument("--force", action="store_true",
                    help="Bypass the 10pm-Eastern scheduling gate")
    args = ap.parse_args()

    maybe_gate_eastern_hour(args.force)
    run(args.mode, args.days)
    log("Done.")


if __name__ == "__main__":
    main()
