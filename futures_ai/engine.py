"""Main loop: poll quotes, run both AI traders, record equity, publish dashboard state."""

from __future__ import annotations

import time
import traceback
from datetime import datetime, timedelta

from .calendar import now_tpe, rollover_info, session_of
from .feed import TaifexFeed
from .learning import to_5min


def completed_5min(bars1: list) -> list:
  """Aggregates completed 1-minute bars into 5-minute bars, dropping a still-forming last bucket."""
  if not bars1:
    return []
  b5 = to_5min(bars1)
  return [b for b in b5 if b.end <= bars1[-1].end]


class Engine:

  def __init__(self, cfg: dict, ledger, traders: list, feed=None, clock=now_tpe, publisher=None, shadows=None):
    self.cfg = cfg
    self.shadows = shadows or []
    self.ledger = ledger
    self.traders = traders
    self.feed = feed or TaifexFeed()
    self.clock = clock
    self.publisher = publisher
    self.last_bar_end: datetime | None = None
    self.last_equity_minute: str | None = None
    self.last_publish = 0.0
    self.prev_live: tuple | None = None  # (bar_end, high, low) seen at the previous poll
    self.barstore = None
    if any(hasattr(t.s, "h60") for t in self.traders + self.shadows):
      from .barstore import BarStore
      self.barstore = BarStore()

  def _record_equity(self, now: datetime, last: float, force: bool = False) -> None:
    minute = now.strftime("%Y-%m-%dT%H:%M")
    if minute == self.last_equity_minute and not force:
      return
    self.last_equity_minute = minute
    for tr in self.traders + self.shadows:
      tr.ledger.add_equity(minute + ":00", tr.ai, tr.ledger.realized(tr.ai), tr.unrealized(last))

  def _touched_range(self, snap) -> tuple[float, float]:
    """Highest / lowest trade since the previous poll, so fast spikes still hit 觸價單."""
    hi = lo = snap.last
    lv, prev = snap.live_bar, self.prev_live
    if lv and prev:
      if prev[0] == lv.end:
        hi = max(hi, lv.h) if lv.h > prev[1] else hi
        lo = min(lo, lv.l) if lv.l < prev[2] else lo
      else:
        hi, lo = max(hi, lv.h), min(lo, lv.l)
        done = next((b for b in snap.bars if b.end == prev[0]), None)
        if done:
          hi = max(hi, done.h) if done.h > prev[1] else hi
          lo = min(lo, done.l) if done.l < prev[2] else lo
    self.prev_live = (lv.end, lv.h, lv.l) if lv else None
    return hi, lo

  def step(self) -> str:
    now = self.clock()
    name, start, end = session_of(now)
    if name is None or (name == "night" and not self.cfg["trade_night_session"]):
      return "closed"
    info = rollover_info(now, self.cfg["product"], self.cfg["roll_days_before"])
    code, td = info["contract"], info["trading_day"]
    snap = self.feed.snapshot(code, name, start, now)
    if name == "day" and snap.cdate and snap.cdate != now.strftime("%Y%m%d"):
      return "holiday"  # Exchange closed today (quote date is not today).
    if snap.quote_time is None or not snap.last:
      return "no-data"
    hi, lo = self._touched_range(snap)
    quote = {"last": snap.last, "bid": snap.bid, "ask": snap.ask, "hi": hi, "lo": lo}
    age = (now - snap.quote_time).total_seconds()
    fresh = age < self.cfg["stale_seconds"]
    session_key = f"{td}-{name}"
    minutes_to_close = (end - now).total_seconds() / 60
    kill = bool(self.ledger.get_state("kill_switch", False))
    self.ledger.set_state("market", {"contract": code, "session": name, "last": snap.last, "bid": snap.bid,
                                     "ask": snap.ask, "quote_time": snap.quote_time.isoformat(),
                                     "fresh": fresh, "updated_at": now.isoformat(timespec="seconds"),
                                     "bars": len(snap.bars)})

    for tr in self.traders + self.shadows:  # Positions left from another session or an expiring contract.
      p = tr.pos
      if p and p["contract"] == code and p["session_key"] != session_key and getattr(tr.s, "hold_overnight", False):
        p["session_key"] = session_key  # Swing positions are held across sessions.
        tr.pos = p
        continue
      if p and not tr.pending and (p["contract"] != code or p["session_key"] != session_key):
        q = quote
        if p["contract"] != code:
          old = self.feed.snapshot(p["contract"], name, start, now)
          q = {"last": old.last, "bid": old.bid, "ask": old.ask}
        tr.flatten(now, p["contract"], q, "ROLL" if p["contract"] != code else "SESSION_END")

    for tr in self.traders:
      tr.on_price(now, td, code, quote, minutes_to_close, kill)
    for tr in self.shadows:  # Shadows keep learning even during an emergency stop (paper only).
      try:
        tr.on_price(now, td, code, quote, minutes_to_close, False)
      except Exception as exc:
        print(f"shadow {tr.ai} error: {exc}")

    if self.barstore is not None and snap.bars:  # Multi-day 60-minute bars for the swing AI.
      self.barstore.add(snap.bars)
      h60 = self.barstore.sixty_min(now)
      for tr in self.traders + self.shadows:
        if hasattr(tr.s, "h60"):
          tr.s.h60 = h60
    bars5 = completed_5min(snap.bars)  # The AIs decide on completed 5-minute K bars.
    if bars5 and bars5[-1].end != self.last_bar_end:
      self.last_bar_end = bars5[-1].end
      for tr in self.traders:
        tr.on_bar(now, td, code, session_key, bars5, quote, minutes_to_close, fresh, kill)
      for tr in self.shadows:
        try:
          tr.on_bar(now, td, code, session_key, bars5, quote, minutes_to_close, fresh, False)
        except Exception as exc:
          print(f"shadow {tr.ai} error: {exc}")

    self._record_equity(now, snap.last)
    return "trading" if fresh else "stale"

  def refresh_margin(self) -> None:
    from .margin import fetch_margin
    try:
      m = fetch_margin(self.cfg["product"])
      if m:
        m["updated"] = self.clock().isoformat(timespec="seconds")
        old = self.ledger.get_state("margin") or {}
        self.ledger.set_state("margin", m)
        if old.get("initial") and old["initial"] != m["initial"]:
          self.ledger.log(f"期交所保證金調整：原始保證金 {old['initial']:,} → {m['initial']:,}", level="WARN")
    except Exception as exc:
      print(f"margin refresh failed: {exc}")

  def archive_history(self) -> None:
    """TAIFEX only keeps ~30 trading days of tick files online; archive them daily so history grows."""
    from datetime import timedelta as td_
    from .pretrain import download
    d = self.clock().date()
    got = 0
    for _ in range(45):
      d -= td_(days=1)
      if d.weekday() < 5 and download(d):
        got += 1
    self.ledger.log(f"歷史資料庫存：已保存 {got} 個交易日的期交所逐筆檔")

  def run_forever(self) -> None:
    from .control import GitHubControl
    from .notify import LineNotifier, summary_text
    from .state import build_state
    self.ledger.log(f"引擎啟動：模式 {self.cfg['mode']}，AI：{', '.join(t.s.name for t in self.traders)}")
    for tr in self.traders:
      tr.broker.connect()
    notifier = LineNotifier(self.cfg, self.ledger)
    control = GitHubControl(self.cfg, self.ledger, notifier)
    names = {t.ai: t.s.name for t in self.traders}
    status, margin_day = None, None
    _, _, prev_end = session_of(self.clock())
    prev_session = session_of(self.clock())[0]
    while True:
      now = self.clock()
      td = now.strftime("%Y-%m-%d")
      if margin_day != td:
        margin_day = td
        self.refresh_margin()
        self.archive_history()
      control.poll()
      try:
        new_status = self.step()
        if new_status != status:
          self.ledger.log(f"狀態：{new_status}")
          status = new_status
      except Exception as exc:
        self.ledger.log(f"引擎錯誤：{exc}", level="ERROR")
        traceback.print_exc()
        new_status = "error"
      sess, _, sess_end = session_of(self.clock())
      try:
        notifier.collect(names)
        if prev_session and sess != prev_session:  # A session just closed.
          # Evaluate the state as of the last second of the closed session (correct trading day).
          st = build_state(self.cfg, self.ledger, now=prev_end - timedelta(seconds=1))
          if prev_session == "day":
            notifier.session_summary(summary_text(st, "日盤"))  # 13:45 report
          else:
            notifier.defer_night(summary_text(st, "夜盤"))  # Sent at notify.night_report_time (08:30)
        elif sess:
          notifier.maybe_digest(f"{'日盤' if sess == 'day' else '夜盤'} {now.strftime('%H:%M')}")
        notifier.maybe_send_night(self.clock())
      except Exception as exc:
        print(f"notify failed: {exc}")
      prev_session, prev_end = sess, sess_end or prev_end
      if self.publisher and time.monotonic() - self.last_publish >= self.cfg["publish"]["interval_seconds"]:
        self.last_publish = time.monotonic()
        try:
          self.publisher.publish()
        except Exception as exc:
          print(f"publish failed: {exc}")
      time.sleep(self.cfg["poll_seconds"] if new_status in ("trading", "stale") else 20)
