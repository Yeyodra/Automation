// Tasklet signIn relay — deploy to Vercel as Edge Function
//
// Setup:
//   mkdir tasklet-relay && cd tasklet-relay
//   npm init -y
//   mkdir api && cp this file api/relay.js
//   echo '{"rewrites":[{"source":"/(.*)", "destination":"/api/relay"}]}' > vercel.json
//   npx vercel --yes
//
// Usage: POST https://<project>.vercel.app/
//   Body: same JSON as api.tasklet.ai/api/signIn
//   Returns: proxied response

export const config = { runtime: "edge" };

export default async function handler(req) {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "POST", "Access-Control-Allow-Headers": "Content-Type" } });
  }
  if (req.method !== "POST") {
    return new Response("POST only", { status: 405 });
  }

  const body = await req.text();
  const resp = await fetch("https://api.tasklet.ai/api/signIn", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
  });

  return new Response(await resp.text(), {
    status: resp.status,
    headers: { "Content-Type": "application/json", "X-Relay": "vercel" },
  });
}
