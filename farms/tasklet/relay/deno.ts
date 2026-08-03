// Tasklet signIn relay — deploy to Deno Deploy
// Each deployment = different ASN = 20 fresh signup quota
//
// Deploy: https://dash.deno.com → New Project → paste this → deploy
// Or CLI: deployctl deploy --project=tasklet-relay-1 deno.ts
//
// Usage: POST https://<your-project>.deno.dev/
//   Body: same JSON as api.tasklet.ai/api/signIn
//   Returns: proxied response (status + body)

const TARGET = "https://api.tasklet.ai/api/signIn";

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "POST", "Access-Control-Allow-Headers": "Content-Type" } });
  }
  if (req.method !== "POST") {
    return new Response("POST only", { status: 405 });
  }

  const body = await req.text();
  const resp = await fetch(TARGET, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
  });

  return new Response(await resp.text(), {
    status: resp.status,
    headers: { "Content-Type": "application/json", "X-Relay": "deno" },
  });
});
