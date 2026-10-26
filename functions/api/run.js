// POST /api/run  {mode, query, threshold, key}
// Starts the GitHub Actions digest workflow. Needs env: GITHUB_TOKEN (fine-grained, Actions: write on the repo), RUN_KEY (passphrase), GH_REPO (owner/repo).
export async function onRequestPost({ request, env }) {
  const json = (o, s = 200) => new Response(JSON.stringify(o), { status: s, headers: { "content-type": "application/json", "cache-control": "no-store" } });
  let body = {};
  try { body = await request.json(); } catch { return json({ ok: false, error: "bad json" }, 400); }
  if (!env.GITHUB_TOKEN || !env.RUN_KEY) return json({ ok: false, error: "not configured: set GITHUB_TOKEN and RUN_KEY in Cloudflare Pages settings" }, 500);
  if ((body.key || "") !== env.RUN_KEY) return json({ ok: false, error: "wrong passphrase" }, 401);
  const repo = env.GH_REPO || "kmvipin-source/market-digest";
  const modes = ["full", "movers", "orders", "results", "policy", "custom"];
  const mode = modes.includes(body.mode) ? body.mode : "full";
  const inputs = { mode, query: String(body.query || "").slice(0, 300), threshold: String(parseFloat(body.threshold) || 10) };
  const r = await fetch(`https://api.github.com/repos/${repo}/actions/workflows/digest.yml/dispatches`, {
    method: "POST",
    headers: { authorization: `Bearer ${env.GITHUB_TOKEN}`, accept: "application/vnd.github+json", "user-agent": "market-digest-pages", "content-type": "application/json" },
    body: JSON.stringify({ ref: "main", inputs }),
  });
  if (r.status === 204) return json({ ok: true, startedAt: new Date().toISOString() });
  const text = await r.text();
  return json({ ok: false, error: `github ${r.status}: ${text.slice(0, 200)}` }, 502);
}
