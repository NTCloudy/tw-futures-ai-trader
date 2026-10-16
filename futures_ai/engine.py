"""Main loop: poll quotes, run both AI traders, record equity, publish dashboard state."""

from __future__ import annotations

import time
import traceback
from datetime import datetime, timedelta

from .calendar import now_tpe, rollover_info, session_of
from .feed import TaifexFeed


class Engine:

  def __init__(self, cfg: dict, ledger, traders: list, feed=None, clock=now_tpe, publisher=None):
    self.cfg = cfg
    self.ledger = ledger
    self.traders = traders
    self.feed = feed or TaifexFeed()
    self.clock = clock
    self.publisher = publisher
    self.last_bar_end: datetime | None = None
    self.last_equity_minute: str | None = None
    self.last_publish = 0.0

  def _record_equity(self, now: datetime, last: float, force: bool = False) -> None:
    minute = now.strftime("%Y-%m-%dT%H:%M")
    if minute == self.last_equity_minute and not force:
      return
    self.last_equity_minute = minute
    for tr in self.traders:
      self.ledger.add_equity(minute + ":00", tr.ai, self.ledger.realized(tr.ai), tr.unrealized(last))

  def step(self) -> str:
    now = self.clock()
    name, start, end = session_of(now)
    if name is None or (name == "night" and not self.cfg["trade_night_session"]):
      return "closed"
    info = rollover_info(now, self.cfg["product"], self.cfg["roll_days_before"])
    code, td = info["contract"], info["trading_day"]
    snap = self.feed.snapshot(code, name, start, now)
    if snap.quote_time is None or not snap.last:
      return "no-data"
    quote = {"last": snap.last, "bid": snap.bid, "ask": snap.ask}
    age = (now - (snap.quote_time - timedelta(minutes=1))).total_seconds()
    fresh = age < self.cfg["stale_seconds"]
    session_key = f"{td}-{name}"
    minutes_to_close = (end - now).total_seconds() / 60
    kill = bool(self.ledger.get_state("kill_switch", False))
    self.ledger.set_state("market", {"contract": code, "session": name, "last": snap.last, "bid": snap.bid,
                                     "ask": snap.ask, "quote_time": snap.quote_time.isoformat(),
                                     "fresh": fresh, "updated_at": now.isoformat(timespec="seconds"),
                                     "bars": len(snap.bars)})

    for tr in self.traders:  # Positions left from another session or an expiring contract.
      p = tr.pos
      if p and not tr.pending and (p["contract"] != code or p["session_key"] != session_key):
        q = quote
        if p["contract"] != code:
          old = self.feed.snapshot(p["contract"], name, start, now)
          q = {"last": old.last, "bid": old.bid, "ask": old.ask}
        tr.flatten(now, p["contract"], q, "ROLL" if p["contract"] != code else "SESSION_END")

    for tr in self.traders:
      tr.on_price(now, td, code, quote, minutes_to_close, kill)

    if snap.bars and snap.bars[-1].end != self.last_bar_end:
      self.last_bar_end = snap.bars[-1].end
      for tr in self.traders:
        tr.on_bar(now, td, code, session_key, snap.bars, quote, minutes_to_close, fresh, kill)

    self._record_equity(now, snap.last)
    return "trading" if fresh else "stale"

  def run_forever(self) -> None:
    self.ledger.log(f"引擎啟動：模式 {self.cfg['mode']}，AI：{', '.join(t.s.name for t in self.traders)}")
    for tr in self.traders:
      tr.broker.connect()
    status = None
    while True:
      try:
        new_status = self.step()
        if new_status != status:
          self.ledger.log(f"狀態：{new_status}")
          status = new_status
      except Exception as exc:
        self.ledger.log(f"引擎錯誤：{exc}", level="ERROR")
        traceback.print_exc()
        new_status = "error"
      if self.publisher and time.monotonic() - self.last_publish >= self.cfg["publish"]["interval_seconds"]:
        self.last_publish = time.monotonic()
        try:
          self.publisher.publish()
        except Exception as exc:
          print(f"publish failed: {exc}")
      time.sleep(self.cfg["poll_seconds"] if new_status in ("trading", "stale") else 20)
