#!/usr/bin/env python3
"""
Daily FUB activity audit -> Google Chat.

Reads yesterday's per-agent row out of the "Daily Activity" tab (written by
fub_sheets_sync.py) plus trailing-7-day context out of "Leaderboard", grades
every agent against the daily goals (weekly targets / 5: 4 conversations, 1
appointment set), and posts a color-coded report to a Google Chat webhook.

Excludes admin / leadership / lending staff, who aren't graded on sales
activity: Chad Leonberg, Brittany Leonberg, Dennis Palapar, Danielle Heitner.

Auth / secrets (read from env, same as fub_sheets_sync.py plus one more):
  SHEET_ID                    - source spreadsheet ID
  GOOGLE_SERVICE_ACCOUNT_JSON - service-account credentials, as a raw JSON string
  FUB_CHAT_WEBHOOK_URL        - Google Chat incoming webhook URL

Run:
  python fub_daily_audit.py            # scheduled: gated to 8pm Eastern
  python fub_daily_audit.py --force    # bypass the gate (manual/testing)
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
    TAB_LEADERBOARD,
    SheetWriter,
    die,
    log,
    num,
)

# Scheduled runs proceed only at this wall-clock Eastern hour (8pm). The
# workflow fires at two UTC times to cover both sides of DST; whichever one
# lands on hour 20 Eastern does the send, the other is a no-op. Mirrors the
# DST-proof gating approach in fub_sheets_sync.py.
RUN_HOUR_ET = 20

# Daily goals = weekly leaderboard targets (20 conversations, 5 appts set) / 5.
DAILY_CONVOS_GOAL = 4
DAILY_APPTS_GOAL = 1

# Staff excluded from the audit: admin, leadership, lending. Matched
# case-insensitively against the "agent" name recorded in the sheet.
EXCLUDED_AGENTS = {
    "chad leonberg",
    "brittany leonberg",
    "dennis palapar",
    "danielle heitner",
}

STATUS_GREEN = {"label": "Green", "color": "#28a745"}
STATUS_YELLOW = {"label": "Yellow", "color": "#ffc107"}
STATUS_OFF = {"label": "Off", "color": "#dc3545"}


def classify(conversations: int, appts_set: int, outbound_dials: int) -> dict:
    """
    GREEN  : daily goal met on both fronts (4+ conversations AND 1+ appt set).
    YELLOW : close to goal (2-3 conversations), or the conversation goal was
             hit but the appointment wasn't, or low conversations but real
             outbound dial effort was still shown.
    OFF    : 0-1 conversations with no offsetting dial effort, i.e. zero/near-
             zero activity for the day.
    """
    if conversations >= DAILY_CONVOS_GOAL and appts_set >= DAILY_APPTS_GOAL:
        return STATUS_GREEN
    if conversations >= DAILY_CONVOS_GOAL:
        return STATUS_YELLOW  # hit the convo goal, missed the appt goal
    if conversations >= 2:
        return STATUS_YELLOW
    if outbound_dials > 0:
        return STATUS_YELLOW  # low conversations, but still dialing
    return STATUS_OFF


def target_date_et() -> "datetime.date":
    """The day being audited: the day before this run (a fully completed day)."""
    return (datetime.now(EASTERN) - timedelta(days=1)).date()


def load_agent_rows(writer: SheetWriter, date_str: str) -> list[dict]:
    activity_cols = HEADERS[TAB_ACTIVITY]
    rows = writer.read_data(TAB_ACTIVITY)
    out = []
    for row in rows:
        if len(row) < len(activity_cols):
            row = row + [""] * (len(activity_cols) - len(row))
        if row[0] != date_str:
            continue
        name = row[1]
        if name.strip().lower() in EXCLUDED_AGENTS:
            continue
        out.append({
            "agent": name,
            "dials_total": int(num(row[2])),
            "outbound_dials": int(num(row[3])),
            "conversations": int(num(row[4])),
            "appts_set": int(num(row[6])),
        })
    return out


def load_weekly_context(writer: SheetWriter) -> dict[str, dict]:
    """agent -> {t7 conversations, t7 appts set} from the Leaderboard tab."""
    rows = writer.read_data(TAB_LEADERBOARD)
    ctx = {}
    for row in rows:
        if len(row) < 3:
            continue
        ctx[row[0]] = {"t7_convos": int(num(row[1])), "t7_appts": int(num(row[2]))}
    return ctx


def escape_html(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_card(date_str: str, title_date: str, graded: list[dict]) -> dict:
    counts = {"Green": 0, "Yellow": 0, "Off": 0}
    for g in graded:
        counts[g["status"]["label"]] += 1

    summary_line = (
        f'<font color="{STATUS_GREEN["color"]}"><b>Green: {counts["Green"]}</b></font>'
        f'  &nbsp;|&nbsp;  '
        f'<font color="{STATUS_YELLOW["color"]}"><b>Yellow: {counts["Yellow"]}</b></font>'
        f'  &nbsp;|&nbsp;  '
        f'<font color="{STATUS_OFF["color"]}"><b>Off: {counts["Off"]}</b></font>'
    )

    sections = [{
        "header": f"Activity for {date_str}",
        "widgets": [{"textParagraph": {"text": summary_line}}],
    }]

    # Worst-first ordering so problems surface at the top of the message.
    order = {"Off": 0, "Yellow": 1, "Green": 2}
    graded_sorted = sorted(graded, key=lambda g: (order[g["status"]["label"]], g["agent"]))

    group_titles = {"Off": "Off Daily Goal", "Yellow": "Near Daily Goal", "Green": "Goal Met"}
    for label in ("Off", "Yellow", "Green"):
        members = [g for g in graded_sorted if g["status"]["label"] == label]
        if not members:
            continue
        widgets = []
        for g in members:
            status = g["status"]
            weekly = g.get("weekly")
            weekly_note = ""
            if weekly:
                weekly_note = (f'<br><font color="#888888">This week so far: '
                                f'{weekly["t7_convos"]} conversations, '
                                f'{weekly["t7_appts"]} appts set</font>')
            text = (
                f'<b>{escape_html(g["agent"])}</b> &mdash; '
                f'<font color="{status["color"]}"><b>{status["label"]}</b></font><br>'
                f'Conversations: {g["conversations"]} | Appts Set: {g["appts_set"]} | '
                f'Dials: {g["dials_total"]} ({g["outbound_dials"]} outbound)'
                f'{weekly_note}'
            )
            widgets.append({"textParagraph": {"text": text}})
        sections.append({"header": group_titles[label], "widgets": widgets})

    return {
        "text": f"Daily FUB Activity Audit: {title_date}",
        "cardsV2": [{
            "cardId": "daily-fub-audit",
            "card": {
                "header": {"title": f"Daily FUB Activity Audit: {title_date}"},
                "sections": sections,
            },
        }],
    }


def send_to_chat(webhook_url: str, payload: dict) -> None:
    resp = requests.post(webhook_url, json=payload, timeout=30)
    if resp.status_code >= 300:
        die(f"Google Chat webhook returned {resp.status_code}: {resp.text[:500]}")


def run(force: bool) -> None:
    sheet_id = os.environ.get("SHEET_ID")
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    webhook_url = os.environ.get("FUB_CHAT_WEBHOOK_URL")
    if not sheet_id:
        die("SHEET_ID is not set")
    if not creds_json:
        die("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
    if not webhook_url:
        die("FUB_CHAT_WEBHOOK_URL is not set")

    maybe_gate_eastern_hour(force)

    writer = SheetWriter(sheet_id, creds_json)

    target_date = target_date_et()
    date_str = target_date.strftime("%Y-%m-%d")
    title_date = datetime.now(EASTERN).strftime("%B %-d, %Y")

    agent_rows = load_agent_rows(writer, date_str)
    if not agent_rows:
        die(f"No Daily Activity rows found for {date_str}; "
            f"has fub_sheets_sync.py run since then?")

    weekly_ctx = load_weekly_context(writer)

    graded = []
    for row in agent_rows:
        status = classify(row["conversations"], row["appts_set"], row["outbound_dials"])
        graded.append({**row, "status": status, "weekly": weekly_ctx.get(row["agent"])})

    payload = build_card(date_str, title_date, graded)
    send_to_chat(webhook_url, payload)

    counts = {"Green": 0, "Yellow": 0, "Off": 0}
    for g in graded:
        counts[g["status"]["label"]] += 1
    log(f"Audit for {date_str} sent: {len(graded)} agents "
        f"(green={counts['Green']} yellow={counts['Yellow']} off={counts['Off']})")


def maybe_gate_eastern_hour(force: bool) -> None:
    """Scheduled runs proceed only at 8pm Eastern; dispatch/--force bypass it."""
    if force:
        return
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return
    hour = datetime.now(EASTERN).hour
    if hour != RUN_HOUR_ET:
        log(f"Scheduled run at Eastern hour {hour:02d}:00, not {RUN_HOUR_ET}:00; skipping.")
        sys.exit(0)


def main() -> None:
    ap = argparse.ArgumentParser(description="FUB daily activity audit -> Google Chat")
    ap.add_argument("--force", action="store_true",
                    help="Bypass the 8pm-Eastern scheduling gate")
    args = ap.parse_args()
    run(args.force)
    log("Done.")


if __name__ == "__main__":
    main()
