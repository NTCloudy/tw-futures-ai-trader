"""Per-AI trading state machine with bracket (OCO) 觸價單.

FLAT --entry signal--> market entry --fill--> IN_POSITION + TAKE(停利觸價) + STOP(停損觸價)
IN_POSITION --either trigger fires--> cancel ALL working orders --> market exit --fill--> FLAT
Also: daily-loss lock, flatten before session close, flatten on contract roll, kill switch.
"""

from __future__ import annotations

from datetime import datetime

from .feed import Bar
from .learning import Brain, market_regime
from .strategies import vwap

KIND_ZH = {"ENTRY": "進場", "TP": "停利觸發", "SL": "停損觸發", "SESSION_END": "收盤前平倉",
           "RISK": "每日虧損上限平倉", "KILL": "緊急停止平倉", "ROLL": "換倉平倉"}


def tp_cap(sl_pts: float, tp_pts: float, bars: list, minutes_to_close: float, reason: str) -> tuple[float, str]:
  """Caps the take-profit at the distance price can plausibly travel before the session ends
  (5-minute ATR x sqrt(remaining 5-minute bars)), but never below 1.5x the stop."""
  from .strategies import atr as atr_fn
  remaining = max(1.0, minutes_to_close / 5)
  cap = max(round(sl_pts * 1.5), round(atr_fn(bars) * remaining ** 0.5))
  if tp_pts > cap:
    return cap, f"{reason}（停利上限 {cap:.0f} 點：依收盤前剩餘時間估算可走距離）"
  return tp_pts, reason


def opposite(side: str) -> str:
  return "S" if side == "B" else "B"


class AiTrader:

  def __init__(self, cfg: dict, strategy, broker, ledger):
    self.cfg = cfg
    self.s = strategy
    self.ai = strategy.id
    self.broker = broker
    self.ledger = ledger
    self.pv = cfg["point_value"]
    self.qty = cfg["qty"]
    self.pending: dict[str, dict] = {}  # broker_order_id -> {"oid", "kind", "entry"}
    self.learning = bool(cfg.get("learning_mode"))
    self.brain = Brain(ledger, strategy, learning=self.learning)

  # ---------- persisted state ----------
  @property
  def pos(self) -> dict | None:
    return self.ledger.get_state(f"pos:{self.ai}")

  @pos.setter
  def pos(self, value: dict | None) -> None:
    self.ledger.set_state(f"pos:{self.ai}", value)

  def locked_day(self) -> str | None:
    return self.ledger.get_state(f"lock:{self.ai}")

  def entries(self, session_key: str) -> int:
    return self.ledger.get_state(f"cnt:{self.ai}:{session_key}", 0)

  # ---------- money ----------
  def side_cost(self, price: float, qty: int) -> float:
    return self.cfg["commission_per_side_twd"] * qty + round(price * self.pv * qty * self.cfg["tax_rate"])

  def unrealized(self, last: float) -> float:
    p = self.pos
    if not p or not last:
      return 0.0
    d = 1 if p["side"] == "B" else -1
    return (last - p["entry_price"]) * d * self.pv * p["qty"] - p["entry_cost"] - self.side_cost(last, p["qty"])

  def today_pnl(self, trading_day: str, last: float) -> float:
    return self.ledger.realized(self.ai, trading_day) + self.unrealized(last)

  # ---------- order plumbing ----------
  def _send_market(self, ts: datetime, td: str, code: str, kind: str, side: str, quote: dict,
                   note: str, entry: dict | None = None) -> None:
    if self.pending:
      return  # One in-flight market order at a time.
    oid = self.ledger.add_order(ai=self.ai, ts=ts.isoformat(timespec="seconds"), trading_day=td, contract=code,
                                kind=kind, order_type="MKT", side=side, qty=self.qty, status="SUBMITTED",
                                broker=self.broker.name, note=note)
    try:
      boid = self.broker.market(code, side, self.qty, quote)
    except Exception as exc:
      self.ledger.update_order(oid, ts.isoformat(timespec="seconds"), status="REJECTED", note=f"{note}｜{exc}")
      self.ledger.log(f"{KIND_ZH.get(kind, kind)} 委託失敗：{exc}", self.ai, "ERROR", ts)
      return
    self.ledger.update_order(oid, ts.isoformat(timespec="seconds"), broker_order_id=boid)
    self.pending[boid] = {"oid": oid, "kind": kind, "entry": entry, "td": td, "code": code}

  def _cancel_working(self, ts: datetime, code: str, reason: str) -> None:
    """Cancels every unfilled order of this AI (ledger triggers + broker-side resting orders)."""
    for o in self.ledger.working_orders(self.ai):
      if o["order_type"] in ("STOP", "TAKE"):
        self.ledger.update_order(o["id"], ts.isoformat(timespec="seconds"), status="CANCELLED", note=reason)
    try:
      self.broker.cancel_all(code)
    except Exception as exc:
      self.ledger.log(f"券商刪單失敗：{exc}", self.ai, "ERROR", ts)

  def process_fills(self, ts: datetime) -> None:
    for f in self.broker.poll_fills():
      info = self.pending.pop(f["broker_order_id"], None)
      if info is None:
        self.ledger.log(f"收到未知委託成交 {f}", self.ai, "WARN", ts)
        continue
      t = ts.isoformat(timespec="seconds")
      self.ledger.update_order(info["oid"], t, status="FILLED", fill_price=f["price"])
      if info["kind"] == "ENTRY":
        self._open(ts, info, f["price"])
      else:
        self._close(ts, info, f["price"])

  def _open(self, ts: datetime, info: dict, price: float) -> None:
    e = info["entry"]
    side = e["side"]
    d = 1 if side == "B" else -1
    tick = self.cfg["tick"]
    sl = round((price - d * e["sl_pts"]) / tick) * tick  # Trigger prices must sit on the tick grid.
    tp = round((price + d * e["tp_pts"]) / tick) * tick
    t = ts.isoformat(timespec="seconds")
    common = dict(ai=self.ai, ts=t, trading_day=info["td"], contract=info["code"], side=opposite(side),
                  qty=self.qty, status="WORKING", broker=self.broker.name)
    tp_oid = self.ledger.add_order(kind="TP", order_type="TAKE", trigger_price=tp, note=f"停利觸價 {tp:.0f}", **common)
    sl_oid = self.ledger.add_order(kind="SL", order_type="STOP", trigger_price=sl, note=f"停損觸價 {sl:.0f}", **common)
    self.pos = {"side": side, "qty": self.qty, "entry_price": price, "entry_ts": t, "contract": info["code"],
                "trading_day": info["td"], "session_key": e["session_key"], "sl": sl, "tp": tp,
                "sl_oid": sl_oid, "tp_oid": tp_oid, "entry_cost": self.side_cost(price, self.qty),
                "reason": e["reason"], "regime": e.get("regime", "chop"), "mode": e.get("mode", "native"),
                "mfe": 0.0, "mae": 0.0, "trail": e.get("trail_pts")}
    self.ledger.log(f"{'做多' if side == 'B' else '放空'}成交 {price:.0f}，同時掛停利觸價 {tp:.0f}／停損觸價 {sl:.0f}"
                    f"｜{e['reason']}", self.ai, "TRADE", ts)

  def _close(self, ts: datetime, info: dict, price: float) -> None:
    p = self.pos
    if not p:
      return
    d = 1 if p["side"] == "B" else -1
    points = (price - p["entry_price"]) * d
    gross = points * self.pv * p["qty"]
    cost = p["entry_cost"] + self.side_cost(price, p["qty"])
    self.ledger.add_trade(ai=self.ai, trading_day=p["trading_day"], contract=p["contract"], side=p["side"],
                          qty=p["qty"], entry_ts=p["entry_ts"], entry_price=p["entry_price"],
                          exit_ts=ts.isoformat(timespec="seconds"), exit_price=price, points=points,
                          gross_twd=gross, cost_twd=cost, pnl_twd=gross - cost, exit_kind=info["kind"],
                          entry_reason=p["reason"])
    self.pos = None
    self.ledger.set_state(f"last_exit:{self.ai}", ts.isoformat())
    self._cancel_working(ts, info["code"], "部位已平倉，刪除剩餘委託")
    self.ledger.log(f"{KIND_ZH.get(info['kind'], info['kind'])}平倉 {price:.0f}，{points:+.0f} 點，"
                    f"淨損益 NT$ {gross - cost:+,.0f}，等待下次進場", self.ai, "TRADE", ts)
    if info["kind"] == "SL" and getattr(self.s, "block_after_stop", False):
      self.ledger.set_state(f"block:{self.ai}", {"side": p["side"], "ts": ts.isoformat()})
    lesson = self.brain.review(ts, p, price, info["kind"], gross - cost)
    self.ledger.log(f"🧠 檢討：{lesson['diagnosis']}｜{'；'.join(lesson['adjustments'])}", self.ai, "LEARN", ts)

  # ---------- decisions ----------
  def flatten(self, ts: datetime, code: str, quote: dict, kind: str) -> None:
    p = self.pos
    if not p or self.pending:
      return
    self._cancel_working(ts, code, KIND_ZH[kind])
    self._send_market(ts, p["trading_day"], p["contract"], kind, opposite(p["side"]), quote, KIND_ZH[kind])

  def on_price(self, ts: datetime, td: str, code: str, quote: dict, minutes_to_close: float, kill: bool) -> None:
    self.process_fills(ts)
    p = self.pos
    last = quote["last"]
    hi, lo = max(last, quote.get("hi") or last), min(last, quote.get("lo") or last)
    if p and not self.pending:
      d = 1 if p["side"] == "B" else -1
      fav = (hi - p["entry_price"]) * d if d == 1 else (p["entry_price"] - lo)
      adv = (p["entry_price"] - lo) if d == 1 else (hi - p["entry_price"])
      if fav > p.get("mfe", 0) or adv > p.get("mae", 0):  # Track best / worst excursion for the review.
        p["mfe"], p["mae"] = max(p.get("mfe", 0), fav), max(p.get("mae", 0), adv)
        self.pos = p
      sl_hit = lo <= p["sl"] if p["side"] == "B" else hi >= p["sl"]
      tp_hit = hi >= p["tp"] if p["side"] == "B" else lo <= p["tp"]
      if kill:
        self.flatten(ts, code, quote, "KILL")
      elif sl_hit:  # If both were touched within one poll, assume the stop came first (conservative).
        self._fire(ts, quote, "SL", p["sl_oid"])
      elif tp_hit:
        self._fire(ts, quote, "TP", p["tp_oid"])
      elif minutes_to_close <= self.cfg["flatten_minutes_before_close"]:
        self.flatten(ts, code, quote, "SESSION_END")
      elif p.get("trail"):  # Ratchet the trailing stop (after the stop check, conservative).
        tick = self.cfg["tick"]
        best = p["entry_price"] + p["mfe"] if p["side"] == "B" else p["entry_price"] - p["mfe"]
        new_sl = round((best - p["trail"] if p["side"] == "B" else best + p["trail"]) / tick) * tick
        if (p["side"] == "B" and new_sl > p["sl"]) or (p["side"] == "S" and new_sl < p["sl"]):
          p["sl"] = new_sl
          self.pos = p
          self.ledger.update_order(p["sl_oid"], ts.isoformat(timespec="seconds"), trigger_price=new_sl,
                                   note=f"移動停損上調至 {new_sl:.0f}" if p["side"] == "B" else f"移動停損下調至 {new_sl:.0f}")
    if (self.cfg.get("enforce_daily_loss", True) and not self.learning and self.locked_day() != td
        and self.today_pnl(td, last) <= -self.cfg["max_daily_loss_twd"]):
      self.ledger.set_state(f"lock:{self.ai}", td)
      self.ledger.log(f"今日損益已達 -NT$ {self.cfg['max_daily_loss_twd']:,}，停止今日交易", self.ai, "WARN", ts)
      if self.pos:
        self.flatten(ts, code, quote, "RISK")
    self.process_fills(ts)

  def _fire(self, ts: datetime, quote: dict, kind: str, trigger_oid: int) -> None:
    p = self.pos
    t = ts.isoformat(timespec="seconds")
    self.ledger.update_order(trigger_oid, t, status="TRIGGERED", note=f"{KIND_ZH[kind]}（成交價 {quote['last']:.0f}）")
    self._cancel_working(ts, p["contract"], f"{KIND_ZH[kind]}，刪除其他未成交委託")
    # The trigger fires the instant the price touches it. Between two 3-second polls the price may
    # already have moved on, so the paper fill is anchored at the trigger: take-profit fills at the
    # trigger (minus slippage); a stop fills at the trigger or worse if the market gapped through it.
    trig = p["tp"] if kind == "TP" else p["sl"]
    q = dict(quote)
    if p["side"] == "B":  # Exit is a sell.
      q["bid"] = trig if kind == "TP" else min(trig, quote.get("bid") or quote["last"])
    else:
      q["ask"] = trig if kind == "TP" else max(trig, quote.get("ask") or quote["last"])
    self._send_market(ts, p["trading_day"], p["contract"], kind, opposite(p["side"]), q, KIND_ZH[kind])

  def _filter(self, sig, bars: list[Bar]) -> str | None:
    """Universal guards applied after learning. Returns a skip reason or None."""
    regime, er, direction = market_regime(bars)
    against = (sig.side == "B" and direction < 0) or (sig.side == "S" and direction > 0)
    if regime == "trend" and against and not getattr(self.s, "skip_trend_filter", False):
      return f"5 分K 強{'多' if direction > 0 else '空'}趨勢（ER {er:.2f}），不逆勢{'做多' if sig.side == 'B' else '放空'}"
    block = self.ledger.get_state(f"block:{self.ai}")
    if block and block["side"] == sig.side:
      vw = vwap(bars)
      touched = any(b.end.isoformat() > block["ts"] and b.l <= vw <= b.h for b in bars)
      if not touched:
        return f"上一筆{'多' if sig.side == 'B' else '空'}單停損，價格尚未回到均價 {vw:.0f}，暫不同向進場"
      self.ledger.set_state(f"block:{self.ai}", None)
    return None

  def on_bar(self, ts: datetime, td: str, code: str, session_key: str, bars: list[Bar], quote: dict,
             minutes_to_close: float, fresh: bool, kill: bool) -> None:
    if self.pos or self.pending or kill or not fresh:
      return
    locked = self.cfg.get("enforce_daily_loss", True) and not self.learning and self.locked_day() == td
    no_entry = self.cfg["flatten_minutes_before_close"] + 1 if self.learning else self.cfg["no_entry_minutes_before_close"]
    if locked or minutes_to_close <= no_entry:
      return
    last_exit = self.ledger.get_state(f"last_exit:{self.ai}")
    since = None
    if last_exit:
      le = datetime.fromisoformat(last_exit)
      if bars and le >= bars[0].end:
        since = sum(1 for b in bars if b.end > le)
    self.brain.daily_decay(td)
    if not self.learning:
      if self.brain.paused(ts) or self.entries(session_key) >= self.cfg["max_entries_per_session"]:
        return
    # Learning mode: no per-session caps or re-entry cool-downs inside the strategies either.
    raw = self.s.decide(bars, 0 if self.learning else self.entries(session_key), None if self.learning else since)
    if raw is None:
      return
    sig, ctx = self.brain.adjust(raw, bars)
    why = ctx["why"] if sig is None else self._filter(sig, bars)
    if why:
      self.ledger.set_state(f"skip:{self.ai}", {"ts": ts.isoformat(timespec="seconds"), "why": why})
      return
    self.ledger.set_state(f"cnt:{self.ai}:{session_key}", self.entries(session_key) + 1)
    tp_pts, reason = tp_cap(sig.sl_pts, sig.tp_pts, bars, minutes_to_close, sig.reason)
    entry = {"side": sig.side, "sl_pts": sig.sl_pts, "tp_pts": tp_pts, "reason": reason,
             "trail_pts": getattr(sig, "trail_pts", None),
             "session_key": session_key, "regime": ctx["regime"], "mode": ctx["mode"]}
    self._send_market(ts, td, code, "ENTRY", sig.side, quote, reason, entry)
    self.process_fills(ts)
