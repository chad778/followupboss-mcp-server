#!/usr/bin/env python3
"""
Daily FUB agent-activity audit -> Google Chat.

Reads yesterday's row (Eastern calendar day) for every agent out of the
"Daily Activity" tab that `fub_sheets_sync.py` maintains, scores each agent
against the daily goals (weekly targets / 5 = 4 conversations + 1 appointment
set), and posts a color-coded card to a Google Chat incoming webhook.

Excludes admin/leadership/lending staff by name (not agents being coached on
dial activity): Chad Leonberg, Brittany Leonberg, Dennis Palapar, Danielle
Heitner.

Secrets (read from env, set as GitHub repo secrets):
  GOOGLE_SERVICE_ACCOUNT_JSON - service-account credentials, as a raw JSON string
  SHEET_ID                    - source spreadsheet ID (same sheet fub_sheets_sync.py writes)
  GOOGLE_CHAT_WEBHOOK_URL     - Google Chat incoming webhook URL

Run:
  python fub_daily_audit.py                  # audits yesterday (Eastern)
  python fub_daily_audit.py --date 2026-07-07
  python fub_daily_audit.py --force          # bypass the 8pm-Eastern gate
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

import pytz
import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

EASTERN = pytz.timezone("America/New_York")
TAB_ACTIVITY = "Daily Activity"

# Scheduled runs proceed only at this wall-clock Eastern hour (DST-proof gate,
# same technique as fub_sheets_sync.py's business-hour gate).
RUN_HOUR_ET = 20

# Daily goals = weekly targets (fub_sheets_sync.py's leaderboard pace) / 5.
DAILY_CONVERSATIONS_GOAL = 4
DAILY_APPTS_SET_GOAL = 1

# Admin / leadership / lending staff excluded from the agent audit.
EXCLUDED_AGENTS = {
    "chad leonberg",
    "brittany leonberg",
    "dennis palapar",
    "danielle heitner",
}

STATUS_COLORS = {
    "Green": "#28a745",
    "Yellow": "#ffc107",
    "Off": "#dc3545",
}


def log(msg: str) -> None:
    print(f"[{datetime.now(EASTERN):%Y-%m-%d %H:%M:%S %Z}] {msg}", flush=True)


def die(msg: str, code: int = 1) -> None:
    log(f"FATAL: {msg}")
    sys.exit(code)


def maybe_gate_eastern_hour(force: bool) -> None:
    """Scheduled runs proceed only at the 8pm-Eastern hour; dispatch/--force bypass it."""
    if force:
        return
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return
    hour = datetime.now(EASTERN).hour
    if hour != RUN_HOUR_ET:
        log(f"Scheduled run at Eastern hour {hour:02d}:00, not {RUN_HOUR_ET}:00; skipping.")
        sys.exit(0)


def read_activity_rows(sheet_id: str, creds_json: str) -> list[list]:
    try:
        info = json.loads(creds_json)
    except json.JSONDecodeError as e:
        die(f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {e}")
    try:
        creds = Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
        )
        svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    except Exception as e:
        die(f"Google auth failed: {e}")
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{TAB_ACTIVITY}'"
    ).execute()
    rows = resp.get("values", [])
    return rows[1:] if rows else []  # drop header


def num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def score_agent(conversations: int, appts_set: int, outbound_dials: int) -> str:
    """
    GREEN: 4+ conversations AND 1+ appointments set (daily goal fully met).
    YELLOW: close to goal -- 2-3 conversations, or any dial effort short of
      the conversation goal (also covers 4+ conversations w/o an appt set,
      since only the full green condition falls out of this bucket).
    RED ("Off"): 0-1 conversations and no outbound dial effort at all.
    """
    if conversations >= DAILY_CONVERSATIONS_GOAL and appts_set >= DAILY_APPTS_SET_GOAL:
        return "Green"
    if conversations >= 2 or outbound_dials > 0:
        return "Yellow"
    return "Off"


def build_audit(rows: list[list], target_date: str) -> list[dict]:
    """One entry per agent (excluding leadership/admin/lenders) for target_date."""
    agents: dict[str, dict] = {}
    for row in rows:
        if len(row) < 7 or row[0] != target_date:
            continue
        name = row[1]
        if name.strip().lower() in EXCLUDED_AGENTS:
            continue
        dials_total = int(num(row[2]))
        outbound_dials = int(num(row[3]))
        conversations = int(num(row[4]))
        appts_set = int(num(row[6]))
        agents[name] = {
            "agent": name,
            "dials_total": dials_total,
            "outbound_dials": outbound_dials,
            "conversations": conversations,
            "appts_set": appts_set,
            "status": score_agent(conversations, appts_set, outbound_dials),
        }
    return [agents[name] for name in sorted(agents)]


def font(color: str, text: str) -> str:
    return f'<font color="{color}">{text}</font>'


def build_chat_card(target_date: str, audit: list[dict]) -> dict:
    counts = {"Green": 0, "Yellow": 0, "Off": 0}
    widgets = []
    for a in audit:
        counts[a["status"]] += 1
        color = STATUS_COLORS[a["status"]]
        widgets.append({
            "decoratedText": {
                "topLabel": a["agent"],
                "text": (
                    f'{font(color, a["status"])} &middot; '
                    f'Conversations: {a["conversations"]} &middot; '
                    f'Appts Set: {a["appts_set"]} &middot; '
                    f'Outbound Dials: {a["outbound_dials"]} &middot; '
                    f'Total Dials: {a["dials_total"]}'
                ),
                "wrapText": True,
            }
        })
    if not widgets:
        widgets.append({"textParagraph": {"text": "No active agents to report for this date."}})

    green_label = f'{counts["Green"]} Green'
    yellow_label = f'{counts["Yellow"]} Yellow'
    off_label = f'{counts["Off"]} Off'
    summary_text = " &middot; ".join([
        font(STATUS_COLORS["Green"], green_label),
        font(STATUS_COLORS["Yellow"], yellow_label),
        font(STATUS_COLORS["Off"], off_label),
    ])

    return {
        "cardsV2": [{
            "cardId": f"fub-daily-audit-{target_date}",
            "card": {
                "header": {
                    "title": f"Daily FUB Activity Audit: {target_date}",
                    "subtitle": "Daily goal: 4+ Conversations & 1+ Appointment Set",
                },
                "sections": [
                    {"widgets": [{"textParagraph": {"text": summary_text}}]},
                    {"header": "Agents", "widgets": widgets},
                ],
            },
        }]
    }


def post_to_chat(webhook_url: str, payload: dict) -> None:
    resp = requests.post(webhook_url, json=payload, timeout=30)
    if resp.status_code >= 400:
        die(f"Google Chat webhook returned HTTP {resp.status_code}: {resp.text[:300]}")
    log(f"Posted to Google Chat (HTTP {resp.status_code})")


def run(target_date: str | None) -> None:
    sheet_id = os.environ.get("SHEET_ID")
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    webhook_url = os.environ.get("GOOGLE_CHAT_WEBHOOK_URL")
    if not sheet_id:
        die("SHEET_ID is not set")
    if not creds_json:
        die("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
    if not webhook_url:
        die("GOOGLE_CHAT_WEBHOOK_URL is not set")

    if not target_date:
        target_date = (datetime.now(EASTERN) - timedelta(days=1)).strftime("%Y-%m-%d")

    log(f"Auditing agent activity for {target_date}")
    rows = read_activity_rows(sheet_id, creds_json)
    audit = build_audit(rows, target_date)
    log(f"Agents in audit: {len(audit)} (excluded: {sorted(EXCLUDED_AGENTS)})")

    payload = build_chat_card(target_date, audit)
    post_to_chat(webhook_url, payload)


def main() -> None:
    ap = argparse.ArgumentParser(description="Daily FUB agent-activity audit -> Google Chat")
    ap.add_argument("--date", help="Target date (YYYY-MM-DD, Eastern). Defaults to yesterday.")
    ap.add_argument("--force", action="store_true", help="Bypass the 8pm-Eastern scheduling gate")
    args = ap.parse_args()

    maybe_gate_eastern_hour(args.force)
    run(args.date)
    log("Done.")


if __name__ == "__main__":
    main()
