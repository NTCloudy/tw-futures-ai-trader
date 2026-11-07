"""Weekly losing-trade review -> lesson library -> validated rules (Saturdays, part of weekly.py).

1. Review: every losing trade of the last weeks is tagged with a failure pattern:
   - consec_sl  : a stop-loss that was already the 2nd+ stop-loss in the same session
   - reversal   : entered against the previous stop-loss within 30 minutes (whipsaw)
   - early_open : entered in the first minutes of the session
2. Library: a pattern counts as a lesson only if it recurs (>= 2 of the last 4 weeks, >= 4 losing trades),
   so a single unlucky week does not change anything.
3. Candidate rule per lesson (max_sl / rev_cool / open_delay; implemented in wfo.simulate and in the live
   Trader._lesson_filter). It must beat the current rules on one year of data: full year, last 60 trading
   days, >= 3 of 4 quarters, and max drawdown not more than 10% worse.
4. Forward trial: the rule is replayed on the newly recorded live bars only (unseen data) for >= 10
   trading days. If it is not worse than the current rules there, it is adopted
   (data/strategy_params.json -> "lessons"); otherwise rejected and not retried for 8 weeks.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from .calendar import TPE
from .config import ROOT
from .ledger import Ledger

STATE = ROOT / "data" / "lessons.json"
PARAMS = ROOT / "data" / "strategy_params.json"
AIS = ("alpha-orb", "sigma-vwap", "nova-nightorb")
NAMES = {"alpha-orb": "Alpha", "sigma-vwap": "Sigma", "nova-nightorb": "Nova"}
TAG_ZH = {"consec_sl": "同一盤連續停損", "reversal": "停損後馬上反手", "early_open": "開盤急著進場", "other": "其他"}
OPEN_MIN = {"alpha-orb": 15, "sigma-vwap": 15, "nova-nightorb": 60}  # "early" = within N min of session open
CANDIDATES = {
    "consec_sl": {"nova-nightorb": [{"max_sl": 1}, {"max_sl": 2}], "*": [{"max_sl": 1}, {"max_sl": 2}, {"max_sl": 3}]},
    "reversal": {"*": [{"rev_cool": 30}, {"rev_cool": 60}]},
    "early_open": {"nova-nightorb": [{"open_delay": 45}, {"open_delay": 60}], "*": [{"open_delay": 15}, {"open_delay": 30}]},
}
TRIAL_DAYS = 10
RETRY_WEEKS = 8


def rule_zh(r: dict) -> str:
  out = []
  if r.get("max_sl"):
    out.append(f"一盤停損 {r['max_sl']} 次就休息")
  if r.get("rev_cool"):
    out.append(f"停損後 {r['rev_cool']} 分鐘內不反手")
  if r.get("open_delay"):
    out.append(f"開盤 {r['open_delay']} 分鐘內不進場")
  return "、".join(out) or "（無）"


def _session(ts: datetime) -> tuple:
  """(session date, name, open time) of a trade timestamp (day 08:45-13:45, night 15:00-05:00)."""
  if 8 <= ts.hour < 15:
    return (ts.date(), "day", ts.replace(hour=8, minute=45, second=0, microsecond=0))
  d = ts.date() if ts.hour >= 15 else ts.date() - timedelta(days=1)
  return (d, "night", datetime.combine(d, datetime.min.time(), TPE).replace(hour=15))


def review(rows: list[dict], ai: str) -> dict:
  """Tag the losing trades in `rows` (one AI, ordered by entry). Returns {tag: count} and the loss count."""
  tags = {k: 0 for k in TAG_ZH}
  sl_in = {}
  last_sl = {}
  losses = 0
  for r in rows:
    e = datetime.fromisoformat(r["entry_ts"])
    sess = _session(e)[:2]
    open_t = _session(e)[2]
    if r["pnl_twd"] < 0:
      losses += 1
      ls = last_sl.get(sess)
      if ls and ls[0] != r["side"] and e <= ls[1] + timedelta(minutes=30):
        tags["reversal"] += 1
      elif r["exit_kind"] == "SL" and sl_in.get(sess, 0) >= 1:
        tags["consec_sl"] += 1
      elif e < open_t + timedelta(minutes=OPEN_MIN[ai]):
        tags["early_open"] += 1
      else:
        tags["other"] += 1
    if r["exit_kind"] == "SL" and r["pnl_twd"] < 0:
      sl_in[sess] = sl_in.get(sess, 0) + 1
      last_sl[sess] = (r["side"], datetime.fromisoformat(r["exit_ts"]))
  return {"tags": tags, "losses": losses, "trades": len(rows)}


def weekly_reviews(ledger: Ledger, now: datetime, weeks: int = 4) -> dict:
  """{ai: [review of this week, last week, ...]}."""
  out = {}
  for ai in AIS:
    out[ai] = []
    for w in range(weeks):
      hi, lo = now - timedelta(days=7 * w), now - timedelta(days=7 * (w + 1))
      rows = ledger.rows("SELECT * FROM trades WHERE ai=? AND entry_ts>=? AND entry_ts<? ORDER BY entry_ts",
                         (ai, lo.isoformat(), hi.isoformat()))
      out[ai].append(review(rows, ai))
  return out


def recurring(revs: list[dict]) -> list[str]:
  """Patterns seen in >= 2 of the weeks with >= 4 losing trades in total (most frequent first)."""
  out = []
  for tag in ("consec_sl", "reversal", "early_open"):
    weeks = sum(1 for r in revs if r["tags"][tag] > 0)
    total = sum(r["tags"][tag] for r in revs)
    if weeks >= 2 and total >= 4:
      out.append((total, tag))
  return [t for _, t in sorted(out, reverse=True)]


def _specs(lessons: dict) -> dict:
  from .replay import sim_specs
  specs = sim_specs()
  sim_ai, p = specs["sigma-vwap"]
  p = dict(p)  # Current live Sigma: time-limited throughout (drop the historical any-time period).
  p.pop("hours_free", None)
  specs["sigma-vwap"] = (sim_ai, p)
  return {ai: (sa, {**sp, **lessons.get(ai, {})}) for ai, (sa, sp) in specs.items() if ai in AIS}


def _metrics(cfg, sim_ai, p, sessions, days) -> dict:
  from . import wfo
  q = max(1, len(days) // 4)
  quarters = [set(days[i * q:(i + 1) * q if i < 3 else len(days)]) for i in range(4)]
  last60 = set(days[-60:])
  full = wfo.simulate(cfg, sim_ai, p, sessions)
  return {"pnl": full["pnl"], "maxdd": full["maxdd"], "trades": full["trades"],
          "last60": wfo.simulate(cfg, sim_ai, p, [s for s in sessions if s.td in last60])["pnl"],
          "q": [wfo.simulate(cfg, sim_ai, p, [s for s in sessions if s.td in qq])["pnl"] for qq in quarters]}


def backtest(cfg, sim_ai, base, rule, sessions, days, base_m=None) -> dict:
  b = base_m or _metrics(cfg, sim_ai, base, sessions, days)
  x = _metrics(cfg, sim_ai, {**base, **rule}, sessions, days)
  qwin = sum(1 for a, c in zip(x["q"], b["q"]) if a > c)
  ok = x["pnl"] > b["pnl"] and x["last60"] >= b["last60"] and qwin >= 3 and x["maxdd"] <= b["maxdd"] * 1.1 + 1
  return {"ok": ok, "gain": round(x["pnl"] - b["pnl"]), "gain60": round(x["last60"] - b["last60"]), "qwin": qwin,
          "dd": round(x["maxdd"]), "base_dd": round(b["maxdd"]), "trades": x["trades"], "base_trades": b["trades"]}


def _live_sessions(since: datetime):
  from . import wfo
  from .replay import LIVE_1M
  if not LIVE_1M.exists():
    return []
  return [s for s in wfo.sessions_from_kbars(LIVE_1M) if s.bars[0].end >= since]


def load_state() -> dict:
  st = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
  st.setdefault("trials", {})
  st.setdefault("rejected", [])
  st.setdefault("adopted", [])
  return st


def run(cfg: dict, ledger: Ledger, sessions: list, now: datetime | None = None, adopt: bool = True) -> dict:
  """Weekly step. Returns {"lines": [...LINE text...], "changed": bool}."""
  from . import wfo
  now = now or datetime.now(TPE)
  c = dict(cfg, max_entries_per_session=999)
  st = load_state()
  params = json.loads(PARAMS.read_text(encoding="utf-8")) if PARAMS.exists() else {}
  lessons = params.setdefault("lessons", {})
  specs = _specs(lessons)
  days = sorted({s.td for s in sessions})
  revs = weekly_reviews(ledger, now)
  lines = ["", "📚 本週學到什麼（虧損檢討）"]
  changed = False
  for ai in AIS:
    r0 = revs[ai][0]
    if r0["losses"]:
      parts = "、".join(f"{TAG_ZH[t]} {n}" for t, n in r0["tags"].items() if n)
      lines.append(f"{NAMES[ai]}：虧 {r0['losses']}/{r0['trades']} 筆｜{parts}")
    else:
      lines.append(f"{NAMES[ai]}：本週沒有虧損單" if r0["trades"] else f"{NAMES[ai]}：本週沒有交易")
  for ai in AIS:
    sim_ai, base = specs[ai]
    trial = st["trials"].get(ai)
    if trial:  # Forward check on live bars recorded since the trial started.
      since = datetime.fromisoformat(trial["since"])
      live = _live_sessions(since)
      n_days = len({s.td for s in live})
      b = wfo.simulate(c, sim_ai, base, live)["pnl"]
      x = wfo.simulate(c, sim_ai, {**base, **trial["rule"]}, live)["pnl"]
      head = f"🧪 {NAMES[ai]} 驗證中「{rule_zh(trial['rule'])}」：{n_days}/{TRIAL_DAYS} 天，套用 {x:+,.0f} vs 不套用 {b:+,.0f}"
      if n_days < TRIAL_DAYS:
        lines.append(head)
        continue
      del st["trials"][ai]
      if x >= b and adopt:
        lessons[ai] = {**lessons.get(ai, {}), **trial["rule"]}
        st["adopted"].append({**trial, "ai": ai, "at": now.date().isoformat(), "fwd": [round(x), round(b)]})
        changed = True
        lines.append(head + " → ✅ 正式採用")
      else:
        st["rejected"].append({**trial, "ai": ai, "at": now.date().isoformat(), "fwd": [round(x), round(b)]})
        lines.append(head + (" → ❌ 實戰沒有比較好，淘汰" if x < b else " → 通過（未自動套用）"))
      continue
    tags = recurring(revs[ai])
    if not tags:
      continue
    cutoff = (now - timedelta(weeks=RETRY_WEEKS)).date().isoformat()
    tried = [j["rule"] for j in st["rejected"] if j["ai"] == ai and j["at"] >= cutoff]
    have = lessons.get(ai, {})
    base_m = _metrics(c, sim_ai, base, sessions, days)
    best = None
    for tag in tags:
      for rule in CANDIDATES[tag].get(ai, CANDIDATES[tag]["*"]):
        if rule in tried or all(have.get(k) == v for k, v in rule.items()):
          continue
        bt = backtest(c, sim_ai, base, rule, sessions, days, base_m)
        txt = (f"一年 {bt['gain']:+,}、近60天 {bt['gain60']:+,}、四季贏 {bt['qwin']}/4、"
               f"回撤 {bt['dd']:,}（原 {bt['base_dd']:,}）")
        if bt["ok"] and (best is None or bt["gain"] > best[2]["gain"]):
          best = (tag, rule, bt, txt)
        elif not bt["ok"]:
          st["rejected"].append({"ai": ai, "rule": rule, "tag": tag, "at": now.date().isoformat(), "bt": bt})
          lines.append(f"🔍 {NAMES[ai]} 反覆出現「{TAG_ZH[tag]}」→ 試「{rule_zh(rule)}」：{txt} → 回測沒變好，不採用")
    if best:
      tag, rule, bt, txt = best
      st["trials"][ai] = {"rule": rule, "tag": tag, "since": now.isoformat(timespec="seconds"), "bt": bt}
      lines.append(f"🔍 {NAMES[ai]} 反覆出現「{TAG_ZH[tag]}」→ 試「{rule_zh(rule)}」：{txt} → 回測通過，開始實戰驗證 {TRIAL_DAYS} 天")
  active = {ai: r for ai, r in lessons.items() if r}
  if active:
    lines.append("✅ 目前已採用：" + "；".join(f"{NAMES.get(ai, ai)} {rule_zh(r)}" for ai, r in active.items()))
  STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
  if changed:
    PARAMS.write_text(json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8")
  return {"lines": lines, "changed": changed}
