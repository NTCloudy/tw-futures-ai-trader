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
4. Forward trial (mandatory, no immediate adoption): the rule is replayed on the live bars recorded AFTER
   the trial starts (unseen data) for >= 10 trading days. A rule that already lost on the recent live bars is
   rejected without a trial. If it is not worse than the current rules in the trial, it is adopted
   (data/strategy_params.json -> "lessons"); otherwise rejected and not retried for 8 weeks.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from .calendar import TPE
from .config import ROOT
from .ledger import Ledger

STATE = ROOT / "data" / "lessons.json"
PARAMS = ROOT / "data" / "strategy_params.json"
AIS = ("alpha-orb", "sigma-vwap", "nova-nightorb", "omega-swing")
NAMES = {"alpha-orb": "Alpha", "sigma-vwap": "Sigma", "nova-nightorb": "Nova", "omega-swing": "Omega"}
TAG_ZH = {
    "consec_sl": "同一盤連續停損",
    "reversal": "停損後馬上反手",
    "early_open": "開盤急著進場",
    "giveback": "大幅浮盈後回吐停損",
    "overextended": "乖離過大追高殺低",
    "weak_trend": "盤整或弱趨勢假突破",
    "other": "其他",
}
ACTIONABLE_TAGS = ("consec_sl", "reversal", "early_open", "giveback", "overextended", "weak_trend")
OPEN_MIN = {"alpha-orb": 15, "sigma-vwap": 15, "nova-nightorb": 60}
CANDIDATES = {
    "consec_sl": {"nova-nightorb": [{"max_sl": 1}, {"max_sl": 2}, {"max_sl": 3}], "*": [{"max_sl": 1}, {"max_sl": 2}]},
    "reversal": {"*": [{"rev_cool": 30}, {"rev_cool": 60}]},
    "early_open": {"nova-nightorb": [{"open_delay": 45}, {"open_delay": 60}], "*": [{"open_delay": 15}, {"open_delay": 30}]},
    "giveback": {"omega-swing": [{"be_r": 0.5}, {"gb_keep": 0.5, "gb_act": 0.5}],
                 "*": [{"be_r": 1.5}, {"be_r": 1.75}, {"be_r": 2.0}]},
    "weak_trend": {"omega-swing": [{"min_er": 0.25}, {"min_er": 0.30}],
                   "*": [{"min_er": 0.15}, {"min_er": 0.20}, {"min_er": 0.25}]},
    "overextended": {"omega-swing": [{"max_ext_atr": 3.5}, {"max_sl_pts": 1200},
                                     {"max_ext_atr": 3.5, "max_sl_pts": 1200}],
                     "*": [{"max_vwap_pts": 200}, {"max_vwap_atr": 2.5}]},
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
  if r.get("be_r"):
    out.append(f"浮盈達 {r['be_r']:g} 倍停損移至保本+6")
  if r.get("gb_keep"):
    out.append(f"浮盈過半後鎖住 {int(r['gb_keep'] * 100)}% 最高獲利")
  if r.get("min_er"):
    out.append(f"效率比 ER ≥ {r['min_er']:.2f} 才進場")
  if r.get("max_ext_atr"):
    out.append(f"60分K 乖離 ≤ {r['max_ext_atr']:g}×ATR 才追突破")
  if r.get("max_sl_pts"):
    out.append(f"單筆停損上限 {r['max_sl_pts']:.0f} 點")
  if r.get("max_vwap_atr"):
    out.append(f"距均價 ≤ {r['max_vwap_atr']:g}×ATR 才進場")
  if r.get("max_vwap_pts"):
    out.append(f"距均價 ≤ {r['max_vwap_pts']:.0f} 點才進場")
  return "、".join(out) or "（無）"


def _session(ts: datetime) -> tuple:
  """(session date, name, open time) of a trade timestamp (day 08:45-13:45, night 15:00-05:00)."""
  if 8 <= ts.hour < 15:
    return (ts.date(), "day", ts.replace(hour=8, minute=45, second=0, microsecond=0))
  d = ts.date() if ts.hour >= 15 else ts.date() - timedelta(days=1)
  return (d, "night", datetime.combine(d, datetime.min.time(), TPE).replace(hour=15))


def review(rows: list[dict], ai: str, journal: list[dict] | None = None, protected: set | None = None) -> dict:
  """Tag the losing trades in `rows` (one AI, ordered by entry). Returns {tag: count} and the loss count.
  protected: exit_ts of protected stop exits (protected mode); losing ones are neither losses nor stops here
  (a winning protected exit still resets the counters, as in the simulator)."""
  tags = {k: 0 for k in TAG_ZH}
  sl_in = {}
  last_sl = {}
  losses = 0
  j_by_ts = {}
  for j in (journal or []):
    if j.get("entry_ts"):
      j_by_ts[j["entry_ts"]] = j
    if j.get("ts"):
      j_by_ts[j["ts"]] = j
  if protected:
    rows = [r for r in rows if not (r["exit_kind"] == "SL" and r["pnl_twd"] < 0 and r["exit_ts"] in protected)]
  for r in rows:
    e = datetime.fromisoformat(r["entry_ts"])
    sig_t = e.replace(second=0, microsecond=0) - timedelta(minutes=e.minute % 5)
    sess = _session(e)[:2]
    open_t = _session(e)[2]
    if r["pnl_twd"] < 0:
      losses += 1
      matched = False
      ls = last_sl.get(sess)
      if ls and ls[0] != r["side"] and sig_t < ls[1] + timedelta(minutes=30):
        tags["reversal"] += 1
        matched = True
      if r["exit_kind"] == "SL" and sl_in.get(sess, 0) >= 1:
        tags["consec_sl"] += 1
        matched = True
      if ai in OPEN_MIN and sig_t < open_t + timedelta(minutes=OPEN_MIN[ai]):
        tags["early_open"] += 1
        matched = True
      j = j_by_ts.get(r.get("entry_ts")) or j_by_ts.get(r.get("exit_ts")) or {}
      reason = r.get("entry_reason") or ""
      diag = j.get("diagnosis") or ""
      mfe = j.get("mfe")
      sl_d = j.get("sl_dist")
      tp_d = j.get("tp_dist")
      if (mfe is not None and ((sl_d and mfe >= 1.5 * sl_d) or (tp_d and mfe >= 0.5 * tp_d))) or "回吐到停損" in diag:
        tags["giveback"] += 1
        matched = True
      m_sl = re.search(r"移動停損\s*(\d+(?:\.\d+)?)\s*點", reason)
      m_em = re.search(r"收盤\s*(\d+(?:\.\d+)?).*?EMA20\s*(\d+(?:\.\d+)?)", reason)
      m_rg = re.search(r"區間\s*(\d+(?:\.\d+)?)\s*點", reason)
      m_vw = re.search(r"VWAP\s*(\d+(?:\.\d+)?)", reason)
      sl_pts_r = float(m_sl.group(1)) if m_sl else 0.0
      ext_pts = abs(float(m_em.group(1)) - float(m_em.group(2))) if m_em else 0.0
      atr_est = (sl_pts_r / 4.0) if sl_pts_r > 0 else 0.0
      vw_dist = abs(float(r.get("entry_price") or 0) - float(m_vw.group(1))) if (m_vw and r.get("entry_price")) else 0.0
      if (ai == "omega-swing" and (sl_pts_r > 1200 or (atr_est > 0 and ext_pts > 2.8 * atr_est))) or \
         (m_rg and float(m_rg.group(1)) > 260) or (vw_dist > 200):
        tags["overextended"] += 1
        matched = True
      m_er = re.search(r"ER\s*(\d+(?:\.\d+)?)", reason)
      er_val = float(m_er.group(1)) if m_er else None
      if "盤整盤" in reason or j.get("regime") == "chop" or (er_val is not None and er_val < 0.25):
        tags["weak_trend"] += 1
        matched = True
      if not matched:
        tags["other"] += 1
    if r["pnl_twd"] > 0:
      sl_in[sess] = 0
      last_sl.pop(sess, None)
    elif r["exit_kind"] == "SL" and r["pnl_twd"] < 0:
      sl_in[sess] = sl_in.get(sess, 0) + 1
      last_sl[sess] = (r["side"], datetime.fromisoformat(r["exit_ts"]))
  return {"tags": tags, "losses": losses, "trades": len(rows)}


def weekly_reviews(ledger: Ledger, now: datetime, weeks: int = 4, respect_since: bool = False,
                   protected_mode: bool = False) -> dict:
  """{ai: [review of this week, last week, ...]}."""
  out = {}
  for ai in AIS:
    prot = set(ledger.get_state(f"protected_exits:{ai}") or []) if protected_mode else None
    b_state = ledger.get_state(f"brain:{ai}") or {}
    journal = b_state.get("journal") or []
    since_ts = ledger.get_state(f"strategy_since:{ai}") if respect_since else None
    out[ai] = []
    for w in range(weeks):
      hi, lo = now - timedelta(days=7 * w), now - timedelta(days=7 * (w + 1))
      rows = ledger.rows("SELECT * FROM trades WHERE ai=? AND exit_ts>=? AND exit_ts<? ORDER BY entry_ts",
                         (ai, lo.isoformat(), hi.isoformat()))
      if since_ts:
        rows = [r for r in rows if r["entry_ts"] >= since_ts]
      out[ai].append(review(rows, ai, journal=journal, protected=prot))
  return out


def recurring(revs: list[dict], min_total: int = 4) -> list[str]:
  """Patterns seen in >= 2 of the weeks with >= min_total losing trades in total (2 for Omega swing, 4 for intraday)."""
  out = []
  for tag in ACTIONABLE_TAGS:
    weeks = sum(1 for r in revs if r["tags"].get(tag, 0) > 0)
    total = sum(r["tags"].get(tag, 0) for r in revs)
    if weeks >= 2 and total >= min_total:
      out.append((total, tag))
  return [t for _, t in sorted(out, reverse=True)]


def _specs(lessons: dict) -> dict:
  from . import slowtrain
  from .replay import sim_specs
  specs = sim_specs()
  sim_ai, p = specs["sigma-vwap"]
  p = dict(p)  # Current live Sigma: time-limited throughout (drop the historical any-time period).
  p.pop("hours_free", None)
  specs["sigma-vwap"] = (sim_ai, p)
  om_p = {**slowtrain.OMEGA_BASE, **slowtrain.LIVE_DEFAULTS["omega-swing"]}
  specs["omega-swing"] = ("omega-swing", om_p)
  return {ai: (sa, {**sp, **lessons.get(ai, {})}) for ai, (sa, sp) in specs.items() if ai in AIS}


def _sim_one(cfg, sim_ai: str, p: dict, sessions: list, start_td=None, record: bool = False) -> dict:
  from . import wfo
  from .swing import simulate_swing
  if sim_ai == "omega-swing":
    r = simulate_swing(cfg, p, sessions, record=record, start_td=start_td, close_at_end=True)
    if record:
      r["log"] = [(e, x, side, net, why) for e, x, side, _ep, _xp, net, why in r.get("log", [])]
    return r
  scoped = [s for s in sessions if start_td is None or s.td >= start_td]
  return wfo.simulate(cfg, sim_ai, p, scoped, record=record)


def _metrics(cfg, sim_ai, p, sessions, days) -> dict:
  q = max(1, len(days) // 4)
  quarters = [ sorted(days[i * q:(i + 1) * q if i < 3 else len(days)]) for i in range(4)]
  last60_start = days[-60] if len(days) >= 60 else days[0]
  full = _sim_one(cfg, sim_ai, p, sessions)
  if sim_ai == "omega-swing":
    last60_pnl = _sim_one(cfg, sim_ai, p, sessions, start_td=last60_start)["pnl"]
    q_pnls = []
    for i, qq in enumerate(quarters):
      end_d = qq[-1]
      sub_s = [s for s in sessions if s.td <= end_d]
      q_pnls.append(_sim_one(cfg, sim_ai, p, sub_s, start_td=qq[0])["pnl"])
  else:
    last60_set = set(days[-60:])
    last60_pnl = _sim_one(cfg, sim_ai, p, [s for s in sessions if s.td in last60_set])["pnl"]
    q_pnls = [_sim_one(cfg, sim_ai, p, [s for s in sessions if s.td in set(qq)])["pnl"] for qq in quarters]
  return {"pnl": full["pnl"], "maxdd": full["maxdd"], "trades": full["trades"],
          "last60": last60_pnl, "q": q_pnls}


def backtest(cfg, sim_ai, base, rule, sessions, days, base_m=None) -> dict:
  b = base_m or _metrics(cfg, sim_ai, base, sessions, days)
  x = _metrics(cfg, sim_ai, {**base, **rule}, sessions, days)
  q_no_regress = sum(1 for a, c in zip(x["q"], b["q"]) if a >= c)
  q_improve = sum(1 for a, c in zip(x["q"], b["q"]) if a > c)
  ok = (x["pnl"] > b["pnl"] and x["last60"] >= b["last60"]
        and q_no_regress >= 3 and q_improve >= 2
        and x["maxdd"] <= b["maxdd"] * 1.1 + 1)
  return {"ok": ok, "gain": round(x["pnl"] - b["pnl"]), "gain60": round(x["last60"] - b["last60"]),
          "qwin": q_no_regress, "qimp": q_improve,
          "dd": round(x["maxdd"]), "base_dd": round(b["maxdd"]), "trades": x["trades"], "base_trades": b["trades"]}


def _live_sessions(since: datetime, cfg: dict | None = None):
  from . import wfo
  from .config import load_config
  all_s = wfo.load_sessions(cfg or load_config())
  return [s for s in all_s if s.bars and s.bars[0].end >= since]


def _eval_live(c: dict, sim_ai: str, base: dict, rule: dict, sessions: list, since: datetime) -> tuple[int, dict, dict]:
  live = _live_sessions(since, c)
  n_days = len({s.td for s in live})
  if not live:
    return 0, {"pnl": 0.0, "log": []}, {"pnl": 0.0, "log": []}
  if sim_ai == "omega-swing":
    start_td = min(s.td for s in live)
    rb = _sim_one(c, sim_ai, base, sessions, start_td=start_td, record=True)
    rx = _sim_one(c, sim_ai, {**base, **rule}, sessions, start_td=start_td, record=True)
  else:
    rb = _sim_one(c, sim_ai, base, live, record=True)
    rx = _sim_one(c, sim_ai, {**base, **rule}, live, record=True)
  return n_days, rb, rx


def load_state() -> dict:
  st = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
  st.setdefault("trials", {})
  st.setdefault("rejected", [])
  st.setdefault("adopted", [])
  return st


def run(cfg: dict, ledger: Ledger, sessions: list, now: datetime | None = None, adopt: bool = True) -> dict:
  """Weekly step. Returns {"lines": [...LINE text...], "changed": bool}."""
  now = now or datetime.now(TPE)
  c = dict(cfg)
  st = load_state()
  params = json.loads(PARAMS.read_text(encoding="utf-8")) if PARAMS.exists() else {}
  lessons = params.setdefault("lessons", {})
  specs = _specs(lessons)
  days = sorted({s.td for s in sessions})
  pm = bool(cfg.get("protected_not_stop", False))
  revs = weekly_reviews(ledger, now, respect_since=False, protected_mode=pm)
  revs_since = weekly_reviews(ledger, now, respect_since=True, protected_mode=pm)
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
    base_sig = json.loads(json.dumps(base, sort_keys=True))
    trial = st["trials"].get(ai)
    if trial and trial.get("base") is not None and trial.get("base") != base_sig:
      del st["trials"][ai]
      trial = None
    if trial:  # Forward check on live bars recorded since the trial started.
      since = datetime.fromisoformat(trial["since"])
      n_days, rb, rx = _eval_live(c, sim_ai, base, trial["rule"], sessions, since)
      b, x = rb["pnl"], rx["pnl"]
      diff_n = abs(len(rb.get("log", [])) - len(rx.get("log", [])))
      head = f"🧪 {NAMES[ai]} 驗證中「{rule_zh(trial['rule'])}」：{n_days}/{TRIAL_DAYS} 天，套用 {x:+,.0f} vs 不套用 {b:+,.0f}"
      if n_days < TRIAL_DAYS:
        lines.append(head)
        continue
      passed = (x > b and rb.get("log") != rx.get("log")) or (x >= b and diff_n >= 3)
      if not passed and x == b and n_days < TRIAL_DAYS * 2:
        lines.append(head + "（期間尚未觸發差異，延長觀察）")
        continue
      del st["trials"][ai]
      if passed and adopt:
        lessons[ai] = {**lessons.get(ai, {}), **trial["rule"]}
        st["adopted"].append({**trial, "base": base_sig, "ai": ai, "at": now.date().isoformat(), "fwd": [round(x), round(b)]})
        ledger.set_state(f"strategy_since:{ai}", now.isoformat(timespec="seconds"))
        changed = True
        lines.append(head + " → ✅ 正式採用")
      elif passed and not adopt:
        lines.append(head + " → 通過（未設定自動套用）")
      elif x < b:
        st["rejected"].append({**trial, "base": base_sig, "ai": ai, "at": now.date().isoformat(), "fwd": [round(x), round(b)]})
        lines.append(head + " → ❌ 實戰沒有比較好，淘汰")
      else:
        lines.append(head + " → ⚪ 期間未觸發有效差異，結束觀察")
      continue
    tags = recurring(revs_since[ai], min_total=2 if ai == "omega-swing" else 4)
    if not tags:
      continue
    cutoff = (now - timedelta(weeks=RETRY_WEEKS)).date().isoformat()
    tried = [j["rule"] for j in st["rejected"]
             if j["ai"] == ai and j["at"] >= cutoff and (j.get("base") is None or j.get("base") == base_sig)]
    have = lessons.get(ai, {})
    base_m = _metrics(c, sim_ai, base, sessions, days)
    best = None
    for tag in tags:
      if tag not in CANDIDATES:
        continue
      for rule in CANDIDATES[tag].get(ai, CANDIDATES[tag]["*"]):
        if rule in tried or all(have.get(k) == v for k, v in rule.items()):
          continue
        bt = backtest(c, sim_ai, base, rule, sessions, days, base_m)
        txt = (f"一年 {bt['gain']:+,}、近60天 {bt['gain60']:+,}、四季不退步 {bt['qwin']}/4（進步 {bt['qimp']}/4）、"
               f"回撤 {bt['dd']:,}（原 {bt['base_dd']:,}）")
        if bt["ok"] and (best is None or bt["gain"] > best[2]["gain"]):
          best = (tag, rule, bt, txt)
        elif not bt["ok"]:
          lines.append(f"🔍 {NAMES[ai]} 反覆出現「{TAG_ZH[tag]}」→ 試「{rule_zh(rule)}」：{txt} → 回測沒變好，不採用")
    if best:
      tag, rule, bt, txt = best
      eval_since = datetime.fromisoformat("2026-10-19T08:45:00+08:00")
      n_days, rb, rx = _eval_live(c, sim_ai, base, rule, sessions, eval_since)
      b_l, x_l = rb["pnl"], rx["pnl"]
      # The recent live bars are the same bars whose losses suggested the rule (in-sample), so passing them is
      # not enough: a rule is never adopted immediately; it always starts a forward trial on NEW bars.
      if n_days >= TRIAL_DAYS and x_l < b_l:
        st["rejected"].append({"rule": rule, "tag": tag, "base": base_sig, "ai": ai,
                               "at": now.date().isoformat(), "fwd": [round(x_l), round(b_l)]})
        lines.append(f"🔍 {NAMES[ai]} 反覆出現「{TAG_ZH[tag]}」→ 試「{rule_zh(rule)}」：{txt}"
                     f"｜近 {n_days} 天實盤 {x_l:+,.0f} vs {b_l:+,.0f} → ❌ 實盤未變好，不採用")
      else:
        st["trials"][ai] = {"rule": rule, "tag": tag, "base": base_sig, "since": now.isoformat(timespec="seconds"), "bt": bt}
        recent = f"｜近 {n_days} 天實盤 {x_l:+,.0f} vs {b_l:+,.0f}" if n_days else ""
        lines.append(f"🔍 {NAMES[ai]} 反覆出現「{TAG_ZH[tag]}」→ 試「{rule_zh(rule)}」：{txt}{recent}"
                     f" → 回測通過，開始實戰驗證 {TRIAL_DAYS} 天（新行情）才決定是否採用")
  active = {ai: r for ai, r in lessons.items() if r}
  if active:
    lines.append("✅ 目前已採用：" + "；".join(f"{NAMES.get(ai, ai)} {rule_zh(r)}" for ai, r in active.items()))
  STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
  if changed:
    PARAMS.write_text(json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8")
  return {"lines": lines, "changed": changed}


# Rule keys the live Trader._lesson_filter enforces for intraday AIs (see trader.py).
LIVE_RULE_KEYS = ("open_delay", "min_er", "max_vwap_atr", "max_vwap_pts", "max_sl", "rev_cool")


def propose(cfg: dict, ai: str, rule: dict, sessions: list | None = None, now: datetime | None = None) -> dict:
  """Manual-change gate: a hand-written rule goes through the same path as a weekly lesson.

  It must pass the one-year / last-60-day / quarters / drawdown backtest, must not lose on the recent live bars,
  and is then registered as a forward trial (adopted by the Saturday review only after >= TRIAL_DAYS of new bars).
  Returns {"ok": bool, "lines": [...]}; never adopts directly.
  """
  from . import wfo
  now = now or datetime.now(TPE)
  if ai not in AIS or ai == "omega-swing":
    return {"ok": False, "lines": [f"❌ {ai}：只支援當沖 AI（alpha-orb / sigma-vwap / nova-nightorb）"]}
  bad = [k for k in rule if k not in LIVE_RULE_KEYS]
  if bad:
    return {"ok": False, "lines": [f"❌ 不支援的規則欄位 {bad}（即時交易只認得 {list(LIVE_RULE_KEYS)}）"]}
  if not rule or any(isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0 for v in rule.values()):
    return {"ok": False, "lines": [f"❌ 規則值必須是大於 0 的數字：{rule}"]}
  st = load_state()
  if st["trials"].get(ai):
    t = st["trials"][ai]
    return {"ok": False, "lines": [f"⏸ {NAMES[ai]} 已有驗證中的規則「{rule_zh(t['rule'])}」（{t['since'][:10]} 起），一次只驗證一條"]}
  params = json.loads(PARAMS.read_text(encoding="utf-8")) if PARAMS.exists() else {}
  have = params.get("lessons", {}).get(ai, {})
  if all(have.get(k) == v for k, v in rule.items()):
    return {"ok": False, "lines": [f"⚪ {NAMES[ai]}「{rule_zh(rule)}」已經在用了"]}
  sim_ai, base = _specs(params.get("lessons", {}))[ai]
  base_sig = json.loads(json.dumps(base, sort_keys=True))
  cutoff = (now - timedelta(weeks=RETRY_WEEKS)).date().isoformat()
  if any(j["ai"] == ai and j["rule"] == rule and j["at"] >= cutoff and (j.get("base") is None or j.get("base") == base_sig)
         for j in st["rejected"]):
    return {"ok": False, "lines": [f"⏸ {NAMES[ai]}「{rule_zh(rule)}」{RETRY_WEEKS} 週內已被淘汰過，暫不重試"]}
  sessions = sessions if sessions is not None else wfo.load_sessions(cfg)
  days = sorted({s.td for s in sessions})
  base_m = _metrics(dict(cfg), sim_ai, base, sessions, days)
  bt = backtest(dict(cfg), sim_ai, base, rule, sessions, days, base_m)
  exp = (base_m["pnl"] + bt["gain"]) * 0.7
  lines = [f"📝 提案 {NAMES[ai]}「{rule_zh(rule)}」：一年 {bt['gain']:+,}、近60天 {bt['gain60']:+,}、"
           f"四季不退步 {bt['qwin']}/4（進步 {bt['qimp']}/4）、回撤 {bt['dd']:,}（原 {bt['base_dd']:,}）",
           f"   保守預期（一年回測 ×0.7）：{exp:+,.0f}／年"]
  if not bt["ok"]:
    return {"ok": False, "lines": lines + ["   → ❌ 回測沒變好，不進入驗證"]}
  n_days, rb, rx = _eval_live(dict(cfg), sim_ai, base, rule, sessions,
                              datetime.fromisoformat("2026-10-19T08:45:00+08:00"))
  if n_days >= TRIAL_DAYS and rx["pnl"] < rb["pnl"]:
    return {"ok": False, "lines": lines + [f"   → ❌ 近 {n_days} 天實盤 {rx['pnl']:+,.0f} vs {rb['pnl']:+,.0f}，不進入驗證"]}
  st["trials"][ai] = {"rule": rule, "tag": "manual", "base": base_sig,
                      "since": now.isoformat(timespec="seconds"), "bt": bt}
  STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
  return {"ok": True, "lines": lines + [f"   → ✅ 已登記為實戰驗證（{TRIAL_DAYS} 個交易日新行情後，週六檢討自動決定是否採用）"]}
