"""Weekly / monthly P&L of the US-stock AI arena (owner's portfolio), for the LINE reports.

Each arena is a Node project cloned next to this repo; it is refreshed (git pull + fresh Yahoo prices) and asked
for its period P&L via `node scripts/report.mjs`. Local data changes are reverted so later pulls stay clean.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SCRATCH = Path(__file__).resolve().parents[2]
ARENAS = [
    ("ai-quant-trading-arena", "🇺🇸 美股（你的持倉，USD）"),
]  # Taiwan stocks: no real holdings, so not included in the LINE reports.
NODE = str(Path.home() / ".local" / "bin" / "node")


def _run(args: list[str], cwd: Path, timeout: int) -> str:
  env = dict(os.environ, PATH=f"{Path.home() / '.local' / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}")
  return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=True, env=env).stdout


def arena_report(repo: str, kind: str, arg: str) -> dict | None:
  """kind: 'week' (arg = any date in the week, YYYY-MM-DD) or 'month' (arg = YYYY-MM)."""
  d = SCRATCH / repo
  if not (d / "scripts" / "report.mjs").exists():
    return None
  try:
    _run(["git", "pull", "-q", "--ff-only"], d, 60)
  except Exception:
    pass  # Offline: use the local copy.
  for script in ("scripts/sync-market-data.mjs", "scripts/sync-broad.mjs"):
    try:
      _run([NODE, script], d, 300)
    except Exception:
      pass  # Keep the last synced prices.
  try:
    return json.loads(_run([NODE, "scripts/report.mjs", kind, arg], d, 120).strip().splitlines()[-1])
  finally:
    subprocess.run(["git", "checkout", "--", "data"], cwd=d, capture_output=True)


def _short(name: str) -> str:
  return "📌 你（不動）" if "不動" in name else name.split(" ")[0]


def stock_lines(kind: str, arg: str) -> list[str]:
  out: list[str] = []
  for repo, title in ARENAS:
    try:
      rep = arena_report(repo, kind, arg)
    except Exception as exc:
      out += ["", title, f"讀取失敗：{exc}"]
      continue
    if not rep:
      continue
    out += ["", f"{title}｜至 {rep['lastDate'][5:].replace('-', '/')}"]
    for r in rep["rows"]:
      out.append(f"{_short(r['name'])}：{r['pnl']:+,.0f}（{r['pnlPct']:+.1f}%）｜累計 {r['totalPnl']:+,.0f}")
  return out
