// GET /api/watchlist -> {ok, list}      PUT /api/watchlist {key, list} -> {ok}
// Needs a KV binding named WATCH on the Pages project, and RUN_KEY.
const json = (o, s = 200) => new Response(JSON.stringify(o), { status: s, headers: { "content-type": "application/json", "cache-control": "no-store" } });
export async function onRequestGet({ env }) {
  if (!env.WATCH) return json({ ok: false, error: "no KV binding" }, 500);
  const raw = await env.WATCH.get("watchlist");
  return json({ ok: true, list: raw ? JSON.parse(raw) : [] });
}
export async function onRequestPut({ request, env }) {
  if (!env.WATCH) return json({ ok: false, error: "no KV binding" }, 500);
  let body = {}; try { body = await request.json(); } catch { return json({ ok: false, error: "bad json" }, 400); }
  if (!env.RUN_KEY || body.key !== env.RUN_KEY) return json({ ok: false, error: "wrong passphrase" }, 401);
  const list = Array.isArray(body.list) ? body.list.slice(0, 15).map(w => ({ sym: String(w.sym || "").slice(0, 20), name: String(w.name || "").slice(0, 60) })).filter(w => w.sym) : [];
  await env.WATCH.put("watchlist", JSON.stringify(list));
  return json({ ok: true, list });
}
