#!/usr/bin/env python3
"""
Weekly FUB Activity Audit

Reads the Daily Activity Google Sheet for the current Mon-Sun week,
aggregates conversations and appointments set per agent, applies weekly
goals, and posts a color-coded report card to a Google Chat webhook.

Schedule: 8:00 PM ET every Sunday (via GitHub Actions).

Env vars required:
  GOOGLE_SERVICE_ACCOUNT_JSON  - service-account credentials JSON string
  SHEET_ID                     - target Google Spreadsheet ID
  FUB_API_KEY                  - Follow Up Boss API key (for active agent roster)

Optional:
  GCHAT_WEBHOOK_URL            - override the hardcoded Chat webhook URL
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

import pytz
import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

EASTERN = pytz.timezone("America/New_York")
FUB_BASE_URL = "https://api.followupboss.com/v1"

GOAL_CONVERSATIONS = 20
GOAL_APPTS_SET = 5

# Staff excluded from the audit (admin, leadership, lending)
EXCLUDED_AGENTS = {
    "Chad Leonberg",
    "Brittany Leonberg",
    "Dennis Palapar",
    "Danielle Heitner",
}

GCHAT_WEBHOOK_URL = os.environ.get(
    "GCHAT_WEBHOOK_URL",
    "https://chat.googleapis.com/v1/spaces/AAQAutY1kmQ/messages"
    "?key=AIzaSyDdI0hCZtE6vySjMm-WEfRq3CPzqKqqsHI"
    "&token=ZOuzGK-1uOv2D-5Y50ZmvrHZh0wDiHvMRblHlYd-Ufw",
)

TAB_ACTIVITY = "Daily Activity"

# Column indices in Daily Activity (0-based, after header):
# date | agent | dials total | outbound dials | conversations | texts sent |
# appts set | appts met | new leads assigned | notes
COL_DATE = 0
COL_AGENT = 1
COL_CONVERSATIONS = 4
COL_APPTS_SET = 6

# Color thresholds
COLOR_GREEN = "#28a745"
COLOR_YELLOW = "#ffc107"
COLOR_RED = "#dc3545"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{datetime.now(EASTERN):%Y-%m-%d %H:%M:%S %Z}] {msg}", flush=True)


def die(msg: str) -> None:
    log(f"FATAL: {msg}")
    sys.exit(1)


def get_week_range() -> tuple[str, str]:
    """Return (monday_str, sunday_str) for the current Mon-Sun week in ET."""
    today = datetime.now(EASTERN).date()
    monday = today - timedelta(days=today.weekday())  # weekday(): 0=Mon
    sunday = monday + timedelta(days=6)
    return monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")


def classify(convos: int, appts: int) -> tuple[str, str, str]:
    """
    Return (label, color_hex, emoji) based on weekly goals.

    Priority:
      1. RED/Off  – convos < 15 OR appts < 3
      2. GREEN    – convos >= 20 AND appts >= 5
      3. YELLOW   – everything between (close to goal)
    """
    if convos < 15 or appts < 3:
        return "Off", COLOR_RED, "🔴"
    if convos >= GOAL_CONVERSATIONS and appts >= GOAL_APPTS_SET:
        return "Green", COLOR_GREEN, "🟢"
    return "Yellow", COLOR_YELLOW, "🟡"


# ---------------------------------------------------------------------------
# FUB: active agent roster
# ---------------------------------------------------------------------------

def fetch_active_agents(api_key: str) -> set[str]:
    """
    Query FUB /users and return the set of active agent names,
    excluding the configured staff list.
    """
    session = requests.Session()
    session.auth = (api_key, "")
    session.headers.update({"Accept": "application/json"})

    agents: set[str] = set()
    url = FUB_BASE_URL + "/users"
    params: dict | None = {"limit": 100}
    pages = 0
    while url and pages < 20:
        try:
            resp = session.get(url, params=params, timeout=30)
            if resp.status_code == 429:
                time.sleep(5)
                continue
            resp.raise_for_status()
        except requests.RequestException as exc:
            log(f"FUB /users error: {exc} — roster will fall back to sheet data")
            return set()
        data = resp.json()
        for u in data.get("users", []) or []:
            if str(u.get("status", "")).lower() == "active":
                name = u.get("name") or ""
                if name and name not in EXCLUDED_AGENTS:
                    agents.add(name)
        next_link = (data.get("_metadata") or {}).get("nextLink")
        url = next_link
        params = None
        pages += 1
    log(f"Active agents from FUB: {len(agents)}")
    return agents


# ---------------------------------------------------------------------------
# Google Sheets reader
# ---------------------------------------------------------------------------

def read_sheet(sheet_id: str, creds_json: str) -> list[list]:
    """Return all rows (including header) from the Daily Activity tab."""
    info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    resp = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=f"'{TAB_ACTIVITY}'")
        .execute()
    )
    return resp.get("values", [])


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate_weekly(
    rows: list[list],
    week_start: str,
    week_end: str,
    roster: set[str],
) -> dict[str, dict]:
    """
    Sum conversations and appointments set per agent for the Mon-Sun window.
    Agents in EXCLUDED_AGENTS are dropped.  If roster is non-empty, any agent
    in the roster but absent from the sheet is added with zeros so they appear
    in the audit even if they had no activity.
    """
    totals: dict[str, dict] = defaultdict(lambda: {"conversations": 0, "appts_set": 0})

    for row in rows[1:]:  # skip header row
        if len(row) <= COL_AGENT:
            continue
        date = row[COL_DATE] if len(row) > COL_DATE else ""
        agent = row[COL_AGENT] if len(row) > COL_AGENT else ""
        if not (week_start <= date <= week_end):
            continue
        if not agent or agent in EXCLUDED_AGENTS:
            continue
        convos = _safe_int(row, COL_CONVERSATIONS)
        appts = _safe_int(row, COL_APPTS_SET)
        totals[agent]["conversations"] += convos
        totals[agent]["appts_set"] += appts

    # Ensure every active roster agent appears (even with all-zero activity)
    for name in roster:
        if name not in totals:
            totals[name] = {"conversations": 0, "appts_set": 0}

    return dict(totals)


def _safe_int(row: list, col: int) -> int:
    try:
        return int(row[col]) if len(row) > col and row[col] != "" else 0
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# Google Chat message builder
# ---------------------------------------------------------------------------

def build_gchat_card(
    agent_metrics: dict[str, dict],
    week_start: str,
    week_end: str,
    report_date: str,
) -> dict:
    """
    Build a Google Chat cardsV2 payload with color-coded agent sections.
    """
    green: list[tuple] = []
    yellow: list[tuple] = []
    red: list[tuple] = []

    for agent in sorted(agent_metrics):
        m = agent_metrics[agent]
        convos = m["conversations"]
        appts = m["appts_set"]
        label, color, emoji = classify(convos, appts)
        entry = (agent, convos, appts, label, color, emoji)
        if label == "Green":
            green.append(entry)
        elif label == "Yellow":
            yellow.append(entry)
        else:
            red.append(entry)

    sections = []

    def _section(emoji: str, header: str, agents: list[tuple]) -> dict | None:
        if not agents:
            return None
        widgets = []
        for agent, convos, appts, label, color, em in agents:
            widgets.append({
                "decoratedText": {
                    "topLabel": agent,
                    "text": (
                        f'<font color="{color}"><b>{convos} Conversations'
                        f" &nbsp;·&nbsp; {appts} Appts Set</b></font>"
                    ),
                    "bottomLabel": f"{em} Status: {label}",
                    "startIcon": {"knownIcon": "PERSON"},
                }
            })
        return {"header": f"{emoji} {header}", "collapsible": False, "widgets": widgets}

    for entry in [
        _section("🟢", "GREEN — Met/Above Weekly Goal (20+ Convos & 5+ Appts)", green),
        _section("🟡", "YELLOW — Close to Weekly Goal (15–19 Convos or 3–4 Appts)", yellow),
        _section("🔴", "OFF — Below Weekly Goal (&lt;15 Convos or &lt;3 Appts)", red),
    ]:
        if entry:
            sections.append(entry)

    if not sections:
        sections = [{
            "widgets": [{"textParagraph": {"text": "No agent data found for this week."}}]
        }]

    # Summary footer
    total_agents = len(agent_metrics)
    sections.append({
        "hasDivider": True,
        "widgets": [{
            "textParagraph": {
                "text": (
                    f"<i>Week: {week_start} → {week_end} &nbsp;|&nbsp; "
                    f"Agents evaluated: {total_agents} &nbsp;|&nbsp; "
                    f"Goals: {GOAL_CONVERSATIONS} Conversations, "
                    f"{GOAL_APPTS_SET} Appointments Set</i>"
                )
            }
        }],
    })

    return {
        "cardsV2": [{
            "cardId": "weekly-fub-audit",
            "card": {
                "header": {
                    "title": f"Weekly FUB Activity Audit: {report_date}",
                    "subtitle": f"Week of {week_start} — {week_end}",
                    "imageUrl": (
                        "https://fonts.gstatic.com/s/i/short-term/release/"
                        "googlesymbols/bar_chart/default/48px.svg"
                    ),
                    "imageType": "CIRCLE",
                },
                "sections": sections,
            },
        }]
    }


# ---------------------------------------------------------------------------
# Send to Google Chat
# ---------------------------------------------------------------------------

def send_to_gchat(payload: dict) -> None:
    resp = requests.post(
        GCHAT_WEBHOOK_URL,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        die(f"Google Chat webhook failed: HTTP {resp.status_code} — {resp.text[:300]}")
    log(f"Google Chat message sent (HTTP {resp.status_code})")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    sheet_id = os.environ.get("SHEET_ID")
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    fub_api_key = os.environ.get("FUB_API_KEY", "")

    if not sheet_id:
        die("SHEET_ID env var is not set")
    if not creds_json:
        die("GOOGLE_SERVICE_ACCOUNT_JSON env var is not set")

    week_start, week_end = get_week_range()
    report_date = datetime.now(EASTERN).strftime("%Y-%m-%d")

    log(f"Weekly audit for {week_start} → {week_end} (report date {report_date})")

    # Fetch FUB roster (best-effort; falls back to sheet-only agents on failure)
    roster: set[str] = set()
    if fub_api_key:
        roster = fetch_active_agents(fub_api_key)
    else:
        log("FUB_API_KEY not set — roster will rely on sheet data only")

    # Read sheet and aggregate
    log("Reading Daily Activity sheet …")
    rows = read_sheet(sheet_id, creds_json)
    log(f"  Rows read (including header): {len(rows)}")

    agent_metrics = aggregate_weekly(rows, week_start, week_end, roster)
    log(f"  Agents in scope: {len(agent_metrics)}")

    for agent in sorted(agent_metrics):
        m = agent_metrics[agent]
        label, _, em = classify(m["conversations"], m["appts_set"])
        log(
            f"  {em} {agent}: "
            f"convos={m['conversations']} appts={m['appts_set']} → {label}"
        )

    # Build and send the Google Chat card
    payload = build_gchat_card(agent_metrics, week_start, week_end, report_date)
    log("Sending Google Chat message …")
    send_to_gchat(payload)

    log("Weekly audit complete.")


if __name__ == "__main__":
    main()
