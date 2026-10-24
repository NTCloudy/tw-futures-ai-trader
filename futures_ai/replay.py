"""Backtest-vs-live reconciliation (回測 vs 即時 對帳).

Replays the simulator on the exact 1-minute bars the live engine recorded (data/live_1m.csv) and
compares, trade by trade, with what the live paper AIs actually did since the evaluation start.
If the two agree, the one-year backtest numbers can be trusted much more; if they diverge, the
report shows where (signals the live engine skipped, missed limit fills, different exits).

Run: python3 -m futures_ai replay   (cron: after each session). Output: data/replay_report.json
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .calendar import TPE
from .config import ROOT
from .ledger import Ledger

LIVE_1M = ROOT / "data" / "live_1m.csv"
KBARS = ROOT / "data" / "kbars_repo" / "TMF_1min.csv"
REPORT = ROOT / "data" / "replay_report.json"
MATCH_MIN = 10  # A live and a simulated entry on the same side within 10 minutes count as the same trade.


def sim_specs() -> dict:
  """Simulator equivalents of the live AIs, built from the live parameters."""
  params_file = ROOT / "data" / "strategy_params.json"
  lp = json.loads(params_file.read_text(encoding="utf-8")) if params_file.exists() else {}
  a = lp.get("alpha-orb", {"range_bars": 3, "sl_frac": 0.75, "tp_mult": 3.0})
  g = lp.get("sigma-vwap", {"pull_atr": 0.5, "sl_atr": 2.0, "tp_mult": 4.0, "trail_atr": None})
  nova = dict(anchor="night", range_min=30, window=600, sl_frac=1.0, tp_mult=3.0, regimes=("trend", "chop"),
              flip=(), limit_atr=0.0, limit_min=5, fill_through=1)
  return {
      "alpha-orb": ("alpha-orb", dict(a, regimes=("trend", "chop"), flip=(), limit_atr=0.0, limit_min=5,
                                      hours=("us_open",), fill_through=1)),
      "sigma-vwap": ("sigma-pullback", dict(g, regimes=("trend",), flip=(), limit_atr=0.2, limit_min=10,
                                             hours=("us_open", "night_open"), fill_through=1)),
      "nova-nightorb": ("anchor-orb", nova),
      "nova-us": ("anchor-orb", dict(nova, hours=("us_open",))),
  }


def _match(live: list[dict], sim: list[dict]) -> tuple[list, list, list]:
  both, used = [], set()
  live_only = []
  for lt in live:
    hit = None
    for j, st in enumerate(sim):
      if j in used or st["side"] != lt["side"]:
        continue
      if abs((st["entry"] - lt["entry"]).total_seconds()) <= MATCH_MIN * 60:
        hit = j
        break
    if hit is None:
      live_only.append(lt)
    else:
      used.add(hit)
      both.append((lt, sim[hit]))
  sim_only = [st for j, st in enumerate(sim) if j not in used]
  return both, live_only, sim_only


def _fmt(t: dict) -> dict:
  return {"entry": t["entry"].strftime("%m/%d %H:%M"), "side": t["side"], "pnl": round(t["pnl"]), "kind": t["kind"]}


EVAL_START = "2026-10-19T08:45:00+08:00"  # Evaluation period start (Monday day session).


def run_replay(cfg: dict, since: str = EVAL_START) -> dict:
  from . import wfo
  from .swing import simulate_swing
  t0 = datetime.fromisoformat(since)
  live_sessions = [s for s in wfo.sessions_from_kbars(LIVE_1M) if s.bars[0].end >= t0] if LIVE_1M.exists() else []
  ledger = Ledger(cfg["db_path"])
  c = dict(cfg, max_entries_per_session=999)
  out = {"generated_at": datetime.now(TPE).isoformat(timespec="seconds"), "since": t0.strftime("%m/%d %H:%M"),
         "sessions": len(live_sessions), "ais": []}
  names = {}
  from .strategies import build_strategies
  for s in build_strategies():
    names[s.id] = s.name
  names["nova-us"] = "Nova 美股開盤（影子）"

  shadow = Ledger(str(ROOT / "data" / "shadow.db")) if (ROOT / "data" / "shadow.db").exists() else None

  def live_trades(ai: str) -> list[dict]:
    rows = ledger.rows("SELECT * FROM trades WHERE ai=? AND entry_ts>=? ORDER BY id", (ai, since))
    if ai == "nova-us" and shadow is not None:  # Front-stage until 10/22, a background shadow since then.
      rows += shadow.rows("SELECT * FROM trades WHERE ai=? AND entry_ts>=? ORDER BY id", (ai, since))
    return [{"entry": datetime.fromisoformat(r["entry_ts"]), "side": r["side"], "pnl": r["pnl_twd"],
             "kind": r["exit_kind"]} for r in rows]

  for ai, (sim_ai, p) in sim_specs().items():
    r = wfo.simulate(c, sim_ai, p, live_sessions, record=True)
    sim = [{"entry": e, "side": side, "pnl": net, "kind": kind} for e, _x, side, net, kind in r.get("log", [])
           if e and e >= t0]
    lt = live_trades(ai)
    both, lo, so = _match(lt, sim)
    out["ais"].append({"id": ai, "name": names.get(ai, ai), "live_n": len(lt), "sim_n": len(sim),
                       "live_pnl": round(sum(t["pnl"] for t in lt)), "sim_pnl": round(sum(t["pnl"] for t in sim)),
                       "matched": len(both), "live_only": [_fmt(t) for t in lo], "sim_only": [_fmt(t) for t in so],
                       "pairs": [{"live": _fmt(a), "sim": _fmt(b)} for a, b in both]})

  # Omega (swing, multi-day): needs the EMA60 history, so replay the year of K-bars plus the live bars.
  try:
    hist = wfo.sessions_from_kbars(KBARS) if KBARS.exists() else []
    seen = {(s.td, s.name) for s in hist}
    allsess = sorted(hist + [s for s in live_sessions if (s.td, s.name) not in seen], key=lambda s: s.bars[0].end)
    rs = simulate_swing(c, dict(lookback=10, atr_k=4.0, direction="both", entry="limit", limit_atr=0.3,
                                tp_atr=10.0, fill_through=1), allsess, record=True)
    sim = [{"entry": e, "side": side, "pnl": net, "kind": why} for e, _x, side, _ep, _xp, net, why in rs["log"]
           if e >= t0]
    lt = live_trades("omega-swing")
    both, lo, so = _match(lt, sim)
    out["ais"].append({"id": "omega-swing", "name": names.get("omega-swing", "Omega"), "live_n": len(lt),
                       "sim_n": len(sim), "live_pnl": round(sum(t["pnl"] for t in lt)),
                       "sim_pnl": round(sum(t["pnl"] for t in sim)), "matched": len(both),
                       "live_only": [_fmt(t) for t in lo], "sim_only": [_fmt(t) for t in so],
                       "pairs": [{"live": _fmt(a), "sim": _fmt(b)} for a, b in both]})
  except Exception as exc:  # The swing replay is best-effort.
    out["omega_error"] = str(exc)

  REPORT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
  return out


def summary_lines(rep: dict) -> list[str]:
  lines = ["🔁 回測 vs 即時（同一段行情重跑回測，NT$）", "AI｜即時｜回測"]
  for a in rep["ais"]:
    lines.append(f"{a['name'].split(' ')[0]}{' 美股' if a['id'] == 'nova-us' else ''}｜{a['live_pnl']:+,}｜{a['sim_pnl']:+,}")
  return lines
