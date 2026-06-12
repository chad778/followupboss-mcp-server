# Follow Up Boss → Google Sheets Sync

A scheduled, headless sync that pulls Follow Up Boss (FUB) data **by agent** into
a single Google Spreadsheet, giving a real-estate team operator a daily
agent-level activity and pipeline dashboard. It runs entirely on **GitHub
Actions** — no local execution required.

- **One-time backfill** of the trailing 30 days (triggered manually from the
  Actions tab).
- **Automatic daily run** locked to **10pm America/New_York**, year round.

> ⚠️ **8pm vs 10pm:** the original brief says "8pm Eastern" in the goal but the
> detailed scheduling step specifies 10pm (`hour == 22`, cron at 02:00 & 03:00
> UTC). This implementation uses **10pm**. To switch to 8pm, change
> `RUN_HOUR_ET = 20` in `fub_sheets_sync.py` **and** the two cron lines in
> `.github/workflows/fub-sheets-sync.yml` to `0 0 * * *` and `0 1 * * *`.

---

## What it writes

One spreadsheet, five tabs (auto-created if missing):

| Tab | Grain | Columns |
| --- | --- | --- |
| **Daily Activity** | agent × day | date, agent, dials total, outbound dials, conversations, texts sent, appts set, appts met, new leads assigned, notes |
| **Pipeline Snapshot** | agent × snapshot date | snapshot date, agent, active deals, active value, under contract count, under contract value, listings count, listings value, projected closings this month |
| **Closings** | appended as deals close | close date, agent, deal name, price, side |
| **Leaderboard** | recomputed every run | agent, t7 conversations, t7 appts set, t30 conversations, t30 closings, convo→appt rate, appt show rate, status |
| **Config** | audit | every resolved mapping (agents, pipelines/stages, outcomes, ponds) + last run timestamp, rows written, endpoint errors |

A **Conversation** is defined exactly as a logged call with `duration ≥ 120`
seconds.

### Leaderboard pace targets

Agents are flagged in the **status** column when below target:

- ≥ 20 conversations / week (trailing 7 days)
- ≥ 5 appointments set / week (trailing 7 days)
- 50–60% appointment show rate (trailing 30 days; flagged below 50%)

---

## Setup checklist

### 1. Create a Google service account

1. Google Cloud Console → **APIs & Services → Enable APIs** → enable **Google
   Sheets API**.
2. **Credentials → Create credentials → Service account.** Name it anything.
3. On the service account → **Keys → Add key → JSON**. Download the JSON file.
4. Open your target Google Sheet → **Share** → paste the service account's
   `client_email` (looks like `name@project.iam.gserviceaccount.com`) → give it
   **Editor** access. *The sync cannot write until the sheet is shared with this
   address.*

### 2. Get the Spreadsheet ID

From the sheet URL:
`https://docs.google.com/spreadsheets/d/`**`THIS_IS_THE_ID`**`/edit`

### 3. Get your FUB API key

FUB → **Settings → API → Create API Key** (or copy an existing one). The sync
uses HTTP Basic auth: API key as username, blank password.

### 4. Add GitHub repo secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
| --- | --- |
| `FUB_API_KEY` | your Follow Up Boss API key |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | the **entire contents** of the downloaded service-account JSON file (paste it raw) |
| `SHEET_ID` | the spreadsheet ID from step 2 |

*(Optional)* If you register an integration with FUB for higher rate limits,
add `FUB_X_SYSTEM` and `FUB_X_SYSTEM_KEY` as secrets and wire them into the
workflow `env:` block.

### 5. Run the one-time backfill

Repo → **Actions → FUB Google Sheets Sync → Run workflow** → set **mode =
`backfill`**, **days = `30`** → **Run**. This builds all tabs from scratch.

### 6. Let the daily run take over

The schedule is already active. Every night it fires at 02:00 and 03:00 UTC;
the script runs only on the one that is 10pm Eastern, computes **yesterday**,
appends activity + a fresh pipeline snapshot + any new closings, and recomputes
the leaderboard.

---

## Run modes (CLI)

```bash
# one-time, trailing 30 days, build everything from scratch
python sheets_sync/fub_sheets_sync.py --mode backfill --days 30

# nightly: yesterday's activity + fresh snapshot + new closings + leaderboard
python sheets_sync/fub_sheets_sync.py --mode daily

# bypass the 10pm-Eastern gate when testing a scheduled-style run locally
python sheets_sync/fub_sheets_sync.py --mode daily --force
```

**Idempotency.** Every run is safe to repeat:

- Daily Activity rows are keyed on **(date, agent)** and replaced — never
  duplicated.
- Pipeline Snapshot rows are keyed on **(snapshot date, agent)**.
- Closings are appended only if not already present (keyed on the full row).
- Leaderboard and Config are fully recomputed each run.

---

## Reliability

At the end of every run a one-line summary is written to **both** the Actions
log and the **Config** tab: rows written, agents processed, agents with zero
activity, endpoints that errored, and whether the texts endpoint was usable.

- If the **FUB key** or **Google auth** fails, the script exits **non-zero** so
  the Actions run shows **red** and you get notified.
- Per-endpoint errors (e.g. a flaky list call) are logged and recorded in the
  Config tab without aborting the whole run.

---

## Field mappings — confirmed against your live account

These were verified directly against the account, and several differ from the
common assumptions in the brief. **Please confirm the ones marked ⚠️.**

| Metric / concept | Source & resolution |
| --- | --- |
| Agents (active only) | `/users`, filtered to `status == "Active"` |
| Stage → Active / Under Contract / Closed | Deal stages live **embedded in `/pipelines[].stages[]`**, not `/stages` (which only returns person-lifecycle stages). `closedStage == true` → **Closed**; name match `under contract/pending/escrow/accepted/mutual` → **Under Contract**; name match `fall through/expired/dead/lost/trash/cancel/withdrawn` → **Dead** (excluded from live pipeline); everything else → **Active**. Resolved mapping is written to the Config tab. |
| Listing vs Buyer **side** | ⚠️ Deals have **no `side`/`dealType` field**. Side is inferred from the **pipeline**: `Sale` (id 1) = **Buyer**, `Listing` (id 2) = **Listing**. A deal on a pipeline that matches neither is labeled **Unknown** and surfaced (not dropped). |
| Appointment "set" | `/appointments` created in the window, attributed to **`createdById`** (the organizing user). |
| Appointment "met" | ⚠️ Outcomes have **no met/showed boolean**. "Met" = the appointment has a non-null `outcome` whose name is **not** a no-show (`no show / cancel / reschedule`). On your account the non-no-show outcomes are *Working with buyers, Listing obtained, Interested, Joining, Not Interested, Nurture* — all treated as **met**. Confirm "Not Interested" should count as met (they showed but declined). |
| Conversations | `/calls` with `duration ≥ 120s`. |
| Dials / outbound | `/calls` count; `isIncoming == false` for outbound. |
| New leads assigned | `/people` created in window, by `assignedUserId`. |
| Notes | ⚠️ `/notes` **list payloads omit the author**, so notes are counted per agent via `/notes?userId=<id>`. |
| Deal agent attribution | ⚠️ Deals have **no `assignedUserId`**; agents come from **`deal.users[]`**. A deal with multiple agents is counted for each (rare on a small team). |
| Deal value | `deal.price`. |
| Projected closings this month | open deals with `projectedCloseDate` in the current Eastern month. |
| Closing "close date" | ⚠️ Deals have **no `closeDate` field**. The close date uses **`enteredStageAt`** (when the deal entered its Closed stage), falling back to `projectedCloseDate`. |

---

## Known gaps (called out, intentionally not solved in v1)

- **Texts sent.** ⚠️ `/textMessages` **cannot be swept account-wide** on this
  account — the API requires a person/thread/phone filter. The script attempts
  the sweep and, when rejected, leaves the **texts sent** column **blank** and
  records `texts_supported = False` in the Config tab + Actions log. If you want
  this metric, we'll need a per-person iteration strategy (phase 2) or a FUB
  plan/permission that allows the account-wide list.
- **Dials undercount.** Dials only count calls that actually log into FUB. Calls
  made from a cell phone that isn't connected to FUB will not be counted.
- **Speed-to-lead** and **unworked pond aging** are **phase 2**. `/ponds` is
  already discovered and written to the Config tab as a hook, but no aging
  metric is computed yet.

---

## Files

- `fub_sheets_sync.py` — the sync (FUB client, config discovery, metrics, Sheets writer).
- `requirements.txt` — Python deps.
- `../.github/workflows/fub-sheets-sync.yml` — the scheduled + manual workflow.
