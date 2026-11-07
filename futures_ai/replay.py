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
UPGRADE_TS = "2026-11-08T00:00:00+08:00"  # Consensus upgrade boundary (pre-upgrade vs post-upgrade live rules).


def sim_specs(legacy: bool = False) -> dict:
  """Simulator equivalents of the live AIs, built from the live parameters."""
  params_file = ROOT / "data" / "strategy_params.json"
  lp = json.loads(params_file.read_text(encoding="utf-8")) if params_file.exists() else {}
  a = lp.get("alpha-orb", {"range_bars": 3, "sl_frac": 0.75, "tp_mult": 3.0})
  g = lp.get("sigma-vwap", {"pull_atr": 0.5, "sl_atr": 2.0, "tp_mult": 4.0, "trail_atr": None})
  if legacy:
    nova = dict(anchor="night", range_min=30, window=600, sl_frac=1.0, tp_mult=3.0, regimes=("trend", "chop"),
                flip=(), limit_atr=0.0, limit_min=5, fill_through=1,
                min_width=0, reach_r=None, min_body_atr=0, dst_skip=None)
    nova.update(lp.get("nova-nightorb", {}))
    les = lp.get("lessons", {})
    specs = {
        "alpha-orb": ("alpha-orb", dict(a, regimes=("trend", "chop"), flip=(), limit_atr=0.0, limit_min=5,
                                        hours=("us_open",), fill_through=1, min_width=0, max_width=450, be_r=None)),
        "sigma-vwap": ("sigma-pullback", dict(g, regimes=("trend",), flip=(), limit_atr=0.2, limit_min=10,
                                               hours=("us_open", "night_open"), fill_through=1, max_wick=None,
                                               hours_free=("2026-10-28T12:00:00+08:00", "2026-11-07T12:30:00+08:00"))),
        "nova-nightorb": ("anchor-orb", nova),
        "nova-us": ("anchor-orb", dict(nova, hours=("us_open",), be_r=1.75)),
    }
    return {ai: (sa, {**p, **les.get("nova-nightorb" if ai == "nova-us" else ai, {}), **les.get(ai, {})})
            for ai, (sa, p) in specs.items()}
  nova = dict(anchor="night", range_min=30, window=600, sl_frac=1.0, tp_mult=3.0, regimes=("trend", "chop"),
              flip=(), limit_atr=0.0, limit_min=5, fill_through=1,
              min_width=30, reach_r=2.5, min_body_atr=0.25, dst_skip=(150, 150))
  nova.update(lp.get("nova-nightorb", {}))
  les = lp.get("lessons", {})  # Rules adopted from the weekly losing-trade review (lessons.py).
  specs = {
      "alpha-orb": ("alpha-orb", dict(a, regimes=("trend", "chop"), flip=(), limit_atr=0.0, limit_min=5,
                                      hours=("us_open",), fill_through=1, min_width=40, max_width=260, be_r=2.0)),
      # Sigma: time-limited, max 2 entries/session, max_wick=0.25 (with historical any-time window kept for replay).
      "sigma-vwap": ("sigma-pullback", dict(g, regimes=("trend",), flip=(), limit_atr=0.2, limit_min=10,
                                             hours=("us_open", "night_open"), fill_through=1,
                                             max_entries=2, max_wick=0.25,
                                             hours_free=("2026-10-28T12:00:00+08:00", "2026-11-07T12:30:00+08:00"))),
      "nova-nightorb": ("anchor-orb", nova),
      "nova-us": ("anchor-orb", dict(nova, hours=("us_open",), be_r=1.75)),
  }
  return {ai: (sa, {**p, **les.get("nova-nightorb" if ai == "nova-us" else ai, {}), **les.get(ai, {})})
          for ai, (sa, p) in specs.items()}


def _match(live: list[dict], sim: list[dict], match_min: int = MATCH_MIN) -> tuple[list, list, list]:
  both, used = [], set()
  live_only = []
  for lt in live:
    hit = None
    for j, st in enumerate(sim):
      if j in used or st["side"] != lt["side"]:
        continue
      if abs((st["entry"] - lt["entry"]).total_seconds()) <= match_min * 60:
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
  d = {"entry": t["entry"].strftime("%m/%d %H:%M"), "side": t["side"], "pnl": round(t["pnl"]), "kind": t["kind"]}
  if t.get("note"):
    d["note"] = t["note"]
  return d


EVAL_START = "2026-10-19T08:45:00+08:00"  # Evaluation period start (Monday day session).


def run_replay(cfg: dict, since: str = EVAL_START) -> dict:
  from . import wfo
  from .swing import simulate_swing
  t0 = datetime.fromisoformat(since)
  up_ts = datetime.fromisoformat(UPGRADE_TS)
  allsess = wfo.load_sessions(cfg)
  live_sessions = [s for s in allsess if s.bars and s.bars[0].end >= t0]
  pre_sessions = [s for s in live_sessions if s.bars[0].end < up_ts]
  post_sessions = [s for s in live_sessions if s.bars[0].end >= up_ts]
  ledger = Ledger(cfg["db_path"])
  shadow = Ledger(str(ROOT / "data" / "shadow.db")) if (ROOT / "data" / "shadow.db").exists() else None
  try:
    c_pre = dict(cfg, max_entries_per_session=999, enforce_daily_loss=False)
    c_post = dict(cfg)
    out = {"generated_at": datetime.now(TPE).isoformat(timespec="seconds"), "since": t0.strftime("%m/%d %H:%M"),
           "sessions": len(live_sessions), "ais": []}
    names = {}
    from .strategies import build_strategies
    for s in build_strategies():
      names[s.id] = s.name
    names["nova-us"] = "Nova 美股開盤（影子）"

    eq_rows = ledger.rows("SELECT DISTINCT substr(ts, 1, 16) m FROM equity WHERE ts>=? ORDER BY m", (since,))
    eq_times = []
    for r in eq_rows:
      try:
        dt = datetime.fromisoformat(r["m"] + ":00")
        eq_times.append(dt if dt.tzinfo else dt.replace(tzinfo=TPE))
      except Exception:
        pass

    def is_offline(ts: datetime) -> bool:
      if not eq_times:
        return False
      import bisect
      idx = bisect.bisect_left(eq_times, ts)
      candidates = []
      if idx < len(eq_times):
        candidates.append(abs((eq_times[idx] - ts).total_seconds()))
      if idx > 0:
        candidates.append(abs((eq_times[idx - 1] - ts).total_seconds()))
      return min(candidates) > 600 if candidates else False

    def live_trades(ai: str) -> list[dict]:
      rows = ledger.rows("SELECT * FROM trades WHERE ai=? AND entry_ts>=? ORDER BY id", (ai, since))
      if ai == "nova-us" and shadow is not None:  # Front-stage until 10/22, a background shadow since then.
        rows += shadow.rows("SELECT * FROM trades WHERE ai=? AND entry_ts>=? ORDER BY id", (ai, since))
      return [{"entry": datetime.fromisoformat(r["entry_ts"]), "side": r["side"], "pnl": r["pnl_twd"],
               "kind": r["exit_kind"]} for r in rows]

    pre_specs = sim_specs(legacy=True)
    post_specs = sim_specs(legacy=False)
    for ai, (sim_ai, p_post) in post_specs.items():
      _, p_pre = pre_specs[ai]
      r_pre = wfo.simulate(c_pre, sim_ai, p_pre, pre_sessions, record=True) if pre_sessions else {"log": []}
      r_post = wfo.simulate(c_post, sim_ai, p_post, post_sessions, record=True) if post_sessions else {"log": []}
      sim = [{"entry": e, "side": side, "pnl": net, "kind": kind}
             for e, _x, side, net, kind in (r_pre.get("log", []) + r_post.get("log", [])) if e and e >= t0]
      lt = live_trades(ai)
      both, lo, so = _match(lt, sim)
      for st in so:
        if is_offline(st["entry"]):
          st["note"] = "engine_offline"
      open_p = (shadow if ai == "nova-us" and shadow else ledger).get_state(f"pos:{ai}")
      out["ais"].append({"id": ai, "name": names.get(ai, ai), "live_n": len(lt), "sim_n": len(sim),
                         "live_pnl": round(sum(t["pnl"] for t in lt)), "sim_pnl": round(sum(t["pnl"] for t in sim)),
                         "matched": len(both), "live_only": [_fmt(t) for t in lo], "sim_only": [_fmt(t) for t in so],
                         "open_live": {"side": open_p["side"], "entry_price": open_p["entry_price"],
                                       "entry_ts": open_p["entry_ts"][5:16].replace("T", " ")} if open_p else None,
                         "pairs": [{"live": _fmt(a), "sim": _fmt(b)} for a, b in both]})

    # Omega (swing, multi-day): needs the EMA60 history, so replay the year of K-bars plus the live bars.
    try:
      rs = simulate_swing(c_post, dict(lookback=10, atr_k=4.0, direction="both", entry="limit", limit_atr=0.3,
                                       tp_atr=10.0, fill_through=1, min_er=0.20, be_r=0.7), allsess, record=True)
      sim = [{"entry": e, "side": side, "pnl": net, "kind": why} for e, _x, side, _ep, _xp, net, why in rs["log"]
             if e >= t0]
      lt = live_trades("omega-swing")
      both, lo, so = _match(lt, sim, match_min=60)
      for st in so:
        if is_offline(st["entry"]):
          st["note"] = "engine_offline"
      open_p = ledger.get_state("pos:omega-swing")
      open_s = rs.get("open_pos")
      out["ais"].append({"id": "omega-swing", "name": names.get("omega-swing", "Omega"), "live_n": len(lt),
                         "sim_n": len(sim), "live_pnl": round(sum(t["pnl"] for t in lt)),
                         "sim_pnl": round(sum(t["pnl"] for t in sim)), "matched": len(both),
                         "live_only": [_fmt(t) for t in lo], "sim_only": [_fmt(t) for t in so],
                         "open_live": {"side": open_p["side"], "entry_price": open_p["entry_price"],
                                       "entry_ts": open_p["entry_ts"][5:16].replace("T", " ")} if open_p else None,
                         "open_sim": {"side": open_s["side"], "entry_price": open_s["entry"],
                                      "entry_ts": open_s["ts"].strftime("%m/%d %H:%M")} if open_s else None,
                         "pairs": [{"live": _fmt(a), "sim": _fmt(b)} for a, b in both]})
    except Exception as exc:  # The swing replay is best-effort.
      out["omega_error"] = str(exc)

    REPORT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
  finally:
    ledger.conn.close()
    if shadow is not None:
      shadow.conn.close()


def summary_lines(rep: dict) -> list[str]:
  lines = ["🔁 回測 vs 即時（同一段行情重跑回測，NT$）", "AI｜即時｜回測"]
  for a in rep["ais"]:
    lines.append(f"{a['name'].split(' ')[0]}{' 美股' if a['id'] == 'nova-us' else ''}｜{a['live_pnl']:+,}｜{a['sim_pnl']:+,}")
  return lines
