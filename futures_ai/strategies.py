"""Two AI trading models. Each returns an Entry (side + stop/take-profit distance) or None."""

from __future__ import annotations

from dataclasses import dataclass

from .feed import Bar


@dataclass
class Entry:
  side: str  # 'B' long / 'S' short
  sl_pts: float
  tp_pts: float
  reason: str


def clamp(x: float, lo: float, hi: float) -> float:
  return max(lo, min(hi, x))


def vwap(bars: list[Bar]) -> float:
  pv = sum((b.h + b.l + b.c) / 3 * b.v for b in bars)
  vol = sum(b.v for b in bars)
  return pv / vol if vol else (bars[-1].c if bars else 0.0)


def atr(bars: list[Bar], n: int = 14) -> float:
  recent = bars[-n:]
  trs = []
  for i, b in enumerate(recent):
    prev_c = recent[i - 1].c if i else b.o
    trs.append(max(b.h - b.l, abs(b.h - prev_c), abs(b.l - prev_c)))
  return sum(trs) / len(trs) if trs else 0.0


class OrbBreakout:
  """Alpha: 15-minute opening-range breakout confirmed by VWAP; max 2 entries per session."""

  id = "alpha-orb"
  name = "Alpha 開盤突破 AI"
  style = "開盤 15 分鐘區間突破 + VWAP 確認，順勢追價"
  color = "#38bdf8"
  modes = {"native": "順勢突破", "flip": "假突破反向"}

  def __init__(self, range_minutes: int = 15, max_entries: int = 2, buffer_pts: float = 3):
    self.range_minutes = range_minutes
    self.max_entries = max_entries
    self.buffer = buffer_pts

  def decide(self, bars: list[Bar], entries_this_session: int, bars_since_exit: int | None) -> Entry | None:
    if len(bars) <= self.range_minutes + 1 or entries_this_session >= self.max_entries:
      return None
    if bars_since_exit is not None and bars_since_exit < 3:
      return None
    rng = bars[: self.range_minutes]
    hi, lo = max(b.h for b in rng), min(b.l for b in rng)
    width = hi - lo
    if width > 450:  # Opening range too wide: stop distance would exceed the risk budget.
      return None
    last, prev = bars[-1], bars[-2]
    vw = vwap(bars)
    sl = clamp(width * 0.5, 30, 80)
    tp = sl * 2
    if last.c > hi + self.buffer and prev.c <= hi + self.buffer and last.c > vw:
      return Entry("B", sl, tp, f"突破開盤區間高點 {hi:.0f}（區間 {width:.0f} 點），站上 VWAP {vw:.0f}")
    if last.c < lo - self.buffer and prev.c >= lo - self.buffer and last.c < vw:
      return Entry("S", sl, tp, f"跌破開盤區間低點 {lo:.0f}（區間 {width:.0f} 點），位於 VWAP {vw:.0f} 之下")
    return None


class VwapReversion:
  """Sigma: fades stretched moves back toward VWAP after a reversal bar; max 4 entries per session."""

  id = "sigma-vwap"
  name = "Sigma VWAP 回歸 AI"
  style = "價格偏離 VWAP 過大且出現反轉K棒時逆勢進場，目標回歸均價"
  color = "#a855f7"
  modes = {"native": "均值回歸", "flip": "趨勢延續"}

  def __init__(self, min_bars: int = 20, max_entries: int = 4, stretch_atr: float = 2.5):
    self.min_bars = min_bars
    self.max_entries = max_entries
    self.stretch = stretch_atr

  def decide(self, bars: list[Bar], entries_this_session: int, bars_since_exit: int | None) -> Entry | None:
    if len(bars) < self.min_bars or entries_this_session >= self.max_entries:
      return None
    if bars_since_exit is not None and bars_since_exit < 5:
      return None
    a = max(atr(bars), 5.0)
    vw = vwap(bars)
    last, prev = bars[-1], bars[-2]
    dev = last.c - vw
    sl = clamp(a * 1.5, 25, 70)
    tp = clamp(abs(dev) * 0.8, sl, sl * 2)
    if dev >= self.stretch * a and last.c < last.o and last.c < prev.c:
      return Entry("S", sl, tp, f"高於 VWAP {dev:.0f} 點（{dev / a:.1f}×ATR）且出現長黑反轉，逆勢放空")
    if dev <= -self.stretch * a and last.c > last.o and last.c > prev.c:
      return Entry("B", sl, tp, f"低於 VWAP {-dev:.0f} 點（{-dev / a:.1f}×ATR）且出現長紅反轉，逆勢做多")
    return None


def ema(values: list[float], n: int) -> float:
  k, out = 2 / (n + 1), values[0]
  for v in values[1:]:
    out = v * k + out * (1 - k)
  return out


class AggressiveMomentum:
  """Gamma: aggressive 1-minute momentum scalper. Trades often (built to exercise the automation)."""

  id = "gamma-scalp"
  name = "Gamma 激進動能 AI"
  style = "1 分K 動能當沖：EMA5/13 同向且突破前一根高/低點就進場，小停損快停利，每盤最多 30 次"
  color = "#f97316"
  modes = {"native": "動能追價", "flip": "動能反轉"}

  def __init__(self, sl_pts: float = 15, tp_pts: float = 20, max_entries: int = 30):
    self.sl, self.tp, self.max_entries = sl_pts, tp_pts, max_entries

  def decide(self, bars: list[Bar], entries_this_session: int, bars_since_exit: int | None) -> Entry | None:
    if len(bars) < 14 or entries_this_session >= self.max_entries:
      return None
    if bars_since_exit is not None and bars_since_exit < 1:
      return None
    closes = [b.c for b in bars[-30:]]
    fast, slow = ema(closes, 5), ema(closes, 13)
    last, prev = bars[-1], bars[-2]
    if fast > slow and last.c > prev.h and last.c > last.o:
      return Entry("B", self.sl, self.tp, f"EMA5 {fast:.0f} > EMA13 {slow:.0f}，收盤 {last.c:.0f} 突破前高 {prev.h:.0f}，激進追多")
    if fast < slow and last.c < prev.l and last.c < last.o:
      return Entry("S", self.sl, self.tp, f"EMA5 {fast:.0f} < EMA13 {slow:.0f}，收盤 {last.c:.0f} 跌破前低 {prev.l:.0f}，激進追空")
    return None


def build_strategies() -> list:
  return [OrbBreakout(), VwapReversion(), AggressiveMomentum()]
