"""Weekly self-review per AI (user request 2026-11-27): each AI reviews what it did wrong this week.

Not "every stop-loss is a mistake": a rule-based strategy loses on most trades by design (Nova breaks even at
~26% wins, backtest ~36%). The review therefore answers, per AI:
 1. Is it doing worse than expected?  Last 20 trading days (non-breakeven trades) vs the one-year backtest win
    rate of the current version, one-sided binomial test; n < 10 = not enough data; p < 5% = "不在優勢區".
 2. Which loss pattern is over-represented this week?  Each losing trade gets one primary tag (priority below);
    a tag is a "problem" only when its share of this week's losses is clearly above its share in the backtest.
    The rest are "照規則的正常停損".
 3. Design trade-offs (not mistakes): night-filter avoided / missed (old- shadows), Omega limit orders that never
    filled while the move ran (excluding signals filled by a later order within 24 h).
 4. What the system did (lesson trials of this AI) and facts to watch next week.
Read-only: never changes parameters, lessons or trading.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta

from .lessons import AIS, NAMES, OPEN_MIN, MarketCtx, _session

STOP_KINDS = ("SL", "trail_stop")
BE_PTS = 10  # |points| <= 10: breakeven exit (counted as 打平, not as a win or a loss)
PRIORITY = ("limit_breakout", "gap_stop", "whipsaw", "night_chop", "noise_stop", "consec_sl", "reversal",
            "early_open")
TAG_ZH = {"limit_breakout": "追漲跌停價的突破", "gap_stop": "開盤跳空越過停損", "whipsaw": "同一盤兩邊都假突破",
          "night_chop": "夜盤洗盤仍進場", "noise_stop": "停損後 30 分鐘內回到進場價", "consec_sl": "同一盤連續停損",
          "reversal": "停損後馬上反手", "early_open": "開盤急著進場"}
EXCESS_ABS = 0.25  # share this week - share in backtest
MIN_N = 10


def binom_cdf(k: int, n: int, p: float) -> float:
  return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))


def locked_prices(sessions) -> dict:
  """{session start iso: set of prices with >= 10 locked 1-minute bars (h == l) in that session}."""
  out = {}
  for s in sessions:
    if not s.bars:
      continue
    cnt = {}
    for b in s.bars:
      if b.h == b.l:
        cnt[b.h] = cnt.get(b.h, 0) + 1
    start = _session(s.bars[0].end - timedelta(seconds=1))[2].isoformat()
    out[start] = {px for px, n in cnt.items() if n >= 10}
  return out


def tag_trades(trades: list[dict], ai: str, ctx: MarketCtx, locked: dict, sess_starts: list) -> list[dict]:
  """trades: dicts with side, e (entry dt), x (exit dt), ep, xp, net, kind, reason (optional). Adds 'tag'."""
  by_sess: dict = {}
  for t in trades:
    by_sess.setdefault(_session(t["e"])[:2], []).append(t)
  for sk, ts in by_sess.items():
    ts.sort(key=lambda t: t["e"])
    sides_sl = {t["side"] for t in ts if t["kind"] in STOP_KINDS and t["net"] < 0}
    sl_seen, last_sl = 0, None
    for t in ts:
      tags = []
      if t["net"] < 0:
        m = re.search(r"(?:突破|跌破)前 \d+ 根(?:高|低)點 (\d+)", t.get("reason") or "")
        if m:
          prev = [st for st in sess_starts if st < _session(t["e"])[2]]
          if prev and float(m.group(1)) in locked.get(prev[-1].isoformat(), set()):
            tags.append("limit_breakout")
        x_open = _session(t["x"] - timedelta(seconds=1))[2]
        if ai == "omega-swing" and t["kind"] in ("SL", "trail_stop") and t["x"] - x_open <= timedelta(minutes=2) \
            and _session(t["x"] - timedelta(seconds=1))[:2] != sk:
          tags.append("gap_stop")
        if ai != "omega-swing" and len(sides_sl) == 2 and t["kind"] in STOP_KINDS:
          tags.append("whipsaw")
        nm = ctx.night_mean(t["e"])
        if nm is not None and nm < 0.40:
          tags.append("night_chop")
        if t["kind"] in ("SL", "trail_stop") and t.get("ep") and ctx.back_to_entry(t["side"], t["ep"], t["x"]):
          tags.append("noise_stop")
        if t["kind"] in STOP_KINDS and sl_seen >= 1:
          tags.append("consec_sl")
        if last_sl and last_sl[0] != t["side"] and t["e"] < last_sl[1] + timedelta(minutes=30):
          tags.append("reversal")
        if ai in OPEN_MIN and t["e"] < _session(t["e"])[2] + timedelta(minutes=OPEN_MIN[ai]):
          tags.append("early_open")
      t["tags"] = tags
      t["tag"] = next((p for p in PRIORITY if p in tags), None)
      if t["net"] > 0:
        sl_seen, last_sl = 0, None
      elif t["kind"] in ("SL", "trail_stop") and t["net"] < 0:
        sl_seen += 1
        last_sl = (t["side"], t["x"])
  return trades


def _pts(t) -> float:
  return ((t["xp"] - t["ep"]) * (1 if t["side"] == "B" else -1)) if t.get("ep") and t.get("xp") else t["net"] / 10


def win_split(trades) -> tuple[int, int, int]:
  w = sum(1 for t in trades if _pts(t) > BE_PTS)
  b = sum(1 for t in trades if -BE_PTS <= _pts(t) <= BE_PTS)
  return w, b, len(trades) - w - b


def backtest_trades(cfg, ai, sessions) -> list[dict]:
  import json
  from . import slowtrain, wfo
  from .config import ROOT
  from .lessons import _specs
  from .swing import simulate_swing
  params = json.loads((ROOT / "data" / "strategy_params.json").read_text(encoding="utf-8")) \
      if (ROOT / "data" / "strategy_params.json").exists() else {}
  if ai == "omega-swing":
    om = {**slowtrain.OMEGA_BASE, **slowtrain.LIVE_DEFAULTS["omega-swing"], **params.get("omega-swing", {})}
    return [dict(e=e, x=x, side=s, ep=ep, xp=xp, net=n, kind=why) for e, x, s, ep, xp, n, why in
            simulate_swing(cfg, om, sessions, record=True)["log"]]
  sim_ai, p = _specs(params.get("lessons", {}))[ai]
  r = wfo.simulate(cfg, sim_ai, p, sessions, record=True)
  return [dict(e=e, x=x, side=s, ep=px[0], xp=px[1], net=n, kind=k)
          for (e, x, s, n, k), px in zip(r["log"], r.get("log_px") or [(None, None)] * len(r["log"])) if e]


def build(cfg: dict, ledger, sessions: list, lesson_lines: list[str] | None = None, now: datetime | None = None,
          shadow=None) -> tuple[list[str], dict]:
  """Returns (lines for the full weekly report, {ai: one LINE line})."""
  from .calendar import TPE, is_business_day
  now = now or datetime.now(TPE)
  week0 = now - timedelta(days=7)
  d, days = now.date(), []
  while len(days) < 20:
    if is_business_day(d):
      days.append(d)
    d -= timedelta(days=1)
  t20 = days[-1].isoformat()
  last = sessions[-1].bars[-1].end if sessions and sessions[-1].bars else now
  sess_y = [s for s in sessions if s.bars and s.bars[0].end >= last - timedelta(days=365)]
  ctx = MarketCtx(sessions)
  locked = locked_prices(sessions)
  sess_starts = sorted({_session(s.bars[0].end - timedelta(seconds=1))[2] for s in sessions if s.bars})
  if shadow is None:
    from .shadows import shadow_ledger
    shadow = shadow_ledger()
  out = ["", "🪞 各 AI 本週自我復盤（不是每筆停損都算錯：先看是否比預期差，再看哪種型態異常偏多）"]
  brief = {}
  for ai in AIS:
    name = NAMES[ai]
    rows = ledger.rows("SELECT * FROM trades WHERE ai=? AND entry_ts>=? ORDER BY entry_ts", (ai, t20))
    live = [dict(e=datetime.fromisoformat(r["entry_ts"]), x=datetime.fromisoformat(r["exit_ts"]), side=r["side"],
                 ep=r["entry_price"], xp=r["exit_price"], net=r["pnl_twd"], kind=r["exit_kind"],
                 reason=r.get("entry_reason") or "") for r in rows
            if r["exit_ts"] and datetime.fromisoformat(r["exit_ts"]) <= now]
    tag_trades(live, ai, ctx, locked, sess_starts)
    week = [t for t in live if t["x"] >= week0]
    bt = tag_trades(backtest_trades(cfg, ai, sess_y), ai, ctx, locked, sess_starts)
    bw, bb, bl = win_split(bt)
    p0 = bw / (bw + bl) if bw + bl else 0
    w20, _b20, l20 = win_split(live)
    n20 = w20 + l20
    if n20 < MIN_N:
      exp_txt, exp_short = f"近 20 個交易日 {n20} 筆（不含打平），樣本不足，不判斷", "樣本不足"
    else:
      pv = binom_cdf(w20, n20, p0)
      bad = pv < 0.05
      exp_txt = (f"近 20 個交易日勝 {w20}/{n20}（{w20 / n20:.0%}），回測 {p0:.0%}，p={pv:.1%}"
                 + (" → 明顯比預期差：本週期行情不在我的優勢區" if bad else " → 在正常波動範圍內"))
      exp_short = "差（不在優勢區）" if bad else "正常"
    ww, wb, wl = win_split(week)
    wpnl = sum(t["net"] for t in week)
    losses = [t for t in week if t["net"] < 0 and _pts(t) < -BE_PTS]
    b_losses = [t for t in bt if t["net"] < 0 and _pts(t) < -BE_PTS]
    problems = []
    for tag in PRIORITY:
      wk = [t for t in losses if t["tag"] == tag]
      if tag == "gap_stop":
        continue  # Overnight gaps are a structural cost of swing holding (one-year gap net +38.7k): trade-offs.
      rare = tag == "limit_breakout"  # Rare, specific pattern (n=1 so far): reported as "to research".
      if len(wk) < (1 if rare else 2):
        continue
      share_w = len(wk) / len(losses)
      share_b = sum(1 for t in b_losses if t["tag"] == tag) / len(b_losses) if b_losses else 0
      if share_w >= share_b + EXCESS_ABS:
        worst = min(wk, key=lambda t: t["net"])
        problems.append((tag, len(wk), sum(t["net"] for t in wk), share_w, share_b, worst))
    normal = len(losses) - sum(p[1] for p in problems)
    out.append(f"【{name}】本週 {len(week)} 筆 {wpnl:+,.0f}（賺 {ww}／打平 {wb}／賠 {wl}）")
    out.append(f"  比預期：{exp_txt}")
    if problems:
      from .shadows import SWITCH_TS
    for tag, n, amt, sw, sb, worst in problems:
        note = ""
        if tag == "night_chop" and ai in ("sigma-vwap", "nova-nightorb") and \
            all(t["e"].isoformat() < SWITCH_TS for t in losses if t["tag"] == tag):
          note = "（都在 11/18 夜盤過濾上線前，已處理）"
        if tag == "noise_stop" and ai in ("sigma-vwap", "nova-nightorb"):
          note = "（放寬停損已測過，一年更差）"
        head = "待研究（樣本少）" if tag == "limit_breakout" else "做錯在哪"
        out.append(f"  {head}：{TAG_ZH[tag]} {n} 筆 {amt:+,.0f}（本週虧損占 {sw:.0%}，回測平常 {sb:.0%}）{note}；"
                   f"例：{worst['e']:%m/%d %H:%M} 進場 {'多' if worst['side'] == 'B' else '空'} {worst['net']:+,.0f}")
    out.append(f"  照規則的正常停損：{normal} 筆" if losses else "  本週沒有虧損單")
    trade = ""
    if ai in ("alpha-orb", "sigma-vwap", "nova-nightorb"):
      from .shadows import SWITCH_TS
      cmp0 = max(week0.isoformat(), SWITCH_TS)  # Old shadows exist only since the 11/18 switch.
      old = shadow.rows("SELECT entry_ts, points, pnl_twd FROM trades WHERE ai=? AND entry_ts>=? AND exit_ts>=? "
                        "AND exit_ts<=?", (f"old-{ai}", cmp0, week0.isoformat(), now.isoformat()))
      newk = {t["e"].isoformat()[:16]: t["net"] for t in week if t["e"].isoformat() >= cmp0}
      oldk = {r["entry_ts"][:16]: (r["points"], r["pnl_twd"]) for r in old}
      new_tot, old_tot = sum(newk.values()), sum(v[1] for v in oldk.values())
      only = [v for k, v in oldk.items() if k not in newk and abs(v[0]) > BE_PTS]  # breakeven exits excluded
      av, ms = sum(v[1] for v in only if v[1] < 0), sum(v[1] for v in only if v[1] > 0)
      same = sum(newk[k] - oldk[k][1] for k in newk if k in oldk and newk[k] != oldk[k][1])
      if old or newk:
        why = "夜盤過濾" if ai != "alpha-orb" else "只有舊版做的單"
        parts = [f"{why}：避開 {av:+,.0f}／錯過 {ms:+,.0f}"] if only else []
        be = sum(v[1] for k, v in oldk.items() if k not in newk and abs(v[0]) <= BE_PTS)
        if be:
          parts.append(f"舊版打平單 {be:+,.0f} 另計")
        if same:
          parts.append(f"同一筆結果不同（{'停損上限 160' if ai == 'alpha-orb' else '規則差異'}）{same:+,.0f}")
        since_txt = "，11/18 新版上線後" if cmp0 > week0.isoformat() else ""
        out.append(f"  設計取捨（新版 vs 舊版影子{since_txt}）：新版 {new_tot:+,.0f} vs 舊版 {old_tot:+,.0f}（{new_tot - old_tot:+,.0f}）"
                   + ("；" + "；".join(parts) if parts else ""))
        trade = f"｜新版比舊版 {new_tot - old_tot:+,.0f}"
    if ai == "omega-swing":
      gaps = [t for t in losses if "gap_stop" in t["tags"]]
      if gaps:
        out.append(f"  設計取捨：隔夜跳空停損 {len(gaps)} 筆 {sum(t['net'] for t in gaps):+,.0f}，屬於順勢波段的結構成本"
                   f"（一年回測跳空淨 +38.7k，保護方式都更差）")
      missed = missed_runners(ledger, sessions, week0)
      if missed:
        out.append(f"  設計取捨（限價等回檔沒成交、之後順向走超過 2×ATR，24 小時內無補單）：{len(missed)} 筆，約 "
                   f"{sum(m for m in missed):+,.0f} 點")
        trade = f"｜取捨：錯過 {len(missed)} 筆"
    acts = [ln for ln in (lesson_lines or []) if ln.startswith(("🔍", "🧪", "🧹")) and f" {name} " in f" {ln[2:]}"]
    started = [ln for ln in acts if "開始實戰驗證" in ln or "驗證中" in ln or "正式採用" in ln]
    if acts:
      out.append(f"  系統的處理：本週測試 {len(acts)} 條候選規則，" +
                 (f"試行或採用 {len(started)} 條" if started else "都沒有通過，維持現狀"))
    watch = watch_line(ai, ledger)
    if watch:
      out.append(f"  下週注意：{watch}")
    main = "、".join(f"{TAG_ZH[p[0]]} {p[1]} 筆" + ("（待研究）" if p[0] == "limit_breakout" else "")
                     for p in problems) if problems else ("正常停損" if losses else "無虧損")
    brief[ai] = f"{name}：本週 {len(week)} 筆 {wpnl:+,.0f}｜比預期：{exp_short}｜主要問題：{main}{trade}"
  return out, brief


def watch_line(ai: str, ledger) -> str:
  if ai in ("sigma-vwap", "nova-nightorb"):
    st = ledger.get_state("night_regime") or {}
    effs = st.get("effs") or []
    if effs:
      m = sum(effs[-5:]) / len(effs[-5:])
      return f"夜盤盤勢最近 5 夜平均 {m:.2f}（門檻 0.40），{'目前擋下夜盤' if m < 0.40 else '目前可以做夜盤'}"
  if ai == "omega-swing":
    p = ledger.get_state("pos:omega-swing")
    return f"目前{'持有' + ('多單' if p['side'] == 'B' else '空單') + ' @ ' + format(p['entry_price'], '.0f') if p else '空手'}"
  return ""


def missed_runners(ledger, sessions, since: datetime) -> list[float]:
  """Omega limit entries cancelled since `since`, not followed by a filled entry on the same side within 24 h,
  where price then moved >= 2 x ATR60 in the signal direction within 24 h. Returns the points moved."""
  bars = sorted({b.end: b for s in sessions for b in s.bars}.values(), key=lambda b: b.end)
  rows = ledger.rows("SELECT ts, side, trigger_price, status FROM orders WHERE ai='omega-swing' AND kind='ENTRY' "
                     "AND order_type='LMT' AND ts>=? ORDER BY id", (since.isoformat(),))
  out = []
  for i, r in enumerate(rows):
    if r["status"] != "CANCELLED":
      continue
    t0 = datetime.fromisoformat(r["ts"])
    later = [q for q in rows[i + 1:] if q["status"] == "FILLED" and q["side"] == r["side"]
             and datetime.fromisoformat(q["ts"]) <= t0 + timedelta(hours=24)]
    if later:
      continue
    win = [b for b in bars if t0 < b.end <= t0 + timedelta(hours=24)]
    if not win:
      continue
    d = 1 if r["side"] == "B" else -1
    ref = r["trigger_price"]
    best = max((b.h - ref) if d == 1 else (ref - b.l) for b in win)
    sig = ledger.rows("SELECT message FROM events WHERE ai='omega-swing' AND level='SIGNAL' AND ts<=? "
                      "ORDER BY id DESC LIMIT 1", (r["ts"],))
    m = re.search(r"停利：\d+（(\d+) 點）", sig[0]["message"]) if sig else None
    atr = float(m.group(1)) / 10 if m else 0  # Omega take-profit = 10 x ATR60 at the signal.
    if atr and best >= 2 * atr:
      out.append(best)
  return out
