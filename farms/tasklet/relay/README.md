# Tasklet SignIn Relay

Minimal proxy functions — forward `/api/signIn` through different ASNs to bypass per-ASN rate limit (~20 signups/window).

## Deploy

### Deno Deploy (AS? — Deno/GCP)
```bash
# Via dashboard: https://dash.deno.com → New Project → paste deno.ts
# Or CLI:
deployctl deploy --project=tasklet-relay-1 deno.ts
```
URL: `https://tasklet-relay-1.deno.dev/`

### Vercel Edge (AS54113)
```bash
mkdir tasklet-relay-v && cd tasklet-relay-v
npm init -y
mkdir api && cp vercel.js api/relay.js
echo '{"rewrites":[{"source":"/(.*)", "destination":"/api/relay"}]}' > vercel.json
npx vercel --yes
```
URL: `https://tasklet-relay-v.vercel.app/`

### Val.town (alternative)
```ts
// Create a new Val at val.town, paste:
export default async function(req: Request) {
  if (req.method !== "POST") return new Response("POST only", { status: 405 });
  const body = await req.text();
  const r = await fetch("https://api.tasklet.ai/api/signIn", { method: "POST", headers: { "Content-Type": "application/json" }, body });
  return new Response(await r.text(), { status: r.status, headers: { "Content-Type": "application/json" } });
}
```
URL: `https://<username>-<valname>.web.val.run`

## Farm config

```env
# Comma-separated relay URLs in hub .env or farms/tasklet/.env
TASKLET_RELAY_URLS=https://tasklet-relay-1.deno.dev/,https://tasklet-relay-v.vercel.app/
```

Farm rotates to next relay after 429 or after N successful signups (default 18, conservative).
