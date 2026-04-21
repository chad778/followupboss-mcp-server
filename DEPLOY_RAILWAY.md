# Deploy to Railway + connect to Claude

End result: you can use every FUB tool from the Claude mobile app, your browser, or any other device signed into your Claude account. No Mac required.

Takes about 15 minutes. Follow in order.

---

## 1. Push this repo to GitHub (2 min)

Railway deploys from GitHub. If this folder is not already on a GitHub repo under your account, do that first.

```bash
cd ~/followupboss-mcp-server
git remote -v        # should show a remote you control
git add .
git commit -m "Add HTTP transport for remote deployment"
git push
```

If it still points at `mindwear-capitian/followupboss-mcp-server`, create your own fork first:
1. Go to https://github.com/mindwear-capitian/followupboss-mcp-server
2. Click Fork
3. Locally: `git remote set-url origin https://github.com/<your-username>/followupboss-mcp-server.git`
4. `git push -u origin main`

---

## 2. Generate a bearer secret (30 seconds)

Terminal:

```bash
openssl rand -hex 32
```

Copy the 64-char hex string. This is your `MCP_BEARER_TOKEN`. Treat it like a password.

---

## 3. Create Railway account + deploy (5 min)

1. Go to https://railway.com
2. Sign up with your GitHub account
3. Click **New Project** > **Deploy from GitHub repo**
4. Pick your `followupboss-mcp-server` fork
5. Railway auto-detects the Dockerfile and starts building

While it builds, click the service and open the **Variables** tab. Add:

| Variable           | Value                                              |
| ------------------ | -------------------------------------------------- |
| `FUB_API_KEY`      | your Follow Up Boss API key                        |
| `FUB_SAFE_MODE`    | `true`  (recommended — blocks all delete tools)    |
| `MCP_TRANSPORT`    | `http`                                             |
| `MCP_BEARER_TOKEN` | the 64-char hex string you just generated          |

Click **Deploy** (or let the auto-redeploy finish).

---

## 4. Get your public URL (1 min)

In the service's **Settings** tab > **Networking** > click **Generate Domain**.

Railway gives you something like `https://followupboss-mcp-server-production.up.railway.app`.

Test it from your laptop:

```bash
curl https://<your-railway-url>/health
```

Expected:

```json
{"status":"ok","version":"1.1.1","tools":134,"safeMode":true,"ts":"..."}
```

If that works, the server is live.

Also test auth (replace `<TOKEN>` with your bearer):

```bash
curl -X POST https://<your-railway-url>/mcp \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}'
```

Should return a JSON-RPC response with `"protocolVersion":"2025-06-18"`.

---

## 5. Add as a custom connector in Claude (2 min)

In Claude (web or desktop):

1. Open **Settings** > **Connectors** (or **Integrations**)
2. Click **Add Custom Connector** (or **Browse** > **Add your own**)
3. Fill in:
   - **Name**: `Follow Up Boss`
   - **Server URL**: `https://<your-railway-url>/mcp`
   - **Auth Type**: Bearer token (or "Custom header")
   - **Token**: your bearer secret
4. Save

Claude will ping the server, list the 134 tools, and confirm it's connected.

Now open Claude on your phone. The FUB tools are there.

---

## 6. (Optional) Shut down local Cowork MCP

Your laptop still has the local stdio MCP running. It's harmless to leave it, but you can remove it from `~/Library/Application Support/Claude/claude_desktop_config.json` if you want a clean setup:

```json
{
  "mcpServers": {}
}
```

Quit and reopen Cowork. Now Claude uses the Railway-hosted connector everywhere — desktop and mobile.

---

## Troubleshooting

**Build fails on Railway.** Check the build log. Usually means `npm ci` couldn't resolve a dep. Try deleting `package-lock.json` locally, running `npm install`, committing, and pushing.

**401 from /mcp with correct token.** Verify the header is exactly `Authorization: Bearer <token>` with a single space, and that you didn't paste an extra newline into the Railway variable.

**Claude says "connector error".** Most likely the URL is wrong (should end in `/mcp`, not `/health`), or your Railway deployment is in a restart loop. Check Railway logs.

**"Deep pagination disabled" errors.** That was the original FUB API bug we already fixed in `index.js` — make sure your deployed version is current. Check the commit hash on Railway matches your latest push.

**Rotating the secret.** Generate a new hex string, update `MCP_BEARER_TOKEN` in Railway, wait for redeploy, update the Claude connector token. Old bearer becomes invalid instantly.

---

## Cost

Railway free tier covers idle time. Expected usage for this server is basically nothing (few HTTP requests a day from Claude).

If it does get hit constantly, you're still under $5/mo on their hobby plan.

---

## Security notes

- `FUB_SAFE_MODE=true` disables all `delete*` tools. Keep this on unless you explicitly need destructive operations.
- The bearer token is the only thing protecting full write access to your CRM. If it leaks, rotate it immediately (step in Troubleshooting above).
- Railway auto-provides HTTPS. Don't set up a custom domain with HTTP.
- Logs may contain FUB data. Railway logs are private to your account; don't paste them publicly.
