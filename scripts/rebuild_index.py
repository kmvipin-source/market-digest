#!/usr/bin/env python3
"""Rebuild data/index.json from the run files on disk (safe to run any time)."""
import json, glob, os
from pathlib import Path
DATA = Path(__file__).resolve().parent.parent / "data"
runs = []
for f in sorted(DATA.glob("*.json")):
    if f.name in ("index.json", "scrips.json", "sample_raw.json"):
        continue
    try:
        d = json.loads(f.read_text())
    except Exception:
        continue
    if not isinstance(d, dict) or "items" not in d:
        continue
    counts = {}
    for it in d.get("items", []):
        counts[it.get("category", "other")] = counts.get(it.get("category", "other"), 0) + 1
    runs.append({"id": d.get("id") or f.stem, "label": d.get("label", f.stem), "date": d.get("date", ""), "generatedAt": d.get("generatedAt", ""),
                 "mode": d.get("mode", "full"), "count": len(d.get("items", [])), "counts": counts})
runs.sort(key=lambda r: r["generatedAt"], reverse=True)
idx = {"runs": runs[:120], "updatedAt": runs[0]["generatedAt"] if runs else ""}
(DATA / "index.json").write_text(json.dumps(idx, ensure_ascii=False, indent=1))
print(f"index: {len(runs)} runs")
