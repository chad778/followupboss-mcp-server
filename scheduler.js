/**
 * Call Grader Scheduler
 *
 * Polls Follow Up Boss every GRADER_POLL_INTERVAL_SECONDS (default 300 = 5 min)
 * for new calls with duration > 2 minutes and no existing "Last Call Rating".
 * Grades each call autonomously via grader.js, writes the rating back to FUB,
 * and posts a coaching note @mentioning the agent.
 *
 * Sends an EOD summary at 8 pm ET; backs up at 9 pm ET if not yet sent.
 *
 * Environment variables:
 *   ANTHROPIC_API_KEY          — enables AI grading (falls back to rule-based if absent)
 *   GRADER_POLL_INTERVAL_SECONDS — poll frequency (default 300)
 *   GRADER_MIN_DURATION_SECONDS  — minimum call duration to grade (default 120)
 *   GRADER_MAX_CALLS_PER_RUN     — safety cap per poll cycle (default 50)
 *   GRADER_STATE_FILE            — path to JSON state file (default ./grader-state.json)
 *   GRADER_NOTIFY_USER_IDS       — comma-separated FUB user IDs to @mention on coaching notes
 *   GRADER_EOD_NOTIFY_USER_IDS   — comma-separated FUB user IDs for EOD note @mentions
 *   GRADER_EOD_PERSON_ID         — FUB person ID to post the EOD note on (required for FUB EOD note)
 *   GRADER_EOD_WEBHOOK_URL       — URL to POST EOD JSON payload (optional)
 *   GRADER_EOD_EMAIL             — email address for EOD summary (used by sendGraderEodSummary MCP tool)
 */

import { readFileSync, writeFileSync, existsSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import axios from 'axios';
import { gradeCall, buildEodNarrative } from './grader.js';

const __dirname = dirname(fileURLToPath(import.meta.url));

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const MIN_DURATION     = parseInt(process.env.GRADER_MIN_DURATION_SECONDS || '120', 10);
const POLL_INTERVAL_MS = parseInt(process.env.GRADER_POLL_INTERVAL_SECONDS || '300', 10) * 1000;
const MAX_PER_RUN      = parseInt(process.env.GRADER_MAX_CALLS_PER_RUN || '50', 10);
const STATE_FILE       = resolve(process.env.GRADER_STATE_FILE || `${__dirname}/grader-state.json`);
const EOD_HOUR         = 20; // 8 pm ET
const EOD_BACKUP_HOUR  = 21; // 9 pm ET
const ET_OFFSET        = 4;  // UTC-4 (EDT); DST-safe enough for this use case

// ---------------------------------------------------------------------------
// FUB API client (initialised by startScheduler)
// ---------------------------------------------------------------------------

let fubApi = null;

function initFub(apiKey) {
  fubApi = axios.create({
    baseURL: 'https://api.followupboss.com/v1',
    auth: { username: apiKey, password: '' },
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    timeout: 20000
  });
}

async function fubGet(path, params = {}) {
  const resp = await fubApi.get(path, { params });
  return resp.data;
}

async function fubPost(path, body) {
  const resp = await fubApi.post(path, body);
  return resp.data;
}

async function fubPut(path, body) {
  const resp = await fubApi.put(path, body);
  return resp.data;
}

// ---------------------------------------------------------------------------
// State persistence
// ---------------------------------------------------------------------------

const state = {
  lastRunAt:        null,    // ISO timestamp of last successful poll
  gradedCallIds:    new Set(), // call IDs we've already graded (in-session + loaded from file)
  lastSentEodFor:   null,    // "YYYY-MM-DD" — prevents double-sending EOD
  todayGradedCalls: []       // [{callId, agentName, leadName, rating, duration, coachingNote, date}]
};

function loadState() {
  try {
    if (!existsSync(STATE_FILE)) return;
    const raw = JSON.parse(readFileSync(STATE_FILE, 'utf8'));
    state.lastRunAt      = raw.lastRunAt      || null;
    state.lastSentEodFor = raw.lastSentEodFor || null;
    state.gradedCallIds  = new Set(Array.isArray(raw.gradedCallIds) ? raw.gradedCallIds : []);
    state.todayGradedCalls = Array.isArray(raw.todayGradedCalls) ? raw.todayGradedCalls : [];
    console.error(`[Grader] State loaded — lastRunAt=${state.lastRunAt}, ${state.gradedCallIds.size} graded call IDs on record`);
  } catch (err) {
    console.error('[Grader] State load error (starting fresh):', err.message);
  }
}

function saveState() {
  try {
    const ids = Array.from(state.gradedCallIds);
    const data = {
      lastRunAt:        state.lastRunAt,
      lastSentEodFor:   state.lastSentEodFor,
      gradedCallIds:    ids.slice(-2000),  // cap at 2000 to bound file size
      todayGradedCalls: state.todayGradedCalls.slice(-200)
    };
    writeFileSync(STATE_FILE, JSON.stringify(data, null, 2), 'utf8');
  } catch (err) {
    console.error('[Grader] State save error:', err.message);
  }
}

// ---------------------------------------------------------------------------
// ET time helpers
// ---------------------------------------------------------------------------

function etNow() {
  return new Date(Date.now() - ET_OFFSET * 3600 * 1000);
}

function todayET() {
  const et = etNow();
  const y  = et.getUTCFullYear();
  const m  = String(et.getUTCMonth() + 1).padStart(2, '0');
  const d  = String(et.getUTCDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

function currentHourET() {
  return etNow().getUTCHours();
}

// ---------------------------------------------------------------------------
// Transcript detection
// ---------------------------------------------------------------------------

/**
 * Search person notes for a Speculo AI (or similar) transcript near the call time.
 */
async function findTranscript(personId, callCreatedIso) {
  try {
    const callMs       = new Date(callCreatedIso).getTime();
    const windowStart  = callMs - 5 * 60 * 1000;   // 5 min before
    const windowEnd    = callMs + 30 * 60 * 1000;   // 30 min after

    const data  = await fubGet('/notes', { personId, limit: 30 });
    const notes = data.notes || [];

    for (const note of notes) {
      const noteMs = new Date(note.created).getTime();
      if (noteMs < windowStart || noteMs > windowEnd) continue;
      const body    = note.body    || '';
      const subject = note.subject || '';
      if (
        body.length > 200 ||
        /transcript|speculo|recording|call log/i.test(subject) ||
        /transcript|speculo|recording/i.test(body)
      ) {
        return body || subject;
      }
    }
  } catch (err) {
    console.error(`[Grader] Transcript fetch error for person ${personId}:`, err.message);
  }
  return null;
}

// ---------------------------------------------------------------------------
// Parse helper
// ---------------------------------------------------------------------------

function parseIds(str) {
  if (!str) return [];
  return str.split(',').map(s => parseInt(s.trim(), 10)).filter(n => Number.isFinite(n));
}

function starLine(rating) {
  return '★'.repeat(rating) + '☆'.repeat(5 - rating);
}

function fmtDur(sec) {
  if (!sec) return '?m';
  return `${Math.floor(sec / 60)}m ${sec % 60}s`;
}

// ---------------------------------------------------------------------------
// Core: poll and grade
// ---------------------------------------------------------------------------

export async function pollAndGradeCalls() {
  if (!fubApi) {
    return { error: 'Scheduler not started — FUB API not initialised' };
  }

  const today = todayET();

  // Reset today's graded list when the date rolls over
  if (state.todayGradedCalls.length > 0 && state.todayGradedCalls[0]?.date !== today) {
    state.todayGradedCalls = [];
  }

  // Determine the time window to search
  const since = state.lastRunAt
    ? new Date(state.lastRunAt)
    : new Date(Date.now() - 24 * 3600 * 1000); // first run → last 24 h

  console.error(`[Grader] Polling calls since ${since.toISOString()}`);

  // Paginate /calls newest-first; stop when we reach records older than `since`
  const candidates = [];
  const seen       = new Set();
  let cursor       = null;
  let page         = 0;
  const maxPages   = 60;

  outer: while (page < maxPages) {
    const params = { limit: 100 };
    if (cursor) params.next = cursor;

    let data;
    try {
      data = await fubGet('/calls', params);
    } catch (err) {
      console.error('[Grader] Failed to fetch calls page:', err.message);
      break;
    }

    page++;
    const calls = data.calls || [];
    if (!calls.length) break;

    for (const c of calls) {
      if (seen.has(c.id)) continue;
      seen.add(c.id);

      const callTime = new Date(c.created || c.startedAt);
      if (callTime < since) break outer;  // oldest in window reached

      if ((c.duration || 0) < MIN_DURATION) continue;  // too short
      if (state.gradedCallIds.has(c.id))    continue;  // already graded

      candidates.push(c);
      if (candidates.length >= MAX_PER_RUN) break outer; // safety cap
    }

    cursor = data._metadata?.next;
    if (!cursor) break;
  }

  console.error(`[Grader] ${candidates.length} qualifying calls found`);

  if (candidates.length === 0) {
    state.lastRunAt = new Date().toISOString();
    saveState();
    return { graded: 0, skipped: 0, total: 0 };
  }

  const notifyIds = parseIds(process.env.GRADER_NOTIFY_USER_IDS);
  let graded = 0;
  let skipped = 0;

  for (const call of candidates) {
    try {
      // Person details (best-effort)
      let person = null;
      if (call.personId) {
        try {
          person = await fubGet(`/people/${call.personId}`);
        } catch (_) { /* non-fatal */ }
      }

      // Transcript (best-effort)
      const transcript = call.personId
        ? await findTranscript(call.personId, call.created || call.startedAt)
        : null;

      // AI or rule-based grade
      const { rating, coachingNote } = await gradeCall({ call, person, transcriptNote: transcript });

      console.error(`[Grader] Call ${call.id} → ${rating}★ — ${coachingNote.substring(0, 60)}…`);

      // Write rating to person custom field
      if (call.personId) {
        try {
          await fubPut(`/people/${call.personId}`, { customLastCallRating: rating });
        } catch (err) {
          console.error(`[Grader] Rating write failed (person ${call.personId}):`, err.message);
        }
      }

      // Post coaching note with @mentions
      if (call.personId) {
        const agentId      = call.userId ? [call.userId] : [];
        const mentionedUsers = [...new Set([...notifyIds, ...agentId])];
        const stars        = starLine(rating);
        const noteBody     = [
          `${stars} Call Rated ${rating}/5`,
          `Duration: ${fmtDur(call.duration)} | ${call.isIncoming ? 'Inbound' : 'Outbound'} | Outcome: ${call.outcome || 'Unknown'}`,
          '',
          `Coaching: ${coachingNote}`,
          '',
          '— Automated Call Grader'
        ].join('\n');

        try {
          await fubPost('/notes', {
            personId: call.personId,
            subject: `Call Coaching — ${rating}★`,
            body: noteBody,
            ...(mentionedUsers.length > 0 ? { mentionedUsers } : {})
          });
        } catch (err) {
          console.error(`[Grader] Note post failed (call ${call.id}):`, err.message);
        }
      }

      // Persist
      state.gradedCallIds.add(call.id);
      state.todayGradedCalls.push({
        callId:      call.id,
        agentName:   call.userName || 'Unknown',
        leadName:    person?.name || call.name || 'Unknown',
        rating,
        duration:    call.duration || 0,
        coachingNote,
        date:        today,
        gradedAt:    new Date().toISOString()
      });

      graded++;
      await pause(1200); // ~50 calls/min max, well within FUB rate limits

    } catch (err) {
      console.error(`[Grader] Unexpected error on call ${call.id}:`, err.message);
      skipped++;
    }
  }

  state.lastRunAt = new Date().toISOString();
  saveState();

  console.error(`[Grader] Poll complete — graded ${graded}, skipped ${skipped}`);
  return { graded, skipped, total: candidates.length };
}

// ---------------------------------------------------------------------------
// EOD summary
// ---------------------------------------------------------------------------

export async function sendEodSummary(opts = {}) {
  const today = todayET();
  const force = opts.force === true;

  if (!force && state.lastSentEodFor === today) {
    return { sent: false, reason: 'already_sent_today', date: today };
  }

  const todaysCalls = state.todayGradedCalls.filter(c => c.date === today);
  const callCount   = todaysCalls.length;

  // Build the summary text
  const summaryText = buildSummaryText(todaysCalls, today);

  // Optional AI narrative (Sonnet)
  const narrative = await buildEodNarrative(todaysCalls, today);

  const fullSummary = narrative
    ? `${summaryText}\n\n📝 COACHING NARRATIVE:\n${narrative}`
    : summaryText;

  let deliveries = [];

  // 1. Webhook
  const webhookUrl = process.env.GRADER_EOD_WEBHOOK_URL;
  if (webhookUrl) {
    try {
      await axios.post(webhookUrl, {
        type:        'eod_call_grading_summary',
        date:        today,
        callsGraded: callCount,
        summary:     fullSummary,
        calls:       todaysCalls.map(c => ({
          agentName:   c.agentName,
          leadName:    c.leadName,
          rating:      c.rating,
          duration:    c.duration,
          coachingNote: c.coachingNote
        }))
      }, { timeout: 10000 });
      deliveries.push('webhook');
    } catch (err) {
      console.error('[Grader] Webhook delivery error:', err.message);
    }
  }

  // 2. FUB note with @mentions (requires GRADER_EOD_PERSON_ID)
  const eodPersonId = process.env.GRADER_EOD_PERSON_ID
    ? parseInt(process.env.GRADER_EOD_PERSON_ID, 10)
    : null;

  const eodUserIds = parseIds(
    process.env.GRADER_EOD_NOTIFY_USER_IDS || process.env.GRADER_NOTIFY_USER_IDS
  );

  if (fubApi && eodPersonId) {
    try {
      await fubPost('/notes', {
        personId: eodPersonId,
        subject: `📊 Daily Call Grading Summary — ${today}`,
        body: fullSummary,
        ...(eodUserIds.length > 0 ? { mentionedUsers: eodUserIds } : {})
      });
      deliveries.push('fub_note');
    } catch (err) {
      console.error('[Grader] EOD FUB note error:', err.message);
    }
  }

  state.lastSentEodFor = today;
  saveState();

  console.error(`[Grader] EOD summary sent for ${today} (${callCount} calls) via: ${deliveries.join(', ') || 'console only'}`);
  return {
    sent:       true,
    date:       today,
    callCount,
    deliveries,
    summary:    fullSummary
  };
}

function buildSummaryText(calls, date) {
  if (calls.length === 0) {
    return `📊 DAILY CALL GRADING SUMMARY — ${date}\n\nNo calls graded today (none met the 2-minute threshold or all were already rated).\n\n— Automated Call Grader`;
  }

  const avgRating = (calls.reduce((s, c) => s + c.rating, 0) / calls.length).toFixed(1);

  // Per-agent stats
  const byAgent = {};
  for (const c of calls) {
    if (!byAgent[c.agentName]) byAgent[c.agentName] = { count: 0, ratingSum: 0 };
    byAgent[c.agentName].count++;
    byAgent[c.agentName].ratingSum += c.rating;
  }
  const agentLines = Object.entries(byAgent)
    .sort((a, b) => (b[1].ratingSum / b[1].count) - (a[1].ratingSum / a[1].count))
    .map(([name, s]) => `  • ${name}: ${s.count} call(s), avg ${(s.ratingSum / s.count).toFixed(1)}★`)
    .join('\n');

  // Individual calls (highest rated first)
  const callLines = calls
    .slice()
    .sort((a, b) => b.rating - a.rating)
    .map(c => {
      const stars = starLine(c.rating);
      const clip  = c.coachingNote.length > 90 ? c.coachingNote.substring(0, 90) + '…' : c.coachingNote;
      return `  ${stars} ${c.agentName} → ${c.leadName} (${fmtDur(c.duration)}): ${clip}`;
    })
    .join('\n');

  return [
    `📊 DAILY CALL GRADING SUMMARY — ${date}`,
    `Calls Graded: ${calls.length} | Team Avg: ${avgRating}★`,
    '',
    'BY AGENT:',
    agentLines,
    '',
    'INDIVIDUAL CALLS (highest rated first):',
    callLines,
    '',
    '— Automated Call Grader'
  ].join('\n');
}

// ---------------------------------------------------------------------------
// Public state accessor (for MCP tool)
// ---------------------------------------------------------------------------

export function getGraderStatus() {
  const today = todayET();
  const todaysCalls = state.todayGradedCalls.filter(c => c.date === today);
  return {
    lastRunAt:        state.lastRunAt,
    lastSentEodFor:   state.lastSentEodFor,
    totalGradedAllTime: state.gradedCallIds.size,
    todayDate:        today,
    todayGradedCount: todaysCalls.length,
    todayGradedCalls: todaysCalls,
    pollIntervalSeconds: POLL_INTERVAL_MS / 1000,
    minDurationSeconds:  MIN_DURATION,
    aiGraderEnabled:   !!process.env.ANTHROPIC_API_KEY
  };
}

// ---------------------------------------------------------------------------
// Scheduler bootstrap
// ---------------------------------------------------------------------------

export function startScheduler(apiKey) {
  loadState();
  initFub(apiKey);

  if (!process.env.ANTHROPIC_API_KEY) {
    console.error('[Grader] ANTHROPIC_API_KEY not set — rule-based grader active');
  }

  // Initial poll 15 s after server boot (let the server finish starting first)
  setTimeout(() => {
    pollAndGradeCalls().catch(err => console.error('[Grader] Initial poll error:', err.message));
  }, 15000);

  // Recurring poll
  setInterval(() => {
    pollAndGradeCalls().catch(err => console.error('[Grader] Poll error:', err.message));
  }, POLL_INTERVAL_MS);

  // EOD check: evaluate every minute
  setInterval(() => {
    const h     = currentHourET();
    const today = todayET();

    if (h === EOD_HOUR && state.lastSentEodFor !== today) {
      console.error('[Grader] 8 pm ET — sending EOD summary');
      sendEodSummary().catch(err => console.error('[Grader] EOD error:', err.message));
    }

    if (h === EOD_BACKUP_HOUR && state.lastSentEodFor !== today) {
      console.error('[Grader] 9 pm ET backup — EOD not yet sent, sending now');
      sendEodSummary().catch(err => console.error('[Grader] EOD backup error:', err.message));
    }
  }, 60 * 1000);

  console.error(`[Grader] Scheduler started — poll every ${POLL_INTERVAL_MS / 1000}s, EOD at 8 pm ET (backup 9 pm ET)`);
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

function pause(ms) {
  return new Promise(r => setTimeout(r, ms));
}
