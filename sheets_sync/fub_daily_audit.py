#!/usr/bin/env python3
"""
Daily agent-activity audit -> Google Chat.

Reads yesterday's per-agent rows out of the "Daily Activity" tab that
fub_sheets_sync.py already keeps up to date, scores each active agent against
daily goals (the same weekly pace targets used on the Leaderboard tab, divided
by 5), and posts a color-coded summary card to a Google Chat space via
incoming webhook.

This script only reads the spreadsheet -- it never talks to the FUB API and
never writes to the sheet. It is meant to run once a day, after the sync's
final pass for "yesterday" has already landed (see fub-sheets-sync.yml).

Secrets (read from env, set as GitHub repo secrets):
  SHEET_ID                    - same spreadsheet fub_sheets_sync.py writes to
  GOOGLE_SERVICE_ACCOUNT_JSON - same service-account credentials (read access
                                 is enough, but the existing key already has it)
  GOOGLE_CHAT_WEBHOOK_URL     - Google Chat incoming-webhook URL (includes its
                                 own key/token query params; keep this a secret,
                                 never commit it)

Run:
  python fub_daily_audit.py            # audits yesterday (Eastern), posts to Chat
  python fub_daily_audit.py --dry-run  # prints the report instead of posting
  python fub_daily_audit.py --force    # bypass the 8pm-Eastern scheduling gate
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from fub_sheets_sync import (
    EASTERN,
    HEADERS,
    TAB_ACTIVITY,
    TARGET_APPTS_SET_PER_WEEK,
    TARGET_CONVOS_PER_WEEK,
    log,
)

# Agents excluded from the audit: admin, leadership, and lending staff.
EXCLUDED_AGENTS = {
    "chad leonberg",
    "brittany leonberg",
    "dennis palapar",
    "danielle heitner",
}

# Daily goals = weekly pace targets / 5 (a 5-day work week).
DAILY_CONVOS_GOAL = TARGET_CONVOS_PER_WEEK / 5   # 4
DAILY_APPTS_GOAL = TARGET_APPTS_SET_PER_WEEK / 5  # 1

GREEN = "#28a745"
YELLOW = "#ffc107"
RED = "#dc3545"

# The audit fires once, at 8pm Eastern.
RUN_HOUR_ET = 20


def die(msg: str, code: int = 1) -> None:
    log(f"FATAL: {msg}")
    sys.exit(code)


def maybe_gate_eastern_hour(force: bool) -> None:
    """Scheduled runs proceed only at the 8pm-Eastern wall clock hour; manual
    dispatch and --force always proceed. Mirrors fub_sheets_sync.py's DST-proof
    gating (the workflow fires at two UTC times to cover both EST and EDT)."""
    if force:
        return
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return
    hour = datetime.now(EASTERN).hour
    if hour != RUN_HOUR_ET:
        log(f"Scheduled run at Eastern hour {hour:02d}:00, not {RUN_HOUR_ET}:00; skipping.")
        sys.exit(0)


def read_activity_tab(sheet_id: str, creds_json: str) -> list[list]:
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


def score_agent(conversations: int, appts_set: int, outbound_dials: int) -> tuple[str, str]:
    """Returns (color_hex, label) per the daily-goal rules."""
    if conversations >= DAILY_CONVOS_GOAL and appts_set >= DAILY_APPTS_GOAL:
        return GREEN, "Green"
    if (2 <= conversations <= 3) or outbound_dials > 0:
        return YELLOW, "Yellow"
    return RED, "Off"


def build_report(rows_for_date: list[dict]) -> tuple[str, dict[str, int]]:
    """Builds the HTML (Google Chat rich-text) body and a status tally."""
    by_status: dict[str, list[str]] = {"Green": [], "Yellow": [], "Off": []}
    tally = {"Green": 0, "Yellow": 0, "Off": 0}

    for r in rows_for_date:
        color, label = score_agent(r["conversations"], r["appts_set"], r["outbound_dials"])
        tally[label] += 1
        line = (
            f'<font color="{color}"><b>{label}</b></font> — <b>{r["agent"]}</b>: '
            f'{r["conversations"]} conversations, {r["appts_set"]} appts set '
            f'({r["dials_total"]} dials, {r["outbound_dials"]} outbound)'
        )
        by_status[label].append(line)

    sections = []
    for label, color, heading in (
        ("Green", GREEN, "Met/Above Daily Goal"),
        ("Yellow", YELLOW, "Close to Daily Goal"),
        ("Off", RED, "Off Daily Goal"),
    ):
        lines = by_status[label]
        if not lines:
            continue
        sections.append(
            f'<font color="{color}"><b>{label} — {heading} ({len(lines)})</b></font><br>'
            + "<br>".join(lines)
        )

    body = "<br><br>".join(sections) if sections else "No agent activity rows found for this date."
    return body, tally


def post_to_chat(webhook_url: str, title: str, body_html: str) -> None:
    payload = {
        "cardsV2": [{
            "cardId": "daily-fub-audit",
            "card": {
                "header": {"title": title},
                "sections": [{"widgets": [{"textParagraph": {"text": body_html}}]}],
            },
        }]
    }
    resp = requests.post(webhook_url, json=payload, timeout=30)
    if resp.status_code >= 300:
        die(f"Google Chat webhook returned HTTP {resp.status_code}: {resp.text[:300]}")
    log(f"Posted audit to Google Chat (HTTP {resp.status_code})")


def run(dry_run: bool) -> None:
    sheet_id = os.environ.get("SHEET_ID")
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    webhook_url = os.environ.get("GOOGLE_CHAT_WEBHOOK_URL")
    if not sheet_id:
        die("SHEET_ID is not set")
    if not creds_json:
        die("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
    if not dry_run and not webhook_url:
        die("GOOGLE_CHAT_WEBHOOK_URL is not set")

    yesterday = (datetime.now(EASTERN) - timedelta(days=1)).strftime("%Y-%m-%d")
    log(f"Auditing Daily Activity for {yesterday}")

    cols = HEADERS[TAB_ACTIVITY]
    data_rows = read_activity_tab(sheet_id, creds_json)

    rows_for_date = []
    for row in data_rows:
        row = row + [""] * (len(cols) - len(row))  # pad short rows
        rec = dict(zip(cols, row))
        if rec.get("date") != yesterday:
            continue
        agent = (rec.get("agent") or "").strip()
        if agent.lower() in EXCLUDED_AGENTS:
            continue
        def as_int(v):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return 0
        rows_for_date.append({
            "agent": agent,
            "dials_total": as_int(rec.get("dials total")),
            "outbound_dials": as_int(rec.get("outbound dials")),
            "conversations": as_int(rec.get("conversations")),
            "appts_set": as_int(rec.get("appts set")),
        })

    rows_for_date.sort(key=lambda r: r["agent"])
    body_html, tally = build_report(rows_for_date)
    title = f"Daily FUB Activity Audit: {yesterday}"

    log(f"Agents audited: {len(rows_for_date)} | "
        f"Green={tally['Green']} Yellow={tally['Yellow']} Off={tally['Off']}")

    if dry_run:
        print(f"TITLE: {title}\n")
        print(body_html.replace("<br>", "\n"))
        return

    post_to_chat(webhook_url, title, body_html)


def main() -> None:
    ap = argparse.ArgumentParser(description="FUB daily agent-activity audit -> Google Chat")
    ap.add_argument("--force", action="store_true", help="Bypass the 8pm-Eastern scheduling gate")
    ap.add_argument("--dry-run", action="store_true", help="Print the report instead of posting it")
    args = ap.parse_args()

    maybe_gate_eastern_hour(args.force)
    run(args.dry_run)
    log("Done.")


if __name__ == "__main__":
    main()
