# FollowUpBoss MCP Server — Claude Instructions

## Daily FUB Activity Audit

Run this audit each session when asked, or when automating the daily 8 PM task.

### Data Source
- **Google Sheet ID**: `1nBR0ngtAeEaHONr3Y8qIh32ybf1dgU0t3fzXa80lslc`
- Use `mcp__Google-Drive__download_file_content` with `exportMimeType: "text/csv"` to get the full CSV (base64-encoded). Do NOT use `read_file_content` — it truncates for large files.
- Decode the base64 CSV and filter rows where the date equals **yesterday's date**.

### CSV Column Order
`date, agent, dials total, outbound dials, conversations, texts sent, appts set, appts met, new leads assigned, notes`

### Excluded Staff (never include in audit)
- Chad Leonberg
- Brittany Leonberg
- Dennis Palapar
- Danielle Heitner

### Daily Goals (weekly targets ÷ 5)
- **Conversations**: 4 per day
- **Appointments Set**: 1 per day

### Status Classification
| Status | Color | Hex | Criteria |
|--------|-------|-----|----------|
| Green | 🟢 GREEN | `#28a745` | 4+ Conversations AND 1+ Appointments Set |
| Yellow | 🟡 YELLOW | `#ffc107` | 2–3 Conversations OR ≥10 Outbound Dials effort shown |
| Off | 🔴 OFF | `#dc3545` | 0–1 Conversations and <10 Outbound Dials |

### Delivery — Google Chat Webhook ONLY
**No Gmail drafts. No email.** Send only to the Google Chat webhook:

```
https://chat.googleapis.com/v1/spaces/AAQAutY1kmQ/messages?key=AIzaSyDdI0hCZtE6vySjMm-WEfRq3CPzqKqqsHI&token=ZOuzGK-1uOv2D-5Y50ZmvrHZh0wDiHvMRblHlYd-Ufw
```

Use `curl` with a POST and `Content-Type: application/json`. WebFetch cannot POST.

### Google Chat Card Format
```json
{
  "cards": [{
    "header": {
      "title": "Daily FUB Activity Audit: [Month DD, YYYY]",
      "subtitle": "Daily Goals: 4 Conversations | 1 Appointment Set (Weekly ÷ 5)"
    },
    "sections": [
      { "header": "🟢 GREEN — Met Daily Goal", "widgets": [...] },
      { "header": "🟡 YELLOW — Close to Daily Goal", "widgets": [...] },
      { "header": "🔴 OFF — Below Daily Goal", "widgets": [...] }
    ]
  }]
}
```

Each agent widget uses `textParagraph` with `<font color="...">` HTML. Format each agent line as:
`<b><font color="#HEX">AgentName</font></b> — Dials: X | Out: X | Convs: X | Appts: X`

The card title must be **exactly**: `Daily FUB Activity Audit: [Date]` — no extra text.
