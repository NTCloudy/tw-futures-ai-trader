"""Experience learning for each AI: post-trade review, regime-aware strategy switching, adaptive stops.

Transparent, bounded rules (not a black box):
1. Regime: Kaufman efficiency ratio over the last 20 one-minute bars -> 趨勢盤 (>= 0.35) / 盤整盤.
2. Each AI has two modes: its native strategy and a flipped variant (e.g. breakout -> fade the false
   breakout). Per regime and mode it keeps an exponentially weighted PnL. When the native mode keeps
   losing in a regime, the AI switches to the other mode for that regime; if both lose, it stops
   trading that regime until the daily decay lets it try again.
3. Every closed trade is reviewed using MFE/MAE (best/worst excursion while holding):
   - stopped out fast with almost no favorable move -> false signal / stop too tight in chop
   - reached most of the take-profit, then reversed to the stop -> take profit earlier
   - large slippage after the trigger -> noted
   Stop / take-profit multipliers adapt within [0.8, 1.5] / [0.6, 1.5].
4. Three losses in a row -> 15-minute cool-down.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta

from .feed import Bar
from .strategies import Entry

REGIME_ZH = {"trend": "趨勢盤", "chop": "盤整盤"}
ER_TREND = 0.35
SL_RANGE = (0.8, 1.5)
TP_RANGE = (0.6, 1.5)
BLOCK_EW = -100.0

EMPTY = {
    "stats": {r: {m: {"n": 0, "wins": 0, "ew": 0.0, "pnl": 0.0} for m in ("native", "flip")} for r in REGIME_ZH},
    "sl_mult": 1.0, "tp_mult": 1.0, "loss_streak": 0, "pause_until": None, "decay_day": None, "journal": [],
}


def efficiency_ratio(bars: list[Bar], n: int = 20) -> float:
  closes = [b.c for b in bars[-(n + 1):]]
  if len(closes) < 3:
    return 0.0
  path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
  return abs(closes[-1] - closes[0]) / path if path else 0.0


def to_5min(bars: list[Bar]) -> list[Bar]:
  out: list[Bar] = []
  for b in bars:
    key = b.end.replace(second=0, microsecond=0)
    bucket_end = key + timedelta(minutes=(5 - key.minute % 5) % 5)
    if out and out[-1].end == bucket_end:
      x = out[-1]
      out[-1] = Bar(bucket_end, x.o, max(x.h, b.h), min(x.l, b.l), b.c, x.v + b.v)
    else:
      out.append(Bar(bucket_end, b.o, b.h, b.l, b.c, b.v))
  return out


def market_regime(bars: list[Bar]) -> tuple[str, float, int]:
  """(regime, efficiency ratio, trend direction +1/-1/0) measured on 5-minute bars (last hour).

  趨勢盤 when the 5-minute efficiency ratio is high, or price keeps printing new 1-hour extremes.
  Falls back to 1-minute bars early in the session."""
  b5 = to_5min(bars)
  if len(b5) >= 6:
    window = b5[-13:]
    er = efficiency_ratio(window, n=len(window) - 1)
    closes = [x.c for x in window]
    direction = 1 if closes[-1] > closes[0] else -1 if closes[-1] < closes[0] else 0
    hi, lo = max(x.h for x in window[:-2]), min(x.l for x in window[:-2])
    new_extreme = (direction > 0 and max(x.h for x in window[-2:]) >= hi) or (direction < 0 and min(x.l for x in window[-2:]) <= lo)
    trend = er >= 0.30 or (new_extreme and er >= 0.18)
  else:
    er = efficiency_ratio(bars)
    closes = [x.c for x in bars[-21:]]
    direction = 1 if closes and closes[-1] > closes[0] else -1 if closes and closes[-1] < closes[0] else 0
    trend = er >= ER_TREND
  return ("trend" if trend else "chop"), er, direction


def clamp(x, lo, hi):
  return max(lo, min(hi, x))


class Brain:

  def __init__(self, ledger, strategy):
    self.ledger = ledger
    self.ai = strategy.id
    self.modes = getattr(strategy, "modes", {"native": "原策略", "flip": "反向策略"})
    self.key = f"brain:{self.ai}"
    self.state = ledger.get_state(self.key) or copy.deepcopy(EMPTY)

  def save(self) -> None:
    self.ledger.set_state(self.key, self.state)

  # ---------- regime & mode ----------
  @staticmethod
  def regime(bars: list[Bar]) -> tuple[str, float]:
    name, er, _ = market_regime(bars)
    return name, er

  def daily_decay(self, trading_day: str) -> None:
    if self.state.get("decay_day") == trading_day:
      return
    if self.state.get("decay_day"):  # New day: halve old evidence so the AI can re-test blocked modes.
      for r in self.state["stats"].values():
        for m in r.values():
          m["ew"] *= 0.5
    self.state["decay_day"] = trading_day
    self.save()

  def choose_mode(self, regime: str) -> tuple[str | None, str]:
    s = self.state["stats"][regime]
    nat, alt = s["native"], s["flip"]
    rz = REGIME_ZH[regime]
    if nat["n"] < 3:
      return "native", f"{rz}樣本不足（{nat['n']} 筆），先用{self.modes['native']}"
    if nat["ew"] < 0 and alt["n"] < 2:
      return "flip", f"{self.modes['native']}在{rz}近期虧損（加權 {nat['ew']:+.0f}），改試{self.modes['flip']}"
    if nat["ew"] <= BLOCK_EW and alt["ew"] <= BLOCK_EW and alt["n"] >= 2:
      return None, f"{rz}兩種策略都在虧（{nat['ew']:+.0f} / {alt['ew']:+.0f}），暫停在{rz}進場"
    if alt["ew"] > nat["ew"]:
      return "flip", f"{rz}中{self.modes['flip']}表現較好（{alt['ew']:+.0f} vs {nat['ew']:+.0f}）"
    return "native", f"{rz}中{self.modes['native']}表現較好（{nat['ew']:+.0f} vs {alt['ew']:+.0f}）"

  def paused(self, now: datetime) -> bool:
    pu = self.state.get("pause_until")
    return bool(pu) and now < datetime.fromisoformat(pu)

  def adjust(self, sig: Entry, bars: list[Bar]) -> tuple[Entry | None, dict]:
    regime, er = self.regime(bars)
    mode, why = self.choose_mode(regime)
    ctx = {"regime": regime, "er": round(er, 2), "mode": mode, "why": why}
    if mode is None:
      return None, ctx
    side = sig.side if mode == "native" else ("S" if sig.side == "B" else "B")
    sl = round(sig.sl_pts * self.state["sl_mult"])
    tp = round(sig.tp_pts * self.state["tp_mult"])
    reason = f"[{REGIME_ZH[regime]} ER {er:.2f}｜{self.modes[mode]}] {sig.reason}"
    if mode == "flip":
      reason += "（學習後反向操作）"
    return Entry(side, sl, tp, reason), ctx

  # ---------- review ----------
  def review(self, now: datetime, pos: dict, exit_price: float, exit_kind: str, pnl: float) -> dict:
    regime, mode = pos.get("regime", "chop"), pos.get("mode", "native")
    sl_dist = abs(pos["entry_price"] - pos["sl"])
    tp_dist = abs(pos["tp"] - pos["entry_price"])
    mfe, mae = pos.get("mfe", 0.0), pos.get("mae", 0.0)
    try:
      held_min = (now - datetime.fromisoformat(pos["entry_ts"])).total_seconds() / 60
    except (ValueError, TypeError):
      held_min = 0.0
    trigger = pos["sl"] if exit_kind == "SL" else pos["tp"] if exit_kind == "TP" else None
    slip = abs(exit_price - trigger) if trigger is not None else 0.0
    st = self.state
    b = st["stats"][regime][mode]
    b["n"] += 1
    b["pnl"] += pnl
    b["ew"] = pnl if b["n"] == 1 else 0.6 * b["ew"] + 0.4 * pnl
    adjust: list[str] = []
    if pnl > 0:
      b["wins"] += 1
      st["loss_streak"] = 0
      diag = f"判斷正確，{'停利' if exit_kind == 'TP' else '出場'}獲利（持有 {held_min:.0f} 分鐘，最大浮盈 {mfe:.0f} 點）"
      if exit_kind == "TP" and st["tp_mult"] < 1.0:
        st["tp_mult"] = clamp(st["tp_mult"] * 1.05, *TP_RANGE)
        adjust.append(f"停利倍數回升至 ×{st['tp_mult']:.2f}")
      if st["sl_mult"] > 1.0:
        st["sl_mult"] = clamp(st["sl_mult"] * 0.97, *SL_RANGE)
        adjust.append(f"停損倍數回落至 ×{st['sl_mult']:.2f}")
    else:
      st["loss_streak"] += 1
      if exit_kind == "SL" and held_min <= 2 and mfe < 0.25 * sl_dist:
        diag = f"進場後立刻反向（{held_min:.1f} 分鐘就停損，最大浮盈只有 {mfe:.0f} 點），{REGIME_ZH[regime]}中的假訊號"
        if regime == "chop":
          st["sl_mult"] = clamp(st["sl_mult"] * 1.1, *SL_RANGE)
          adjust.append(f"盤整盤停損太緊容易被洗，停損放寬至 ×{st['sl_mult']:.2f}")
      elif exit_kind == "SL" and mfe >= 0.6 * tp_dist:
        diag = f"曾浮盈 {mfe:.0f} 點（停利需 {tp_dist:.0f} 點）卻回吐到停損，停利設太遠"
        st["tp_mult"] = clamp(st["tp_mult"] * 0.9, *TP_RANGE)
        adjust.append(f"下次提早停利，停利縮為 ×{st['tp_mult']:.2f}")
      elif exit_kind == "SL":
        diag = f"走勢與判斷相反（最大浮盈 {mfe:.0f} 點、最大浮虧 {mae:.0f} 點），{REGIME_ZH[regime]}中{self.modes[mode]}失效"
      elif exit_kind == "SESSION_END":
        diag = "收盤前仍未達停利，強制平倉小賠"
      else:
        diag = f"{exit_kind} 出場虧損"
      if st["loss_streak"] >= 3:
        st["pause_until"] = (now + timedelta(minutes=15)).isoformat()
        adjust.append(f"連虧 {st['loss_streak']} 筆，冷靜 15 分鐘")
        st["loss_streak"] = 0
    if slip >= 3:
      diag += f"；觸發後滑價 {slip:.0f} 點"
    nxt, why = self.choose_mode(regime)
    adjust.append(f"下次{REGIME_ZH[regime]}：{self.modes[nxt] if nxt else '暫停進場'}（{why}）")
    entry = {"ts": now.isoformat(timespec="seconds"), "ai": self.ai, "pnl": round(pnl), "side": pos["side"],
             "regime": regime, "mode": mode, "exit_kind": exit_kind, "diagnosis": diag, "adjustments": adjust}
    st["journal"] = ([entry] + st["journal"])[:60]
    self.save()
    return entry

  def summary(self) -> dict:
    st = self.state
    choices = {}
    for r in REGIME_ZH:
      m, why = self.choose_mode(r)
      choices[r] = {"mode": m, "label": self.modes[m] if m else "暫停", "why": why}
    pre = st.get("pretrained")
    return {"pretrained": {k: pre[k] for k in ("days", "range", "trades", "at")} if pre else None,
            "sl_mult": st["sl_mult"], "tp_mult": st["tp_mult"], "loss_streak": st["loss_streak"],
            "pause_until": st["pause_until"], "stats": st["stats"], "choices": choices, "modes": self.modes}
