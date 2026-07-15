#!/usr/bin/env python3
"""
Daily FUB Activity Audit -> Google Chat.

Reads yesterday's row for every agent out of the "Daily Activity" tab that
`fub_sheets_sync.py` maintains, scores each agent against the daily goals
(the same weekly Leaderboard targets from `fub_sheets_sync.py`, divided by
5), and posts a color-coded summary to a Google Chat space.

This script does NOT talk to the Follow Up Boss API. It only reads the
sheet `fub_sheets_sync.py` already keeps current, so it has no FUB_API_KEY
dependency -- just the same Google service-account creds and sheet ID.

Secrets (read from env, set as GitHub repo secrets):
  GOOGLE_SERVICE_ACCOUNT_JSON - service-account credentials, as a raw JSON string
  SHEET_ID                    - source spreadsheet ID
  GOOGLE_CHAT_WEBHOOK_URL     - incoming webhook URL for the target Chat space
                                (the URL itself is a bearer credential --
                                treat it like any other secret, never commit it)

Run:
  python daily_audit.py                  # audits yesterday (Eastern), scheduled gate applies
  python daily_audit.py --force          # bypass the 8pm-Eastern scheduling gate
  python daily_audit.py --date 2026-07-14 --force   # audit a specific date (manual re-run)
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

import requests

from fub_sheets_sync import (
    EASTERN,
    HEADERS,
    TAB_ACTIVITY,
    TARGET_APPTS_SET_PER_WEEK,
    TARGET_CONVOS_PER_WEEK,
    SheetWriter,
    die,
    log,
    num,
)

# ---------------------------------------------------------------------------
# Constants / tunables
# ---------------------------------------------------------------------------

# Daily goals are the weekly Leaderboard targets (fub_sheets_sync.py) / 5.
DAILY_CONVOS_GOAL = TARGET_CONVOS_PER_WEEK / 5   # 20 / 5 = 4
DAILY_APPTS_GOAL = TARGET_APPTS_SET_PER_WEEK / 5  # 5 / 5 = 1

# Below this many outbound dials, 0-1 conversations counts as "no effort" (Off)
# rather than "Close to Daily Goal" (Yellow). Not specified upstream -- flagged
# here as an assumption to confirm with Chad, same as the ⚠️ notes in the main
# sync's README.
OUTBOUND_EFFORT_THRESHOLD = 20

# Admin / leadership / lending staff excluded from the agent audit.
EXCLUDED_AGENTS = {
    "Chad Leonberg",
    "Brittany Leonberg",
    "Dennis Palapar",
    "Danielle Heitner",
}

STATUS_GREEN = "Green"
STATUS_YELLOW = "Yellow"
STATUS_OFF = "Off"

STATUS_COLOR = {
    STATUS_GREEN: "#28a745",
    STATUS_YELLOW: "#ffc107",
    STATUS_OFF: "#dc3545",
}

# Scheduled runs proceed only at this Eastern wall-clock hour (8pm). The
# workflow fires hourly (UTC) and this gate makes it DST-proof, same pattern
# as fub_sheets_sync.maybe_gate_eastern_hour.
RUN_HOUR_ET = 20


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def classify(conversations: float, appts_set: float, outbound_dials: float) -> str:
    """
    Green: 4+ conversations AND 1+ appointments set.
    Yellow: 2-3 conversations, OR 0-1 conversations backed by real outbound
      dial effort (>= OUTBOUND_EFFORT_THRESHOLD).
    Off: everything else (0-1 conversations with little/no dialing, or zero
      activity). Note: an agent who clears the conversation goal but sets no
      appointment (e.g. 5 conversations, 0 appts) does not fit any bucket in
      the spec as given and falls through to Off here -- confirm with Chad if
      that should instead be Yellow.
    """
    if conversations >= DAILY_CONVOS_GOAL and appts_set >= DAILY_APPTS_GOAL:
        return STATUS_GREEN
    if 2 <= conversations <= 3 or outbound_dials >= OUTBOUND_EFFORT_THRESHOLD:
        return STATUS_YELLOW
    return STATUS_OFF


def rows_for_date(activity_rows: list[list], target_date: str) -> list[dict]:
    """Filter the Daily Activity rows to target_date, minus excluded staff."""
    cols = HEADERS[TAB_ACTIVITY]
    out = []
    for row in activity_rows:
        if len(row) < len(cols):
            row = row + [""] * (len(cols) - len(row))
        record = dict(zip(cols, row))
        if record["date"] != target_date:
            continue
        if record["agent"] in EXCLUDED_AGENTS:
            continue
        record["dials total"] = num(record["dials total"])
        record["outbound dials"] = num(record["outbound dials"])
        record["conversations"] = num(record["conversations"])
        record["appts set"] = num(record["appts set"])
        record["appts met"] = num(record["appts met"])
        record["status"] = classify(
            record["conversations"], record["appts set"], record["outbound dials"]
        )
        out.append(record)
    out.sort(key=lambda r: r["agent"])
    return out


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def format_agent_line(r: dict) -> str:
    return (
        f"<b>{r['agent']}</b> &mdash; "
        f"{int(r['conversations'])} conversations, {int(r['appts set'])} appts set "
        f"({int(r['outbound dials'])} outbound dials)"
    )


def build_section(status: str, label: str, records: list[dict]) -> str:
    color = STATUS_COLOR[status]
    if not records:
        body = "<i>none</i>"
    else:
        body = "<br>".join(format_agent_line(r) for r in records)
    return f'<font color="{color}"><b>{label}</b></font><br>{body}'


def build_chat_payload(title: str, target_date: str, records: list[dict]) -> dict:
    green = [r for r in records if r["status"] == STATUS_GREEN]
    yellow = [r for r in records if r["status"] == STATUS_YELLOW]
    off = [r for r in records if r["status"] == STATUS_OFF]

    widgets = [
        {"textParagraph": {"text": f"Daily goals: {int(DAILY_CONVOS_GOAL)}+ conversations, "
                                    f"{int(DAILY_APPTS_GOAL)}+ appointment set &mdash; "
                                    f"reporting on <b>{target_date}</b>."}},
        {"textParagraph": {"text": build_section(STATUS_GREEN, "Green", green)}},
        {"textParagraph": {"text": build_section(STATUS_YELLOW, "Yellow", yellow)}},
        {"textParagraph": {"text": build_section(STATUS_OFF, "Off", off)}},
    ]

    return {
        "cardsV2": [
            {
                "cardId": "fub-daily-audit",
                "card": {
                    "header": {"title": title},
                    "sections": [{"widgets": widgets}],
                },
            }
        ]
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def maybe_gate_eastern_hour(force: bool) -> None:
    """Scheduled runs proceed only at 8pm Eastern; manual/--force bypass."""
    if force:
        return
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return
    hour = datetime.now(EASTERN).hour
    if hour != RUN_HOUR_ET:
        log(f"Scheduled run at Eastern hour {hour:02d}:00, not {RUN_HOUR_ET}:00; skipping.")
        sys.exit(0)


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

    now_et = datetime.now(EASTERN)
    if target_date is None:
        target_date = (now_et - timedelta(days=1)).strftime("%Y-%m-%d")

    writer = SheetWriter(sheet_id, creds_json)
    activity_rows = writer.read_data(TAB_ACTIVITY)
    log(f"Daily Activity rows read: {len(activity_rows)}")

    records = rows_for_date(activity_rows, target_date)
    if not records:
        die(f"No Daily Activity rows found for {target_date} (after exclusions); aborting")

    counts = {
        STATUS_GREEN: sum(1 for r in records if r["status"] == STATUS_GREEN),
        STATUS_YELLOW: sum(1 for r in records if r["status"] == STATUS_YELLOW),
        STATUS_OFF: sum(1 for r in records if r["status"] == STATUS_OFF),
    }
    log(f"Audited {len(records)} agents for {target_date}: "
        f"green={counts[STATUS_GREEN]} yellow={counts[STATUS_YELLOW]} off={counts[STATUS_OFF]}")

    title = f"Daily FUB Activity Audit: {now_et.strftime('%B %d, %Y')}"
    payload = build_chat_payload(title, target_date, records)

    resp = requests.post(webhook_url, json=payload, timeout=30)
    if resp.status_code >= 300:
        die(f"Google Chat webhook post failed: HTTP {resp.status_code}: {resp.text[:300]}")
    log("Posted daily audit to Google Chat.")


def main() -> None:
    ap = argparse.ArgumentParser(description="FUB Daily Activity Audit -> Google Chat")
    ap.add_argument("--date", default=None,
                    help="Audit a specific date (YYYY-MM-DD) instead of yesterday (Eastern)")
    ap.add_argument("--force", action="store_true",
                    help="Bypass the 8pm-Eastern scheduling gate")
    args = ap.parse_args()

    maybe_gate_eastern_hour(args.force)
    run(args.date)
    log("Done.")


if __name__ == "__main__":
    main()
