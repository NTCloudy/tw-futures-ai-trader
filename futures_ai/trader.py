"""Per-AI trading state machine with bracket (OCO) 觸價單.

FLAT --entry signal--> market entry --fill--> IN_POSITION + TAKE(停利觸價) + STOP(停損觸價)
IN_POSITION --either trigger fires--> cancel ALL working orders --> market exit --fill--> FLAT
Also: daily-loss lock, flatten before session close, flatten on contract roll, kill switch.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .feed import Bar
from .learning import Brain, market_regime
from .strategies import vwap

# Actual entries of each AI in this process, so contrarian AIs can mirror real trades (not raw signals).
ENTRY_BUS: dict[str, dict] = {}
# Market risk from the news radar (set by the engine): normal / elevated / high / positive, + FOMC window.
RISK_STATE: dict = {"mode": "normal", "fomc_window": False}

KIND_ZH = {"ENTRY": "進場", "TP": "停利觸發", "SL": "停損觸發", "SESSION_END": "收盤前平倉",
           "RISK": "每日虧損上限平倉", "KILL": "緊急停止平倉", "ROLL": "換倉平倉",
           "RETIRE": "AI 下架平倉", "STOP_TIME": "試跑結束時間平倉"}
BE_OFFSET_PTS = 6  # Breakeven offset (+6 pts) to cover round-trip commission, tax and slippage.
PROTECT_MAX_LOSS_PTS = 30
PROTECTED_KEEP = 300


def is_protected_exit(side: str, entry: float, stop_at_exit: float, exit_price: float) -> bool:
  """Protected stop exit: the stop had been moved to the entry price or beyond (breakeven / trailing) and the
  fill stayed within PROTECT_MAX_LOSS_PTS of entry. Such an exit loses only costs + slippage; in protected
  mode it is not counted as a stop-loss (max_sl, rev_cool, Sigma's stop-block, weekly loss tags). Shared by the
  live trader and the simulator (wfo) so both classify identically."""
  d = 1 if side == "B" else -1
  return d * (stop_at_exit - entry) >= 0 and (exit_price - entry) * d >= -PROTECT_MAX_LOSS_PTS


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
    # Protected-exit mode (see is_protected_exit): off for live AIs until the user approves; on for the
    # "prot-" shadow twins of the 10-trading-day comparison.
    pm = getattr(strategy, "protected_not_stop", None)
    self.protected_mode = bool(cfg.get("protected_not_stop", False) if pm is None else pm)
    self.brain = Brain(ledger, strategy, learning=self.learning)
    if not cfg.get("learn_per_trade", True):
      # Parameters change at most once a week (weekly job), never after a single trade: no stop /
      # take-profit rescaling and no switching to the reversed strategy. Reviews are still written.
      self.brain.fixed, self.brain.allow_flip, self.brain.frozen = True, False, True
    if hasattr(self.s, "reentry_side"):
      saved_re = self.ledger.get_state(f"reentry:{self.ai}")
      if saved_re:
        self.s.reentry_side = saved_re
    self._reconcile_startup_orders()

  def _reconcile_startup_orders(self) -> None:
    """Cancels orphaned WORKING/SUBMITTED entry orders left in the ledger before a restart."""
    from .calendar import now_tpe
    p = self.pos
    keep_ids = {p.get("sl_oid"), p.get("tp_oid")} if p else set()
    now_iso = now_tpe().isoformat(timespec="seconds")
    for o in self.ledger.working_orders(self.ai):
      if o["id"] in keep_ids:
        continue
      boid = o.get("broker_order_id")
      if boid:
        try:
          self.broker.cancel(str(boid))
        except Exception as exc:
          self.ledger.log(f"啟動清理殘留委託 {boid} 失敗：{exc}", self.ai, "ERROR")
      self.ledger.update_order(o["id"], now_iso, status="CANCELLED", note="程式啟動清理未完成掛單")

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
    self.pending[boid] = {"oid": oid, "kind": kind, "entry": entry, "td": td, "code": code,
                          "sent": ts.isoformat()}

  def _send_limit(self, ts: datetime, td: str, code: str, side: str, price: float, minutes: float,
                  note: str, entry: dict) -> None:
    """Entry by resting limit order (方向 B: no slippage). Cancelled if not filled in `minutes`."""
    if self.pending:
      return
    t = ts.isoformat(timespec="seconds")
    oid = self.ledger.add_order(ai=self.ai, ts=t, trading_day=td, contract=code, kind="ENTRY", order_type="LMT",
                                side=side, qty=self.qty, trigger_price=price, status="WORKING", broker=self.broker.name,
                                note=f"限價 {price:.0f}（{minutes:.0f} 分鐘內有效）｜{note}")
    try:
      boid = self.broker.limit(code, side, self.qty, price)
    except Exception as exc:
      self.ledger.update_order(oid, t, status="REJECTED", note=f"{note}｜{exc}")
      self.ledger.log(f"限價進場委託失敗：{exc}", self.ai, "ERROR", ts)
      return
    self.ledger.update_order(oid, t, broker_order_id=boid)
    anchor_ts = datetime.fromisoformat(entry["bar_end"]) if entry.get("bar_end") else ts
    self.pending[boid] = {"oid": oid, "kind": "ENTRY", "entry": entry, "td": td, "code": code,
                          "expires": (anchor_ts + timedelta(minutes=minutes)).isoformat(), "limit": True}
    if hasattr(self.s, "reentry_side") and self.ledger.get_state(f"reentry:{self.ai}"):
      self.ledger.set_state(f"reentry:{self.ai}", None)
    d = 1 if side == "B" else -1
    self.ledger.log(f"📣 訊號：{'做多' if side == 'B' else '放空'}｜進場（限價）：{price:.0f}（{minutes:.0f} 分鐘內有效）"
                    f"｜停損：{price - d * entry['sl_pts']:.0f}（{entry['sl_pts']:.0f} 點）"
                    f"｜停利：{price + d * entry['tp_pts']:.0f}（{entry['tp_pts']:.0f} 點）｜原因：{note}", self.ai, "SIGNAL", ts)

  def _expire_limits(self, ts: datetime, force: str | None = None) -> None:
    for boid, info in list(self.pending.items()):
      if info.get("limit") and (force or ts.isoformat() >= info["expires"]):
        try:
          self.broker.cancel(boid)
        except Exception as exc:
          self.ledger.log(f"刪除限價單失敗：{exc}", self.ai, "ERROR", ts)
          continue
        self.ledger.update_order(info["oid"], ts.isoformat(timespec="seconds"), status="CANCELLED",
                                 note=force or "限價單逾時未成交，已刪除")
        e = info.get("entry") or {}
        self.ledger.log(f"❌ 限價單未成交，已取消（{force or '逾時'}）｜原訊號：{'做多' if e.get('side') == 'B' else '放空'}"
                        f"｜請一併刪除你的掛單", self.ai, "INFO", ts)
        del self.pending[boid]

  def _expire_markets(self, ts: datetime) -> None:
    """Reconciles a market (IOC) order with no fill report after `market_timeout_seconds`."""
    limit = timedelta(seconds=self.cfg.get("market_timeout_seconds", 20))
    for boid, info in list(self.pending.items()):
      if not info.get("limit") and info.get("sent") and ts - datetime.fromisoformat(info["sent"]) > limit:
        if hasattr(self.broker, "reconcile_order") and self.broker.reconcile_order(boid):
          self.process_fills(ts)
          continue
        if self.broker.name != "local_paper":
          self.ledger.update_order(info["oid"], ts.isoformat(timespec="seconds"), status="REJECTED",
                                   note="市價單逾時且對帳無成交回報，啟動緊急停止")
          self.ledger.set_state("kill_switch", {"ts": ts.isoformat(timespec="seconds"),
                                                "reason": f"{self.ai} 市價單 {boid} 逾時未收到成交回報"})
          self.ledger.log(f"⛔ {KIND_ZH.get(info['kind'], info['kind'])} 市價單逾時未收到永豐回報，啟動緊急停止（禁止自動重送）",
                          self.ai, "KILL", ts)
          del self.pending[boid]
          continue
        self.ledger.update_order(info["oid"], ts.isoformat(timespec="seconds"), status="CANCELLED",
                                 note="市價單逾時未收到成交回報，視為未成交，重新監控")
        self.ledger.log(f"{KIND_ZH.get(info['kind'], info['kind'])} 市價單逾時未成交，將重新送出", self.ai, "WARN", ts)
        del self.pending[boid]

  def _cancel_working(self, ts: datetime, code: str, reason: str) -> None:
    """Cancels every unfilled order of this AI (ledger triggers + broker-side resting orders)."""
    for o in self.ledger.working_orders(self.ai):
      if o["order_type"] in ("STOP", "TAKE", "LMT"):
        self.ledger.update_order(o["id"], ts.isoformat(timespec="seconds"), status="CANCELLED", note=reason)
    try:
      self.broker.cancel_all(code)
    except Exception as exc:
      self.ledger.log(f"券商刪單失敗：{exc}", self.ai, "ERROR", ts)

  def process_fills(self, ts: datetime) -> None:
    for f in self.broker.poll_fills():
      info = self.pending.pop(f["broker_order_id"], None)
      if info is None:
        self.ledger.log(f"⛔ 收到未知委託成交 {f}，啟動緊急保護", self.ai, "ERROR", ts)
        if self.broker.name != "local_paper":
          self.ledger.set_state("kill_switch", {"ts": ts.isoformat(timespec="seconds"),
                                                "reason": f"未知委託成交 {f}"})
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
    sk = e["session_key"]
    self.ledger.set_state(f"cnt:{self.ai}:{sk}", self.entries(sk) + 1)
    common = dict(ai=self.ai, ts=t, trading_day=info["td"], contract=info["code"], side=opposite(side),
                  qty=self.qty, status="WORKING", broker=self.broker.name)
    tp_oid = self.ledger.add_order(kind="TP", order_type="TAKE", trigger_price=tp, note=f"停利觸價 {tp:.0f}", **common)
    sl_oid = self.ledger.add_order(kind="SL", order_type="STOP", trigger_price=sl, note=f"停損觸價 {sl:.0f}", **common)
    self.pos = {"side": side, "qty": self.qty, "entry_price": price, "entry_ts": t, "contract": info["code"],
                "trading_day": info["td"], "session_key": sk, "sl": sl, "tp": tp,
                "sl_oid": sl_oid, "tp_oid": tp_oid, "entry_cost": self.side_cost(price, self.qty),
                "reason": e["reason"], "regime": e.get("regime", "chop"), "mode": e.get("mode", "native"),
                "mfe": 0.0, "mae": 0.0, "trail": e.get("trail_pts"), "init_sl_pts": float(e["sl_pts"])}
    ENTRY_BUS[self.ai] = {"bar_end": e.get("bar_end"), "side": side, "sl_pts": e["sl_pts"], "tp_pts": e["tp_pts"],
                          "price": price, "reason": e["reason"]}
    self.ledger.log(f"✅ {'做多' if side == 'B' else '放空'}成交｜進場：{price:.0f}"
                    f"｜停損：{sl:.0f}（{abs(price - sl):.0f} 點）｜停利：{tp:.0f}（{abs(tp - price):.0f} 點）"
                    f"｜{e['reason']}", self.ai, "TRADE", ts)

  def _close(self, ts: datetime, info: dict, price: float) -> None:
    from .calendar import trading_day as td_fn
    p = self.pos
    if not p:
      return
    d = 1 if p["side"] == "B" else -1
    points = (price - p["entry_price"]) * d
    gross = points * self.pv * p["qty"]
    cost = p["entry_cost"] + self.side_cost(price, p["qty"])
    net = gross - cost
    trade_td = td_fn(ts).isoformat() if getattr(self.s, "hold_overnight", False) else p["trading_day"]
    self.ledger.add_trade(ai=self.ai, trading_day=trade_td, contract=p["contract"], side=p["side"],
                          qty=p["qty"], entry_ts=p["entry_ts"], entry_price=p["entry_price"],
                          exit_ts=ts.isoformat(timespec="seconds"), exit_price=price, points=points,
                          gross_twd=gross, cost_twd=cost, pnl_twd=net, exit_kind=info["kind"],
                          entry_reason=p["reason"])
    self.pos = None
    self.ledger.set_state(f"last_exit:{self.ai}", ts.isoformat())
    self._cancel_working(ts, info["code"], "部位已平倉，刪除剩餘委託")
    self.ledger.log(f"🏁 {KIND_ZH.get(info['kind'], info['kind'])}平倉｜進場：{p['entry_price']:.0f} → 出場：{price:.0f}"
                    f"（{points:+.0f} 點）｜淨損益 NT$ {net:+,.0f}，等待下次進場", self.ai, "TRADE", ts)
    if info["kind"] == "ROLL" and hasattr(self.s, "reentry_side"):
      self.s.reentry_side = p["side"]  # Re-open in the new contract if the trend is intact.
      self.ledger.set_state(f"reentry:{self.ai}", p["side"])
    prot = info["kind"] == "SL" and is_protected_exit(p["side"], p["entry_price"], p["sl"], price)
    if prot:  # Recorded in every mode (analysis); only protected mode acts on it.
      keep = (self.ledger.get_state(f"protected_exits:{self.ai}") or [])[-(PROTECTED_KEEP - 1):]
      self.ledger.set_state(f"protected_exits:{self.ai}", keep + [ts.isoformat(timespec="seconds")])
    if info["kind"] == "SL" and net < 0 and getattr(self.s, "block_after_stop", False) and \
        not (self.protected_mode and prot):
      self.ledger.set_state(f"block:{self.ai}", {"side": p["side"], "ts": ts.isoformat()})
    lesson = self.brain.review(ts, p, price, info["kind"], net)
    self.ledger.log(f"🧠 檢討：{lesson['diagnosis']}｜{'；'.join(lesson['adjustments'])}", self.ai, "LEARN", ts)

  # ---------- decisions ----------
  def flatten(self, ts: datetime, code: str, quote: dict, kind: str) -> None:
    p = self.pos
    if not p or self.pending:
      return
    self._cancel_working(ts, code, KIND_ZH[kind])
    self._send_market(ts, p["trading_day"], p["contract"], kind, opposite(p["side"]), quote, KIND_ZH[kind])

  def on_price(self, ts: datetime, td: str, code: str, quote: dict, minutes_to_close: float, kill: bool) -> None:
    if kill:
      self._expire_limits(ts, "緊急停止，刪除限價單")
    elif minutes_to_close <= self.cfg["no_entry_minutes_before_close"] and not getattr(self.s, "hold_overnight", False):
      self._expire_limits(ts, "接近收盤，刪除未成交限價單")
    else:
      self._expire_limits(ts)
    had_pos = self.pos is not None
    if hasattr(self.broker, "on_quote"):
      self.broker.on_quote(quote)
    self.process_fills(ts)
    self._expire_markets(ts)
    p = self.pos
    last = quote["last"]
    hi, lo = max(last, quote.get("hi") or last), min(last, quote.get("lo") or last)
    if p and not self.pending:
      d = 1 if p["side"] == "B" else -1
      if kill:
        self.flatten(ts, code, quote, "KILL")
      elif minutes_to_close <= self.cfg["flatten_minutes_before_close"] and not getattr(self.s, "hold_overnight", False):
        self.flatten(ts, code, quote, "SESSION_END")
      elif had_pos:
        fav = (hi - p["entry_price"]) * d if d == 1 else (p["entry_price"] - lo)
        adv = (p["entry_price"] - lo) if d == 1 else (hi - p["entry_price"])
        if fav > p.get("mfe", 0) or adv > p.get("mae", 0):  # Track best / worst excursion for the review.
          p["mfe"], p["mae"] = max(p.get("mfe", 0), fav), max(p.get("mae", 0), adv)
          self.pos = p
        sl_hit = lo <= p["sl"] if p["side"] == "B" else hi >= p["sl"]
        tp_hit = hi >= p["tp"] if p["side"] == "B" else lo <= p["tp"]
        if sl_hit:  # If both were touched within one poll, assume the stop came first (conservative).
          self._fire(ts, quote, "SL", p["sl_oid"])
        elif tp_hit:
          self._fire(ts, quote, "TP", p["tp_oid"])
        elif getattr(self.s, "hold_overnight", False) and (
            self.cfg["capital_per_ai_twd"] + self.ledger.realized(self.ai) + self.unrealized(last)
            < self.cfg.get("maintenance_margin_twd", 26900)
            or self.unrealized(last) < -(self.cfg["capital_per_ai_twd"] - self.cfg.get("maintenance_margin_twd", 26900))):
          self.ledger.log("未實現虧損逼近維持保證金，強制平倉避免追繳", self.ai, "WARN", ts)
          self.flatten(ts, code, quote, "RISK")
        else:
          tick = self.cfg["tick"]
          cand_sl = p["sl"]
          be_r = getattr(self.s, "be_r", None)
          init_sl = p.get("init_sl_pts") or abs(p["entry_price"] - p["sl"])
          if be_r and init_sl and p.get("mfe", 0) >= be_r * init_sl:
            be_sl = round((p["entry_price"] + d * BE_OFFSET_PTS) / tick) * tick
            cand_sl = max(cand_sl, be_sl) if d == 1 else min(cand_sl, be_sl)
          if p.get("trail"):
            if hasattr(self.s, "current_atr"):
              a60 = self.s.current_atr()
              if a60 is not None:
                tr_d = round(self.s.atr_k * a60)
                if getattr(self.s, "max_sl_pts", None):
                  tr_d = min(tr_d, round(self.s.max_sl_pts))
                p["trail"] = tr_d
            best = p["entry_price"] + p["mfe"] if d == 1 else p["entry_price"] - p["mfe"]
            tr_sl = round((best - d * p["trail"]) / tick) * tick
            cand_sl = max(cand_sl, tr_sl) if d == 1 else min(cand_sl, tr_sl)
          if (d == 1 and cand_sl > p["sl"]) or (d == -1 and cand_sl < p["sl"]):
            p["sl"] = cand_sl
            self.pos = p
            self.ledger.update_order(p["sl_oid"], ts.isoformat(timespec="seconds"), trigger_price=cand_sl,
                                     note=f"停損上調至 {cand_sl:.0f}" if d == 1 else f"停損下調至 {cand_sl:.0f}")
    self.process_fills(ts)
    if (self.cfg.get("enforce_daily_loss", True) and not getattr(self.s, "hold_overnight", False)
        and self.locked_day() != td and self.ledger.realized(self.ai, td) <= -self.cfg["max_daily_loss_twd"]):
      self.ledger.set_state(f"lock:{self.ai}", td)
      self.ledger.log(f"今日已實現損益達 -NT$ {self.cfg['max_daily_loss_twd']:,}，今日不再進新單", self.ai, "WARN", ts)

  def _fire(self, ts: datetime, quote: dict, kind: str, trigger_oid: int) -> None:
    p = self.pos
    t = ts.isoformat(timespec="seconds")
    self.ledger.update_order(trigger_oid, t, status="TRIGGERED", note=f"{KIND_ZH[kind]}（成交價 {quote['last']:.0f}）")
    self._cancel_working(ts, p["contract"], f"{KIND_ZH[kind]}，刪除其他未成交委託")
    trig = p["tp"] if kind == "TP" else p["sl"]
    open_p = quote.get("open") or trig
    q = dict(quote)
    if p["side"] == "B":  # Exit is a sell.
      q["bid"] = trig if kind == "TP" else min(trig, open_p)
    else:
      q["ask"] = trig if kind == "TP" else max(trig, open_p)
    self._send_market(ts, p["trading_day"], p["contract"], kind, opposite(p["side"]), q, KIND_ZH[kind])

  def _filter(self, sig, bars: list[Bar]) -> str | None:
    """Universal guards applied after learning. Returns a skip reason or None."""
    hold = getattr(self.s, "hold_overnight", False)
    if RISK_STATE.get("fomc_window") and not hold:
      return "🏦 聯準會利率決議前後 1 小時，暫停當沖進場"
    regime, er, direction = market_regime(bars)
    against = (sig.side == "B" and direction < 0) or (sig.side == "S" and direction > 0)
    if regime == "trend" and against and not getattr(self.s, "skip_trend_filter", False):
      return f"5 分K 強{'多' if direction > 0 else '空'}趨勢（ER {er:.2f}），不逆勢{'做多' if sig.side == 'B' else '放空'}"
    block = self.ledger.get_state(f"block:{self.ai}")
    if block and bars:
      from .calendar import session_of
      _, s_start, _ = session_of(bars[-1].end - timedelta(seconds=1))
      reset_before = s_start.isoformat() if s_start else (bars[0].end - timedelta(minutes=5)).isoformat()
      if block["ts"] < reset_before:
        self.ledger.set_state(f"block:{self.ai}", None)  # Stop-loss in an earlier session: reset (as in wfo).
        block = None
    if block and block["side"] == sig.side:
      # Same as the simulator: a bar after the stop touched the session VWAP as it was AT that bar.
      touched, pv, vol = False, 0.0, 0.0
      span = (bars[-1].end - bars[-2].end) if len(bars) >= 2 else timedelta(minutes=1)
      for b in bars:
        pv += (b.h + b.l + b.c) / 3 * b.v
        vol += b.v
        vw_b = pv / vol if vol else b.c
        # Only bars that START at/after the stop (the stop's own bar also contains pre-stop prices).
        if (b.end - span).isoformat() >= block["ts"] and b.l <= vw_b <= b.h:
          touched = True
          break
      if not touched:
        vw = vwap(bars)
        return f"上一筆{'多' if sig.side == 'B' else '空'}單停損，價格尚未回到均價 {vw:.0f}，暫不同向進場"
      self.ledger.set_state(f"block:{self.ai}", None)
    return self._lesson_filter(sig, bars)

  def _lesson_filter(self, sig, bars: list[Bar]) -> str | None:
    """Rules adopted from the weekly losing-trade review (data/strategy_params.json "lessons"; see lessons.py)."""
    rules = getattr(self.s, "rules", None) or {}
    if not rules or not bars:
      return None
    from .calendar import session_of
    _, s_start, _ = session_of(bars[-1].end - timedelta(seconds=1))
    start = s_start or (bars[0].end - timedelta(minutes=5))
    now = bars[-1].end
    if rules.get("open_delay") and now < start + timedelta(minutes=rules["open_delay"]):
      return f"📚 經驗規則：開盤 {rules['open_delay']} 分鐘內不進場"
    if rules.get("min_er") and not getattr(self.s, "hold_overnight", False):
      _, er, _ = market_regime(bars)
      if er < rules["min_er"]:
        return f"📚 經驗規則：5 分K 效率比 {er:.2f} < {rules['min_er']:.2f}，趨勢純度不足不進場"
    if rules.get("max_vwap_atr") and not getattr(self.s, "hold_overnight", False):
      from .strategies import atr as atr_fn
      from .learning import to_5min
      b5 = to_5min(bars)
      a5 = max(atr_fn(b5), 5.0) if len(b5) >= 2 else 20.0
      vw = vwap(bars)
      if abs(bars[-1].c - vw) > rules["max_vwap_atr"] * a5:
        return f"📚 經驗規則：距離均價 {abs(bars[-1].c - vw):.0f} 點 > {rules['max_vwap_atr']}×ATR，不追高殺低"
    if rules.get("max_vwap_pts") and not getattr(self.s, "hold_overnight", False):
      vw = vwap(bars)
      if abs(bars[-1].c - vw) > rules["max_vwap_pts"]:
        return f"📚 經驗規則：距離均價 {abs(bars[-1].c - vw):.0f} 點 > {rules['max_vwap_pts']:.0f} 點，不追高殺低"
    trades = self.ledger.rows("SELECT side, exit_ts, exit_kind, pnl_twd FROM trades WHERE ai=? AND exit_ts>=? "
                              "ORDER BY exit_ts, id", (self.ai, start.isoformat()))
    if self.protected_mode:  # Losing protected exits are not stops: skip them (a winning one still resets,
      prot = set(self.ledger.get_state(f"protected_exits:{self.ai}") or [])  # exactly as in the simulator).
      trades = [r for r in trades if not (r["exit_kind"] == "SL" and r["pnl_twd"] < 0 and r["exit_ts"] in prot)]
    consec_sl = 0
    for r in reversed(trades):
      if r["exit_kind"] == "SL" and r["pnl_twd"] < 0:
        consec_sl += 1
      elif r["pnl_twd"] > 0:
        break
    if rules.get("max_sl") and consec_sl >= rules["max_sl"]:
      return f"📚 經驗規則：這一盤已連續停損 {consec_sl} 次，今天這盤休息"
    if rules.get("rev_cool") and trades:
      last = trades[-1]
      if last["exit_kind"] == "SL" and last["pnl_twd"] < 0 and last["side"] != sig.side:
        ex_dt = datetime.fromisoformat(last["exit_ts"])
        sl_bar_end = ex_dt.replace(second=0, microsecond=0) + (timedelta(minutes=1) if ex_dt.second > 0 else timedelta())
        if (now - sl_bar_end).total_seconds() / 60 < rules["rev_cool"]:
          return f"📚 經驗規則：剛停損，{rules['rev_cool']} 分鐘內不反手"
    return None

  def on_bar(self, ts: datetime, td: str, code: str, session_key: str, bars: list[Bar], quote: dict,
             minutes_to_close: float, fresh: bool, kill: bool, bars1: list[Bar] | None = None) -> None:
    if not kill:
      self._expire_limits(ts)
    if self.pos or self.pending or kill or not fresh or minutes_to_close <= 0:
      return
    hold = getattr(self.s, "hold_overnight", False)
    locked = self.cfg.get("enforce_daily_loss", True) and not hold and self.locked_day() == td
    no_entry = self.cfg["no_entry_minutes_before_close"]
    if locked or (minutes_to_close <= no_entry and not hold):
      return
    hours = getattr(self.s, "hours", None)
    if hours and bars:
      from .calendar import tod_bucket
      if tod_bucket(bars[-1].end) not in hours:
        return
    last_exit = self.ledger.get_state(f"last_exit:{self.ai}")
    since = None
    if last_exit:
      le = datetime.fromisoformat(last_exit)
      if bars and le >= bars[0].end:
        since = sum(1 for b in bars if b.end > le)
    self.brain.daily_decay(td)
    cap_s = getattr(self.s, "max_entries", None)
    cap = min(cap_s, self.cfg["max_entries_per_session"]) if cap_s else self.cfg["max_entries_per_session"]
    n_ent = self.entries(session_key)
    if n_ent >= cap:
      return
    try:
      raw = self.s.decide(bars, n_ent, since, bars1)
    except TypeError:
      raw = self.s.decide(bars, n_ent, since)
    if raw is None:
      return
    b1 = bars1 if bars1 else bars
    sig, ctx = self.brain.adjust(raw, b1)
    why = ctx["why"] if sig is None else self._filter(sig, b1)
    if why:
      self.ledger.set_state(f"skip:{self.ai}", {"ts": ts.isoformat(timespec="seconds"), "why": why})
      return
    exp_base = bars[-1].end if (bars and abs((ts - bars[-1].end).total_seconds()) <= 300) else ts
    if getattr(self.s, "fixed_bracket", False):
      tp_pts, reason = sig.tp_pts, sig.reason
    else:
      from .calendar import session_of
      _, _, s_close = session_of(exp_base - timedelta(seconds=1)) if bars else (None, None, None)
      eff_mtc = (s_close - exp_base).total_seconds() / 60 if s_close else minutes_to_close
      rem_min = eff_mtc if abs(eff_mtc - minutes_to_close) <= 5 else minutes_to_close
      tp_pts, reason = tp_cap(sig.sl_pts, sig.tp_pts, bars, rem_min, sig.reason)
    entry = {"side": sig.side, "sl_pts": sig.sl_pts, "tp_pts": tp_pts, "reason": reason,
             "trail_pts": getattr(sig, "trail_pts", None),
             "session_key": session_key, "regime": ctx["regime"], "mode": ctx["mode"],
             "bar_end": exp_base.isoformat()}
    lim = getattr(sig, "limit_price", None)
    if lim is None and getattr(self.s, "limit_atr", None) is not None:
      from .strategies import atr as atr_fn  # Limit = last close pulled back by limit_atr x ATR (5-min).
      d = 1 if sig.side == "B" else -1
      lim = round(bars[-1].c - d * self.s.limit_atr * atr_fn(bars))
    if lim is not None:
      self._send_limit(ts, td, code, sig.side, lim, getattr(self.s, "limit_min", 10), reason, entry)
    else:
      self._send_market(ts, td, code, "ENTRY", sig.side, quote, reason, entry)
    self.process_fills(ts)
