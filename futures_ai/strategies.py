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
  trail_pts: float | None = None  # trailing stop distance (None = fixed stop)
  limit_price: float | None = None  # explicit limit entry price (otherwise derived from limit_atr)


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
    prev_c = recent[i - 1].c if i else (bars[-n - 1].c if len(bars) > n else b.o)
    trs.append(max(b.h - b.l, abs(b.h - prev_c), abs(b.l - prev_c)))
  return sum(trs) / len(trs) if trs else 0.0


class OrbBreakout:
  """Alpha: opening-range breakout on 5-minute bars (default first 3 bars = 15 minutes), VWAP-confirmed."""

  id = "alpha-orb"
  name = "Alpha 開盤突破 AI"
  style = "5 分K：開盤前 15 分鐘（3 根）區間突破 + VWAP 確認（區間 40–260 點），浮盈 2 倍停損移至保本+6，每盤最多 2 次"
  color = "#38bdf8"
  modes = {"native": "順勢突破", "flip": "假突破反向"}

  def __init__(self, range_bars: int = 3, max_entries: int = 2, buffer_pts: float = 3,
               sl_frac: float = 0.5, tp_mult: float = 2.0, min_width: float = 40, max_width: float = 260,
               be_r: float | None = 2.0):
    self.range_bars = range_bars
    self.max_entries = max_entries
    self.buffer = buffer_pts
    self.sl_frac, self.tp_mult = sl_frac, tp_mult
    self.min_width, self.max_width = min_width, max_width
    self.be_r = be_r

  def decide(self, bars: list[Bar], entries_this_session: int, bars_since_exit: int | None,
             bars1: list[Bar] | None = None) -> Entry | None:
    from datetime import timedelta as _td
    from .calendar import session_of
    if len(bars) <= self.range_bars or (self.max_entries and entries_this_session >= self.max_entries):
      return None
    _, s_start, _ = session_of(bars[-1].end - _td(seconds=1))
    if s_start is not None and len(bars) >= 2 and (bars[-1].end - bars[-2].end) >= _td(minutes=5):
      r_end = s_start + _td(minutes=5 * self.range_bars)
      if bars[-1].end <= r_end:
        return None
      rng = [b for b in bars if s_start < b.end <= r_end]
      if len(rng) < self.range_bars:
        return None
    else:
      rng = bars[: self.range_bars]
    hi, lo = max(b.h for b in rng), min(b.l for b in rng)
    width = hi - lo
    if width < self.min_width or width > self.max_width:
      return None
    last, prev = bars[-1], bars[-2]
    vw = vwap(bars1 if bars1 else bars)
    sl = clamp(width * self.sl_frac, 30, 120)
    tp = sl * self.tp_mult
    if last.c > hi + self.buffer and prev.c <= hi + self.buffer and last.c > vw:
      return Entry("B", sl, tp, f"5 分K 突破開盤區間高點 {hi:.0f}（區間 {width:.0f} 點），站上 VWAP {vw:.0f}")
    if last.c < lo - self.buffer and prev.c >= lo - self.buffer and last.c < vw:
      return Entry("S", sl, tp, f"5 分K 跌破開盤區間低點 {lo:.0f}（區間 {width:.0f} 點），位於 VWAP {vw:.0f} 之下")
    return None


class VwapReversion:
  """Sigma: fades stretched moves back toward VWAP after a reversal bar; max 4 entries per session."""

  id = "sigma-vwap"
  name = "Sigma VWAP 回歸 AI"
  style = "價格偏離 VWAP 過大且出現反轉K棒時逆勢進場，目標回歸均價；5 分K 強趨勢時不逆勢，停損後須回到均價才再同向進場"
  color = "#a855f7"
  modes = {"native": "均值回歸", "flip": "趨勢延續"}
  block_after_stop = True

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
  style = "1 分K 動能當沖：EMA5/13 同向且突破前一根高/低點就進場；停損依波動調整（至少 20 點），停利 ≥ 1.5 倍停損"
  color = "#f97316"
  modes = {"native": "動能追價", "flip": "動能反轉"}

  def __init__(self, sl_pts: float = 20, tp_pts: float = 30, max_entries: int = 30):
    self.sl, self.tp, self.max_entries = sl_pts, tp_pts, max_entries

  def decide(self, bars: list[Bar], entries_this_session: int, bars_since_exit: int | None) -> Entry | None:
    if len(bars) < 14 or entries_this_session >= self.max_entries:
      return None
    if bars_since_exit is not None and bars_since_exit < 1:
      return None
    closes = [b.c for b in bars[-30:]]
    fast, slow = ema(closes, 5), ema(closes, 13)
    last, prev = bars[-1], bars[-2]
    # Stops scale with 1-minute volatility so normal noise does not knock the trade out;
    # take-profit >= 1.5x stop so a win clearly covers the ~6-point round-trip cost.
    sl = max(self.sl, round(1.2 * atr(bars)))
    tp = max(self.tp, round(sl * 1.5))
    if fast > slow and last.c > prev.h and last.c > last.o:
      return Entry("B", sl, tp, f"EMA5 {fast:.0f} > EMA13 {slow:.0f}，收盤 {last.c:.0f} 突破前高 {prev.h:.0f}，激進追多")
    if fast < slow and last.c < prev.l and last.c < last.o:
      return Entry("S", sl, tp, f"EMA5 {fast:.0f} < EMA13 {slow:.0f}，收盤 {last.c:.0f} 跌破前低 {prev.l:.0f}，激進追空")
    return None


def _trend(bars: list[Bar]):
  from .learning import market_regime  # Local import avoids a module cycle.
  return market_regime(bars)


class TrendPullback:
  """Sigma v2: in a 5-minute trend, buy the dip back to VWAP (sell the rally in a downtrend)."""

  id = "sigma-vwap"  # Same slot/id as before so the account history continues.
  name = "Sigma 趨勢回測 AI"
  style = "5 分K 明確趨勢（ER≥0.30）中，價格回測 VWAP 後收回趨勢方向且逆勢影線 ≤ 25% 順勢進場；浮盈 1.75 倍停損移至保本+6；核心時段＋高波動解鎖早盤/歐盤/極端日盤；每盤最多 2 次"
  color = "#a855f7"
  modes = {"native": "順勢回測", "flip": "順勢回測"}
  allow_flip = False
  block_after_stop = True

  def __init__(self, pull_atr: float = 0.5, sl_atr: float = 1.5, tp_mult: float = 4.0,
               trail_atr: float | None = None, max_entries: int = 2, max_wick: float | None = 0.25,
               bonus_hours: tuple[str, ...] | None = ("day_open", "night_pre_us"),
               bonus_min_atr: float = 35.0, day_min_atr: float | None = 100.0,
               min_er: float | None = 0.30, be_r: float | None = 1.75):
    self.pull, self.sl_atr, self.tp_mult, self.trail_atr = pull_atr, sl_atr, tp_mult, trail_atr
    self.max_entries, self.max_wick = max_entries, max_wick
    self.bonus_hours, self.bonus_min_atr = bonus_hours, bonus_min_atr
    self.day_min_atr, self.min_er, self.be_r = day_min_atr, min_er, be_r

  def decide(self, bars: list[Bar], entries_this_session: int, bars_since_exit: int | None,
             bars1: list[Bar] | None = None) -> Entry | None:
    if len(bars) < 6 or (self.max_entries and entries_this_session >= self.max_entries):
      return None
    regime, er, d = _trend(bars1 if bars1 else bars)
    if regime != "trend" or d == 0:
      return None
    if self.min_er and er < self.min_er:
      return None
    a = max(atr(bars), 5.0)
    vw = vwap(bars1 if bars1 else bars)
    x = bars[-1]
    if getattr(self, "hours", None):
      from .calendar import tod_bucket
      tb = tod_bucket(x.end)
      if tb == "day" and self.day_min_atr and a < self.day_min_atr:
        return None
      if self.bonus_hours and tb in self.bonus_hours and a < self.bonus_min_atr:
        return None
    rng_b = x.h - x.l
    mw = self.max_wick
    sl = clamp(a * self.sl_atr, 20, 150)
    tp = sl * self.tp_mult
    trail = a * self.trail_atr if self.trail_atr else None
    if d > 0 and x.l <= vw + self.pull * a and x.c > vw and x.c > x.o:
      if mw is not None and (rng_b <= 0 or (x.h - x.c) / rng_b > mw):
        return None
      return Entry("B", sl, tp, f"5 分K 多頭趨勢（ER {er:.2f}），回測 VWAP {vw:.0f} 後收紅，順勢做多", trail)
    if d < 0 and x.h >= vw - self.pull * a and x.c < vw and x.c < x.o:
      if mw is not None and (rng_b <= 0 or (x.c - x.l) / rng_b > mw):
        return None
      return Entry("S", sl, tp, f"5 分K 空頭趨勢（ER {er:.2f}），反彈 VWAP {vw:.0f} 後收黑，順勢放空", trail)
    return None


class TrendBreakout:
  """Gamma v2: in a 5-minute trend, enter on a break of the prior N-bar high/low; trailing stop."""

  id = "gamma-scalp"
  name = "Gamma 趨勢突破 AI"
  style = "5 分K 明確趨勢中，突破前 N 根 5 分K 高點（或跌破低點）順勢進場，移動停損讓獲利奔跑"
  color = "#f97316"
  modes = {"native": "趨勢突破", "flip": "趨勢突破"}
  allow_flip = False

  def __init__(self, lookback: int = 12, sl_atr: float = 2.0, trail_atr: float = 3.0, tp_mult: float = 4.0):
    self.n, self.sl_atr, self.trail_atr, self.tp_mult = lookback, sl_atr, trail_atr, tp_mult

  def decide(self, bars: list[Bar], entries_this_session: int, bars_since_exit: int | None) -> Entry | None:
    if len(bars) < self.n + 2:
      return None
    regime, er, d = _trend(bars)
    if regime != "trend":
      return None
    a = max(atr(bars), 5.0)
    window = bars[-self.n - 1:-1]
    hi, lo = max(b.h for b in window), min(b.l for b in window)
    x, prev = bars[-1], bars[-2]
    sl = clamp(a * self.sl_atr, 20, 150)
    if d > 0 and x.c > hi and prev.c <= hi:
      return Entry("B", sl, sl * self.tp_mult, f"5 分K 多頭趨勢（ER {er:.2f}），突破前 {self.n} 根高點 {hi:.0f}，移動停損 {a * self.trail_atr:.0f} 點", a * self.trail_atr)
    if d < 0 and x.c < lo and prev.c >= lo:
      return Entry("S", sl, sl * self.tp_mult, f"5 分K 空頭趨勢（ER {er:.2f}），跌破前 {self.n} 根低點 {lo:.0f}，移動停損 {a * self.trail_atr:.0f} 點", a * self.trail_atr)
    return None


class InverseGamma:
  """Delta: whenever Gamma wants to go long, go short at that point (and vice versa).
  Its own stop / take-profit are set to make money in the opposite direction."""

  id = "delta-inverse"
  name = "Delta 反向 Gamma AI"
  style = "跟 Gamma 唱反調：Gamma 想做多就放空、想放空就做多（賭突破失敗），停損停利依反向方向設定"
  color = "#eab308"
  modes = {"native": "反向 Gamma", "flip": "反向 Gamma"}
  allow_flip = False
  skip_trend_filter = True  # By design it trades against the breakout direction.

  def __init__(self, gamma_params: dict | None = None, sl_scale: float = 1.0, tp_mult: float = 3.0):
    self.gamma = TrendBreakout(**(gamma_params or {}))
    self.sl_scale, self.tp_mult = sl_scale, tp_mult

  def decide(self, bars: list[Bar], entries_this_session: int, bars_since_exit: int | None) -> Entry | None:
    g = self.gamma.decide(bars, 0, None)
    if g is None:
      return None
    side = "S" if g.side == "B" else "B"
    sl = g.sl_pts * self.sl_scale
    return Entry(side, sl, sl * self.tp_mult,
                 f"Gamma 想{'做多' if g.side == 'B' else '放空'}，Delta 反向{'放空' if side == 'S' else '做多'}｜{g.reason.split('，移動停損')[0]}")


class InverseOf:
  """Contrarian twin that mirrors the base AI's ACTUAL trades: at the moment the base AI really
  opens a long, this AI opens a short at that point (and vice versa). Stop distance follows the
  base trade (x sl_scale); take-profit = stop x tp_mult, set to make money in the opposite direction.
  If the base AI does not trade (already in a position, filtered, paused...), neither does this AI."""

  allow_flip = False
  skip_trend_filter = True

  def __init__(self, base, id: str, name: str, color: str, sl_scale: float = 1.0, tp_mult: float = 1.5):
    self.base = base
    self.follow = base.id  # live AI whose real entries are mirrored
    self.id, self.name, self.color = id, name, color
    who = base.name.split(" ")[0]
    self.style = f"鏡像反向 {who}：{who} 實際做多的同一根 K 棒就放空、實際放空就做多；停損停利依反向方向設定"
    self.modes = {"native": f"反向 {who}", "flip": f"反向 {who}"}
    self.sl_scale, self.tp_mult = sl_scale, tp_mult
    self._mirrored: str | None = None

  def decide(self, bars: list[Bar], entries_this_session: int, bars_since_exit: int | None) -> Entry | None:
    from .trader import ENTRY_BUS  # Local import avoids a module cycle.
    ev = ENTRY_BUS.get(self.follow)
    if not ev or not bars or ev.get("bar_end") != bars[-1].end.isoformat() or self._mirrored == ev["bar_end"]:
      return None
    self._mirrored = ev["bar_end"]
    side = "S" if ev["side"] == "B" else "B"
    sl = ev["sl_pts"] * self.sl_scale
    who = self.base.name.split(" ")[0]
    return Entry(side, sl, sl * self.tp_mult,
                 f"{who} 剛{'做多' if ev['side'] == 'B' else '放空'}成交 {ev['price']:.0f}，鏡像反向{'放空' if side == 'S' else '做多'}（限價 {ev['price']:.0f}）",
                 None, ev["price"])


class FixedBracket:
  """Uses a base AI's entry logic (same direction) with a fixed take-profit / stop in points."""

  allow_flip = False
  fixed_bracket = True  # Not scaled by learning multipliers, not capped by remaining-session reach.

  def __init__(self, base, id: str, name: str, color: str, tp_pts: float = 100, sl_pts: float = 50):
    self.base = base
    self.id, self.name, self.color = id, name, color
    self.tp_pts, self.sl_pts = tp_pts, sl_pts
    self.block_after_stop = getattr(base, "block_after_stop", False)
    who = base.name.split(" ")[0]
    self.style = f"用 {who} 的進場訊號（同方向），固定獲利 {tp_pts:.0f} 點停利、虧損 {sl_pts:.0f} 點停損"
    self.modes = {"native": f"{who} 進場＋固定停利停損", "flip": f"{who} 進場＋固定停利停損"}

  def decide(self, bars: list[Bar], entries_this_session: int, bars_since_exit: int | None) -> Entry | None:
    g = self.base.decide(bars, entries_this_session, bars_since_exit)
    if g is None:
      return None
    return Entry(g.side, self.sl_pts, self.tp_pts,
                 f"{g.reason}｜固定停利 {self.tp_pts:.0f} 點／停損 {self.sl_pts:.0f} 點")


class NightOpenBreakout:
  """Nova (replaces Gamma): night-session opening-range breakout. Range = 15:00-15:30 (six 5-min bars);
  trades a VWAP-confirmed breakout during the next 600 minutes (until 01:30; extended from 120 after the
  one-year check: 10 of 12 20-day blocks profitable vs 7 of 12); stop = range width (30-150 pts),
  take-profit = 3x stop; limit entry at the signal close."""

  id = "nova-nightorb"
  name = "Nova 夜盤開盤突破 AI"
  style = "夜盤 15:00–15:30 開盤區間突破（區間 ≥ 30 點、實體 ≥ 0.25×ATR、未達 2.5 倍區間耗盡、避開美股盤前冷清段），浮盈 1.75 倍停損移至保本+6，停損＝區間寬度，停利 3 倍，限價進場"
  color = "#facc15"  # Yellow: distinct from Alpha (sky blue)
  modes = {"native": "夜盤開盤突破", "flip": "夜盤開盤突破"}
  allow_flip = False
  learn_scale = False  # Keep the validated stop / take-profit geometry.

  def __init__(self, range_min: int = 30, window: int = 600, sl_frac: float = 1.0, tp_mult: float = 3.0,
               min_width: float = 30, reach_r: float | None = 2.5, min_body_atr: float | None = 0.25,
               dst_skip: tuple[int, int] | None = (150, 150), be_r: float | None = 1.75):
    self.range_min, self.window, self.sl_frac, self.tp_mult = range_min, window, sl_frac, tp_mult
    self.min_width, self.reach_r, self.min_body_atr, self.dst_skip, self.be_r = (
        min_width, reach_r, min_body_atr, dst_skip, be_r)
    self.max_entries = None
    self.limit_atr, self.limit_min = 0.0, 5

  def decide(self, bars: list[Bar], entries_this_session: int, bars_since_exit: int | None,
             bars1: list[Bar] | None = None) -> Entry | None:
    from datetime import datetime as _dt, time as _t, timedelta as _td
    from .calendar import us_open_at
    if len(bars) < 2:
      return None
    x, prev = bars[-1], bars[-2]
    t = x.end
    if _t(5, 1) <= t.time() < _t(15, 0):
      return None  # Night session only.
    d0 = t.date() if t.time() >= _t(15, 0) else t.date() - _td(days=1)
    anchor = _dt.combine(d0, _t(15, 0), t.tzinfo)
    r_end = anchor + _td(minutes=self.range_min)
    if not (r_end < t <= r_end + _td(minutes=self.window)):
      return None
    sw = getattr(self, "skip_window", None)  # ("HH:MM", "HH:MM"): left-open, right-closed skip window.
    if sw and sw[0] < t.strftime("%H:%M") <= sw[1]:
      return None
    ds = getattr(self, "dst_skip", None)
    if ds and not getattr(self, "hours", None):
      if anchor + _td(minutes=ds[0]) < t <= us_open_at(t) - _td(minutes=ds[1]):
        return None
    rng = [b for b in bars if anchor < b.end <= r_end]
    if len(rng) < self.range_min // 5 - 1:
      return None
    hi, lo = max(b.h for b in rng), min(b.l for b in rng)
    width = hi - lo
    if width > 450 or width < getattr(self, "min_width", 0):
      return None
    sl = clamp(width * self.sl_frac, 30, 150)
    a = max(atr(bars), 5.0)
    rr = getattr(self, "reach_r", None)
    after_rng = [b for b in bars[:-1] if b.end > r_end] if rr else []
    vw = vwap(bars1 if bars1 else bars)
    mb = getattr(self, "min_body_atr", None)
    if x.c > hi + 3 and prev.c <= hi + 3 and x.c > vw and (not mb or x.c - x.o >= mb * a):
      if rr and after_rng and max(b.h for b in after_rng) >= hi + rr * width:
        return None
      return Entry("B", sl, sl * self.tp_mult, f"突破夜盤開盤區間高點 {hi:.0f}（區間 {width:.0f} 點），站上 VWAP {vw:.0f}")
    if x.c < lo - 3 and prev.c >= lo - 3 and x.c < vw and (not mb or x.o - x.c >= mb * a):
      if rr and after_rng and min(b.l for b in after_rng) <= lo - rr * width:
        return None
      return Entry("S", sl, sl * self.tp_mult, f"跌破夜盤開盤區間低點 {lo:.0f}（區間 {width:.0f} 點），位於 VWAP {vw:.0f} 之下")
    return None


class SwingTrend:
  """Omega: swing trend-following on 60-minute bars; holds overnight; ATR trailing stop; limit entry.

  Long when the 60-min close breaks the prior N-bar high and EMA20 > EMA60 (short = mirror).
  Exit only by the trailing stop (atr_k x ATR14 of 60-min bars). After a forced roll exit it
  re-enters on the first new 60-min bar if the EMA trend is unchanged. Validated on one year of
  data: rolling 60-day practice -> 20-day exam, 7 of 9 exam windows profitable."""

  id = "omega-swing"
  name = "Omega 波段順勢 AI"
  color = "#f97316"
  modes = {"native": "波段順勢", "flip": "波段順勢"}
  allow_flip = False
  skip_trend_filter = True
  fixed_bracket = True
  hold_overnight = True
  limit_min = 60

  def __init__(self, lookback: int = 10, atr_k: float = 4.0, direction: str = "both", limit_atr: float = 0.3,
               tp_atr: float = 10.0, min_er: float | None = 0.20, be_r: float | None = 0.7,
               max_ext_atr: float | None = 3.5, max_sl_pts: float | None = 1200):
    self.lookback, self.atr_k, self.direction, self.limit_atr_60 = lookback, atr_k, direction, limit_atr
    self.tp_atr, self.min_er, self.be_r = tp_atr, min_er, be_r
    self.max_ext_atr, self.max_sl_pts = max_ext_atr, max_sl_pts
    self.max_entries = None
    self.style = (f"60 分K 波段：突破前 {lookback} 根高點且 EMA20>EMA60、ER10≥0.20、乖離≤{max_ext_atr or 3.5:g}×ATR 做多（{'多空都做' if direction == 'both' else '只做多'}），"
                  f"可留倉過夜，浮盈 0.7 倍停損移至保本+6、{atr_k:g} 倍 ATR 移動停損（上限 {max_sl_pts or 1200:.0f} 點）、{tp_atr:g} 倍 ATR 停利，限價進場")
    self.h60: list[Bar] = []  # completed 60-minute bars across sessions (set by the engine)
    self._last_decided = None
    self.reentry_side: str | None = None

  def current_atr(self) -> float | None:
    h = self.h60
    if len(h) < 15:
      return None
    trs = []
    for i in range(len(h) - 14, len(h)):
      pc = h[i - 1].c
      trs.append(max(h[i].h - h[i].l, abs(h[i].h - pc), abs(h[i].l - pc)))
    return sum(trs) / len(trs)

  def decide(self, bars: list[Bar], entries_this_session: int, bars_since_exit: int | None,
             bars1: list[Bar] | None = None) -> Entry | None:
    h = self.h60
    if len(h) < max(60, self.lookback + 1) or h[-1].end == self._last_decided:
      return None
    if bars and bars[-1].end != h[-1].end:
      return None  # Act strictly on the exact 5-minute bar that completes the 60-minute bar.
    self._last_decided = h[-1].end
    closes = [b.c for b in h[-200:]]
    ef, es = ema_series(closes, 20)[-1], ema_series(closes, 60)[-1]
    a = self.current_atr() or 0.0
    hi = max(b.h for b in h[-self.lookback - 1:-1])
    lo = min(b.l for b in h[-self.lookback - 1:-1])
    c = h[-1].c
    side, why = None, ""
    if self.reentry_side:
      if self.reentry_side == "B" and ef > es:
        side, why = "B", "換倉後趨勢仍向上，重新做多"
      elif self.reentry_side == "S" and ef < es and self.direction == "both":
        side, why = "S", "換倉後趨勢仍向下，重新放空"
      else:
        self.reentry_side = None
    if side is None:
      if getattr(self, "min_er", None):
        cl10 = closes[-11:]
        path = sum(abs(cl10[k] - cl10[k - 1]) for k in range(1, len(cl10)))
        if (abs(cl10[-1] - cl10[0]) / path if path else 0.0) < self.min_er:
          return None
      if getattr(self, "max_ext_atr", None) and a > 0 and abs(c - ef) > self.max_ext_atr * a:
        return None
      if c > hi and ef > es:
        side, why = "B", f"60 分K 收盤 {c:.0f} 突破前 {self.lookback} 根高點 {hi:.0f}，EMA20 {ef:.0f} > EMA60 {es:.0f}"
      elif c < lo and ef < es and self.direction == "both":
        side, why = "S", f"60 分K 收盤 {c:.0f} 跌破前 {self.lookback} 根低點 {lo:.0f}，EMA20 {ef:.0f} < EMA60 {es:.0f}"
    if side is None:
      return None
    d = 1 if side == "B" else -1
    trail = round(self.atr_k * a)
    if getattr(self, "max_sl_pts", None):
      trail = min(trail, round(self.max_sl_pts))
    tp = round(a * self.tp_atr)  # 10 x ATR(60m) ~ 2,000-2,500 pts: realistic, and best in the 1-year test.
    return Entry(side, trail, tp, f"{why}｜移動停損 {trail} 點、停利 {tp} 點", trail, round(c - d * self.limit_atr_60 * a))


def ema_series(values: list[float], n: int) -> list[float]:
  k, out = 2 / (n + 1), []
  for v in values:
    out.append(v if not out else v * k + out[-1] * (1 - k))
  return out


def build_strategies(params: dict | None = None) -> list:
  """params: optional {ai_id: kwargs} chosen by walk-forward training (data/strategy_params.json)."""
  if params is None:
    import json
    from .config import ROOT
    f = ROOT / "data" / "strategy_params.json"
    params = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
  alpha = OrbBreakout(**params.get("alpha-orb", {}))
  sigma = TrendPullback(**params.get("sigma-vwap", {}))
  omega = SwingTrend(**params.get("omega-swing", {}))
  # Removed at the user's request: Epsilon / Zeta (inverse Alpha / Sigma, 2026-10-19), Cloudy's strategy
  # (2026-10-19), Delta (inverse Gamma; one-year backtest -212,029) and the hidden Gamma that drove it (2026-10-22).
  # 方向 B: limit-order entries (validated on one year of data).
  alpha.limit_atr, alpha.limit_min = 0.0, 5
  sigma.limit_atr, sigma.limit_min = 0.2, 10
  # Time-of-day filters (validated: rolling 40-day practice -> 20-day exam).
  alpha.hours = ("us_open",)
  # Sigma: core hours ("us_open", "night_open") always active + high-volatility ("day_open", "night_pre_us")
  # unlocked when 5-min ATR >= 35 pts + extreme day ("day") unlocked when 5-min ATR >= 100 pts; avoids late-night chop.
  sigma.hours = ("day_open", "day", "night_open", "night_pre_us", "us_open")
  nova = NightOpenBreakout(**params.get("nova-nightorb", {}))
  # Nova US (US-open-only Nova) runs as a background shadow since 2026-10-22: see build_nova_us().
  lessons = params.get("lessons", {})  # Rules adopted from the weekly losing-trade review (lessons.py).
  for st in (alpha, sigma, nova, omega):
    st.rules = dict(lessons.get(st.id, {}))
    for rk, rv in st.rules.items():
      if hasattr(st, rk):
        setattr(st, rk, rv)
  return [alpha, sigma, nova, omega]


def build_nova_us(params: dict | None = None) -> "NightOpenBreakout":
  """Nova US: inherits live Nova's rules/params/lessons, entries only in the first hour after US cash open,
  with breakeven stop at 1.75R (+6 pts)."""
  import copy
  base = [s for s in build_strategies(params) if s.id == "nova-nightorb"][0]
  nova_us = copy.deepcopy(base)
  for k, v in (params or {}).get("nova-us", {}).items():
    setattr(nova_us, k, v)
  nova_us.id, nova_us.name, nova_us.color = "nova-us", "Nova 美股開盤（影子）", "#818cf8"
  nova_us.style = "Nova 同一套夜盤開盤區間突破，但只在美股開盤後 1 小時內進場；浮盈 1.75 倍停損移至保本+6，停利 3 倍，限價進場"
  nova_us.hours = ("us_open",)
  nova_us.skip_window = None
  nova_us.dst_skip = None
  nova_us.be_r = 1.75
  return nova_us
