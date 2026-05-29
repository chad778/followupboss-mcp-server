/**
 * Lead Call Grader — autonomous 1-5 star rating engine.
 *
 * Uses claude-haiku-4-5 for per-call grading (cost-efficient, ~many calls/day)
 * and claude-sonnet-4-6 for EOD summary synthesis.
 * Falls back to rule-based scoring when ANTHROPIC_API_KEY is not set.
 */

import Anthropic from '@anthropic-ai/sdk';

let anthropic = null;
if (process.env.ANTHROPIC_API_KEY) {
  anthropic = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });
}

// Cached system prompt — reused across all per-call grading requests.
const GRADING_SYSTEM = `You are an expert real estate sales coach reviewing agent phone calls. \
Grade each call on a 1-5 star scale and provide a short, direct coaching note.

Rating rubric:
1★ — Unacceptable: agent was unprepared, dismissive, or the call went nowhere
2★ — Below average: connected but missed major opportunities; voicemail/no-answer with no strategy evident
3★ — Average: decent conversation but missing needs discovery, rapport, or a clear next step
4★ — Good: built rapport, uncovered key needs, secured a next step
5★ — Excellent: outstanding rapport, strong discovery, appointment or firm commitment locked in

Coaching note rules:
- Max 2 sentences
- Written directly to the agent ("Great job…", "Next time try…")
- Mention ONE thing done well and ONE specific improvement
- Never generic — reference call specifics when available

Respond ONLY in this exact two-line format (no preamble, no extra text):
RATING: [1-5]
COACHING: [your note here]`;

/**
 * Grade a single call.
 * @param {{ call: object, person: object|null, transcriptNote: string|null }} ctx
 * @returns {Promise<{ rating: number, coachingNote: string }>}
 */
export async function gradeCall({ call, person, transcriptNote }) {
  if (anthropic) {
    return aiGrade(call, person, transcriptNote);
  }
  return ruleBasedGrade(call);
}

/**
 * Build a concise EOD narrative summary using Sonnet.
 * @param {Array} gradedCalls
 * @param {string} dateLabel  "YYYY-MM-DD"
 * @returns {Promise<string>}
 */
export async function buildEodNarrative(gradedCalls, dateLabel) {
  if (!anthropic || gradedCalls.length === 0) return null;

  const callLines = gradedCalls.map(c => {
    const dur = `${Math.floor(c.duration / 60)}m ${c.duration % 60}s`;
    return `- ${c.agentName} → ${c.leadName} | ${c.rating}★ | ${dur} | ${c.coachingNote}`;
  }).join('\n');

  try {
    const msg = await anthropic.messages.create({
      model: 'claude-sonnet-4-6',
      max_tokens: 600,
      system: [
        {
          type: 'text',
          text: 'You write concise daily coaching summaries for real estate team managers. Be direct, data-driven, and encouraging. Max 150 words.',
          cache_control: { type: 'ephemeral' }
        }
      ],
      messages: [{
        role: 'user',
        content: `Write a 3-4 sentence team coaching narrative for ${dateLabel} based on these graded calls:\n\n${callLines}\n\nHighlight the team's overall trend, the top performer, and the single biggest improvement area.`
      }]
    });
    return msg.content[0].text.trim();
  } catch (err) {
    console.error('[Grader] EOD narrative generation failed:', err.message);
    return null;
  }
}

// ---------------------------------------------------------------------------
// AI grading (Haiku — cheap per call)
// ---------------------------------------------------------------------------

async function aiGrade(call, person, transcript) {
  const userContent = buildCallPrompt(call, person, transcript);
  try {
    const msg = await anthropic.messages.create({
      model: 'claude-haiku-4-5-20251001',
      max_tokens: 256,
      system: [
        { type: 'text', text: GRADING_SYSTEM, cache_control: { type: 'ephemeral' } }
      ],
      messages: [{ role: 'user', content: userContent }]
    });
    const parsed = parseOutput(msg.content[0].text, call);
    if (parsed) return parsed;
    // Parse failed — fall through to rule-based
  } catch (err) {
    console.error('[Grader] AI call failed, using rule-based fallback:', err.message);
  }
  return ruleBasedGrade(call);
}

function buildCallPrompt(call, person, transcript) {
  const dur = fmtDuration(call.duration);
  const lines = [
    'CALL DETAILS:',
    `  Agent: ${call.userName || 'Unknown'}`,
    `  Duration: ${dur}`,
    `  Direction: ${call.isIncoming ? 'Inbound' : 'Outbound'}`,
    `  Outcome: ${call.outcome || 'Unknown'}`,
    `  Date/Time: ${call.startedAt || call.created || 'Unknown'}`,
    '',
    'LEAD DETAILS:',
    `  Name: ${person?.name || call.name || 'Unknown'}`,
    `  Stage: ${person?.stage || 'Unknown'}`,
    `  Source: ${person?.source || 'Unknown'}`,
    `  Previously Contacted: ${person?.contacted ? 'Yes' : 'No'}`,
  ];

  if (call.note) {
    lines.push('', `AGENT CALL NOTES: ${call.note}`);
  }

  if (transcript) {
    const clip = transcript.length > 3000
      ? transcript.substring(0, 3000) + '\n...[transcript truncated]'
      : transcript;
    lines.push('', 'CALL TRANSCRIPT:', clip);
  }

  lines.push('', 'Grade this call.');
  return lines.join('\n');
}

function parseOutput(text, call) {
  const ratingMatch = text.match(/RATING:\s*([1-5])/i);
  const coachingMatch = text.match(/COACHING:\s*([\s\S]+)/i);
  if (!ratingMatch || !coachingMatch) return null;
  const rating = parseInt(ratingMatch[1], 10);
  const coachingNote = coachingMatch[1].trim().split('\n')[0].trim(); // first line only
  if (rating < 1 || rating > 5 || !coachingNote) return null;
  return { rating, coachingNote };
}

// ---------------------------------------------------------------------------
// Rule-based fallback (no API key required)
// ---------------------------------------------------------------------------

function ruleBasedGrade(call) {
  const duration = call.duration || 0;
  const outcome = (call.outcome || '').toLowerCase();
  const dur = fmtDuration(duration);

  if (outcome.includes('voicemail') || outcome.includes('no answer') || outcome.includes('not answered')) {
    return {
      rating: 2,
      coachingNote: `Good effort placing the call (${dur}). Try varying your call time slots — early morning and early evening typically yield higher connect rates. Leave a brief, value-focused voicemail with a specific reason to call back.`
    };
  }

  if (duration < 180) {
    return {
      rating: 2,
      coachingNote: `Short connection (${dur}) — work on extending rapport early in the call. Start with a genuine compliment or question about their goals to keep leads engaged before pivoting to business.`
    };
  }

  if (duration < 300) {
    return {
      rating: 3,
      coachingNote: `Solid start at ${dur}! Focus on deeper discovery next time — ask one more open-ended question about their timeline or motivation, and always secure a specific next step before hanging up.`
    };
  }

  if (duration < 600) {
    return {
      rating: 4,
      coachingNote: `Great conversation (${dur})! You're building real rapport. Make sure every call ends with a committed date and time for the next interaction so momentum doesn't slip.`
    };
  }

  return {
    rating: 5,
    coachingNote: `Excellent engagement (${dur}) — long calls like this signal strong connection. Confirm that you locked in a concrete appointment or follow-up date, and jot down their key motivators while they're fresh.`
  };
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function fmtDuration(seconds) {
  if (!seconds) return 'unknown duration';
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return s > 0 ? `${m}m ${s}s` : `${m}m`;
}
