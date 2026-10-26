// GET /api/status -> latest digest workflow run {status, conclusion, createdAt, updatedAt, url}
export async function onRequestGet({ env }) {
  const json = (o, s = 200) => new Response(JSON.stringify(o), { status: s, headers: { "content-type": "application/json", "cache-control": "no-store" } });
  const repo = env.GH_REPO || "kmvipin-source/market-digest";
  const headers = { accept: "application/vnd.github+json", "user-agent": "market-digest-pages" };
  if (env.GITHUB_TOKEN) headers.authorization = `Bearer ${env.GITHUB_TOKEN}`;
  const r = await fetch(`https://api.github.com/repos/${repo}/actions/workflows/digest.yml/runs?per_page=1`, { headers });
  if (!r.ok) return json({ ok: false, error: `github ${r.status}` }, 502);
  const d = await r.json();
  const run = (d.workflow_runs || [])[0];
  if (!run) return json({ ok: true, status: "none" });
  return json({ ok: true, status: run.status, conclusion: run.conclusion, createdAt: run.created_at, updatedAt: run.updated_at, url: run.html_url, configured: !!env.RUN_KEY });
}
