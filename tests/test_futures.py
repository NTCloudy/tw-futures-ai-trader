import os
import unittest
from pathlib import Path
from unittest import mock
from datetime import date, datetime, timedelta

from futures_ai.calendar import TPE, active_month, contract_code, rollover_info, session_of, third_wednesday, trading_day
from futures_ai.config import DEFAULTS, _merge
from futures_ai.feed import Bar, Snapshot
from futures_ai.brokers import LocalPaperBroker
from futures_ai.engine import Engine
from futures_ai.ledger import Ledger
from futures_ai.strategies import AggressiveMomentum, Entry, OrbBreakout, VwapReversion
from futures_ai.trader import AiTrader


def cfg(**over):
  c = _merge(DEFAULTS, {"db_path": ":memory:", **over})
  c["capital_per_ai_twd"] = round(c["initial_margin_twd"] * c["capital_multiple"])
  return c


def at(y, m, d, hh, mm, ss=0):
  return datetime(y, m, d, hh, mm, ss, tzinfo=TPE)


class CalendarTest(unittest.TestCase):

  def test_sessions_and_trading_day(self):
    self.assertEqual(session_of(at(2026, 10, 16, 9, 30))[0], "day")
    self.assertEqual(session_of(at(2026, 10, 16, 14, 0))[0], None)
    self.assertEqual(session_of(at(2026, 10, 16, 15, 0))[0], "night")
    self.assertEqual(session_of(at(2026, 10, 17, 2, 0))[0], "night")  # Friday night -> Saturday 02:00
    self.assertEqual(session_of(at(2026, 10, 19, 2, 0))[0], None)  # Monday 02:00 (no Sunday night)
    self.assertEqual(trading_day(at(2026, 10, 16, 20, 0)), date(2026, 10, 19))  # Fri night -> Mon

  def test_settlement_and_rollover(self):
    self.assertEqual(third_wednesday(2026, 10), date(2026, 10, 21))
    self.assertEqual(contract_code("TMF", 2026, 10), "TMFJ6")
    self.assertEqual(active_month(date(2026, 10, 16)), (2026, 10))
    self.assertEqual(active_month(date(2026, 10, 20)), (2026, 11))  # 1 business day before settlement
    self.assertEqual(active_month(date(2026, 12, 15)), (2027, 1))
    self.assertEqual(rollover_info(at(2026, 10, 19, 20, 0), "TMF")["contract"], "TMFK6")  # Mon night = Tue


def bars_from(prices, start=at(2026, 10, 16, 8, 46)):
  out = []
  for i, (o, h, l, c) in enumerate(prices):
    out.append(Bar(start + timedelta(minutes=i), o, h, l, c, 100))
  return out


class StrategyTest(unittest.TestCase):

  def test_orb_long_breakout(self):
    start = at(2026, 10, 16, 8, 50)
    rng = [(47000, 47060, 47000, 47030)] * 3  # 08:50..09:00 opening range width = 60 (within 40..260)
    after = [(47030, 47055, 47020, 47045), (47045, 47080, 47040, 47075)]
    bars = [Bar(start + timedelta(minutes=5 * i), o, h, l, c, 100) for i, (o, h, l, c) in enumerate(rng + after)]
    orb = OrbBreakout(range_bars=3, sl_frac=0.75, tp_mult=3.0)
    e = orb.decide(bars, 0, None)
    self.assertIsNotNone(e)
    self.assertEqual(e.side, "B")
    self.assertEqual(e.tp_pts, e.sl_pts * 3)
    self.assertIsNone(orb.decide(bars, 2, None))  # max entries per session
    narrow = [Bar(start + timedelta(minutes=5 * i), 47000, 47020, 47000, 47010, 100) for i in range(3)] + [
        Bar(start + timedelta(minutes=15), 47010, 47018, 47008, 47015, 100),
        Bar(start + timedelta(minutes=20), 47015, 47040, 47014, 47035, 100),
    ]
    self.assertIsNone(orb.decide(narrow, 0, None))  # width 20 < min_width 40 filtered out

  def test_vwap_reversion_short(self):
    flat = [(47000, 47005, 46995, 47000)] * 25
    spike = [(47000, 47090, 47000, 47090), (47090, 47160, 47085, 47150), (47150, 47155, 47100, 47110)]
    e = VwapReversion().decide(bars_from(flat + spike), 0, None)
    self.assertIsNotNone(e)
    self.assertEqual(e.side, "S")


class AggressiveTest(unittest.TestCase):

  def test_momentum_long_and_short(self):
    up = [(47000 + i * 3, 47004 + i * 3, 46998 + i * 3, 47003 + i * 3) for i in range(20)] + [(47062, 47080, 47061, 47078)]
    self.assertEqual(AggressiveMomentum().decide(bars_from(up), 0, None).side, "B")
    down = [(47000 - i * 3, 47002 - i * 3, 46996 - i * 3, 46997 - i * 3) for i in range(20)] + [(46940, 46941, 46920, 46922)]
    self.assertEqual(AggressiveMomentum().decide(bars_from(down), 0, None).side, "S")
    self.assertIsNone(AggressiveMomentum().decide(bars_from(up), 30, None))


class FakeStrategy:
  id = "alpha-orb"
  name = "fake"

  def __init__(self, entry):
    self.entry = entry

  def decide(self, bars, n, since):
    return self.entry if n == 0 else None


class SkipLogTest(unittest.TestCase):
  """v2 spec 2026-11-28: raw signals and on_bar pauses are written as SKIP events (never notified)."""

  def setUp(self):
    self.cfg = cfg()
    self.ledger = Ledger(":memory:")
    self.tr = AiTrader(self.cfg, FakeStrategy(Entry("B", 40, 80, "test")), LocalPaperBroker(self.cfg), self.ledger)
    self.bars = bars_from([(47000, 47030, 46970, 47000)] * 20)

  def skips(self):
    return [r["message"] for r in self.ledger.rows("SELECT message FROM events WHERE level='SKIP' ORDER BY id")]

  def bar(self, m, fresh=True):
    self.tr.on_bar(at(2026, 10, 16, 9, m), "2026-10-16", "TMFJ6", "2026-10-16-day", self.bars,
                   {"last": 47000, "bid": 46999, "ask": 47000}, 200, fresh, False)

  def test_raw_signal_ordered_then_pause_logged_once(self):
    self.bar(10)
    self.assertEqual(self.skips(), ["🔎 原始訊號 做多 @ 47000｜已下單"])
    self.bar(15)
    self.bar(20)  # still in position: no second pause event
    self.assertEqual(self.skips()[1:], ["⏸ 暫停判斷進場：持有部位中"])

  def test_blocked_signal_logged_with_reason(self):
    from futures_ai import trader as trader_mod
    with mock.patch.dict(trader_mod.RISK_STATE, {"fomc_window": True}):
      self.bar(10)
    self.assertIsNone(self.tr.pos)
    self.assertEqual(len(self.skips()), 1)
    self.assertTrue(self.skips()[0].startswith("🔎 原始訊號 做多 @ 47000｜被擋：🏦 聯準會"))

  def test_entry_cap_pause_and_resume(self):
    self.cfg["max_entries_per_session"] = 0
    self.bar(10)
    self.bar(15)
    self.cfg["max_entries_per_session"] = 5
    self.tr.s.entry = None
    self.bar(20)
    self.assertEqual(self.skips(), ["⏸ 暫停判斷進場：本盤已進場 0 次（上限 0）", "▶ 恢復判斷進場"])

  def test_replay_skip_reason(self):
    from futures_ai import replay
    led = Ledger(":memory:")
    e = datetime(2026, 12, 1, 9, 31, tzinfo=TPE)
    with mock.patch.object(replay, "SKIP_SINCE", "2026-11-30T00:00:00+08:00"):
      self.assertEqual(replay.skip_reason(led, "sigma-vwap", e), "實盤沒有產生原始訊號（K 棒或計算與回測不同）")
      led.log("⏸ 暫停判斷進場：有未成交的進場掛單", "sigma-vwap", "SKIP", datetime(2026, 12, 1, 9, 0, tzinfo=TPE))
      self.assertEqual(replay.skip_reason(led, "sigma-vwap", e), "⏸ 暫停判斷進場：有未成交的進場掛單")
      led.log("⏸ 行情延遲，不判斷進場", None, "SKIP", datetime(2026, 12, 1, 9, 5, tzinfo=TPE))
      self.assertEqual(replay.skip_reason(led, "sigma-vwap", e), "⏸ 行情延遲，不判斷進場")
      led.log("🔎 原始訊號 放空 @ 40000｜被擋：x", "sigma-vwap", "SKIP", datetime(2026, 12, 1, 9, 30, tzinfo=TPE))
      self.assertEqual(replay.skip_reason(led, "sigma-vwap", e), "🔎 原始訊號 放空 @ 40000｜被擋：x")
      self.assertIsNone(replay.skip_reason(led, "sigma-vwap", datetime(2026, 11, 29, 9, 0, tzinfo=TPE)))

  def test_engine_gate_logged_on_change(self):
    eng = Engine.__new__(Engine)
    eng.ledger = self.ledger
    for why in (None, "行情延遲，不判斷進場", "行情延遲，不判斷進場", None, None):
      eng._skip_gate(at(2026, 10, 16, 9, 0), why)
    self.assertEqual(self.skips(), ["⏸ 行情延遲，不判斷進場", "▶ 行情恢復，繼續判斷進場"])

  def test_skip_events_not_notified_or_shown(self):
    import inspect
    from futures_ai import notify, state
    self.assertNotIn("SKIP", inspect.getsource(notify))
    self.assertIn("level != 'SKIP'", inspect.getsource(state))


class OcoBracketTest(unittest.TestCase):

  def setUp(self):
    self.cfg = cfg()
    self.ledger = Ledger(":memory:")
    self.tr = AiTrader(self.cfg, FakeStrategy(Entry("B", 40, 80, "test")), LocalPaperBroker(self.cfg), self.ledger)
    self.td = "2026-10-16"
    self.bars = bars_from([(47000, 47030, 46970, 47000)] * 20)

  def enter(self, price=47000):
    q = {"last": price, "bid": price - 1, "ask": price}
    self.tr.on_bar(at(2026, 10, 16, 9, 10), self.td, "TMFJ6", "2026-10-16-day", self.bars, q, 200, True, False)

  def test_entry_sets_both_triggers(self):
    self.enter()
    p = self.tr.pos
    self.assertEqual(p["entry_price"], 47001)  # ask + 1 tick slippage
    self.assertEqual((p["tp"], p["sl"]), (47081, 46961))
    working = self.ledger.working_orders("alpha-orb")
    self.assertEqual(sorted(o["order_type"] for o in working), ["STOP", "TAKE"])

  def test_take_profit_cancels_stop_and_goes_flat(self):
    self.enter()
    self.tr.on_price(at(2026, 10, 16, 9, 20), self.td, "TMFJ6", {"last": 47085, "bid": 47084, "ask": 47086}, 200, False)
    self.assertIsNone(self.tr.pos)
    statuses = {o["order_type"]: o["status"] for o in self.ledger.rows("SELECT * FROM orders WHERE order_type!='MKT'")}
    self.assertEqual(statuses, {"TAKE": "TRIGGERED", "STOP": "CANCELLED"})
    self.assertEqual(self.ledger.working_orders("alpha-orb"), [])
    t = self.ledger.rows("SELECT * FROM trades")[0]
    self.assertEqual(t["exit_kind"], "TP")
    self.assertEqual(t["points"], 47080 - 47001)  # TP trigger 47081 - 1 tick slippage
    cost = 2 * 12 + round(47001 * 10 * 0.00002) + round(47080 * 10 * 0.00002)
    self.assertAlmostEqual(t["pnl_twd"], 79 * 10 - cost)

  def test_spike_between_polls_triggers_take_profit(self):
    self.enter()
    # Last price is back below TP, but the high since the previous poll touched it.
    self.tr.on_price(at(2026, 10, 16, 9, 20), self.td, "TMFJ6",
                     {"last": 47060, "bid": 47059, "ask": 47061, "hi": 47090, "lo": 47055}, 200, False)
    self.assertIsNone(self.tr.pos)
    t = self.ledger.rows("SELECT exit_kind, exit_price FROM trades")[0]
    self.assertEqual(t["exit_kind"], "TP")
    self.assertEqual(t["exit_price"], 47081 - 1)  # anchored at the trigger, minus 1 tick slippage

  def test_stop_loss_and_daily_loss_lock(self):
    self.tr.s.entry = Entry("B", 150, 300, "wide")
    self.enter()
    # Price collapses: stop triggers first (-150 pts = -1500), not yet the 2000 lock.
    self.tr.on_price(at(2026, 10, 16, 9, 20), self.td, "TMFJ6", {"last": 46850, "bid": 46849, "ask": 46851}, 200, False)
    self.assertIsNone(self.tr.pos)
    self.assertEqual(self.ledger.rows("SELECT exit_kind FROM trades")[0]["exit_kind"], "SL")
    self.assertNotEqual(self.tr.locked_day(), self.td)

  def test_risk_lock_flattens_when_daily_loss_hit(self):
    self.cfg["max_daily_loss_twd"] = 2000
    self.tr.s.entry = Entry("B", 220, 440, "wide")
    self.enter()
    # Unrealized loss alone does not force-flatten an open position before its stop:
    self.tr.on_price(at(2026, 10, 16, 9, 15), self.td, "TMFJ6", {"last": 46795, "bid": 46794, "ask": 46796}, 200, False)
    self.assertIsNotNone(self.tr.pos)
    # Once stop-loss is hit and realized loss exceeds max_daily_loss_twd (2,000), the AI is locked for the day:
    self.tr.on_price(at(2026, 10, 16, 9, 20), self.td, "TMFJ6", {"last": 46770, "bid": 46769, "ask": 46771}, 200, False)
    self.assertIsNone(self.tr.pos)
    self.assertEqual(self.ledger.rows("SELECT exit_kind FROM trades")[0]["exit_kind"], "SL")
    self.assertEqual(self.tr.locked_day(), self.td)
    self.tr.s.entry = Entry("B", 40, 80, "again")
    self.tr.on_bar(at(2026, 10, 16, 10, 0), self.td, "TMFJ6", "2026-10-16-day", self.bars,
                   {"last": 46800, "bid": 46799, "ask": 46800}, 200, True, False)
    self.assertIsNone(self.tr.pos)  # locked for the rest of the day

  def test_kill_switch_flattens_cancels_triggers_and_blocks_entries(self):
    self.enter()
    self.tr.on_price(at(2026, 10, 16, 9, 15), self.td, "TMFJ6", {"last": 47010, "bid": 47009, "ask": 47011}, 200, True)
    self.assertIsNone(self.tr.pos)
    self.assertEqual(self.ledger.rows("SELECT exit_kind FROM trades")[0]["exit_kind"], "KILL")
    self.assertEqual(self.ledger.working_orders("alpha-orb"), [])
    self.tr.s.entry = Entry("B", 40, 80, "again")
    self.tr.on_bar(at(2026, 10, 16, 9, 20), self.td, "TMFJ6", "2026-10-16-day", self.bars,
                   {"last": 47000, "bid": 46999, "ask": 47000}, 200, True, True)
    self.assertIsNone(self.tr.pos)

  def test_session_end_flatten_and_no_late_entries(self):
    self.enter()
    self.tr.on_price(at(2026, 10, 16, 13, 41), self.td, "TMFJ6", {"last": 47010, "bid": 47009, "ask": 47011}, 4, False)
    self.assertIsNone(self.tr.pos)
    self.assertEqual(self.ledger.rows("SELECT exit_kind FROM trades")[0]["exit_kind"], "SESSION_END")
    self.tr.on_bar(at(2026, 10, 16, 13, 30), self.td, "TMFJ6", "2026-10-16-day2", self.bars,
                   {"last": 47000, "bid": 46999, "ask": 47000}, 15, True, False)
    self.assertIsNone(self.tr.pos)


class FakeFeed:

  def __init__(self):
    self.last = {"TMFJ6": 47000.0, "TMFK6": 47100.0}

  def snapshot(self, code, session, start, now):
    bars = bars_from([(self.last[code],) * 4] * 20, start=start + timedelta(minutes=1))
    bars = [b for b in bars if b.end <= now]
    return Snapshot(code, self.last[code], self.last[code] - 1, self.last[code] + 1, bars, now, None)


class EngineRollTest(unittest.TestCase):

  def test_position_in_expiring_contract_is_closed_on_roll(self):
    c = cfg()
    ledger = Ledger(":memory:")
    tr = AiTrader(c, FakeStrategy(None), LocalPaperBroker(c), ledger)
    tr.pos = {"side": "B", "qty": 1, "entry_price": 46900, "entry_ts": "x", "contract": "TMFJ6",
              "trading_day": "2026-10-19", "session_key": "2026-10-19-day", "sl": 1, "tp": 99999,
              "sl_oid": 0, "tp_oid": 0, "entry_cost": 29, "reason": "old"}
    eng = Engine(c, ledger, [tr], feed=FakeFeed(), clock=lambda: at(2026, 10, 20, 9, 30))
    eng.step()
    self.assertIsNone(tr.pos)
    trade = ledger.rows("SELECT * FROM trades")[0]
    self.assertEqual((trade["exit_kind"], trade["contract"]), ("ROLL", "TMFJ6"))
    self.assertEqual(ledger.get_state("market")["contract"], "TMFK6")


class LearningTest(unittest.TestCase):

  def setUp(self):
    from futures_ai.learning import Brain
    self.ledger = Ledger(":memory:")
    self.brain = Brain(self.ledger, AggressiveMomentum())

  def pos(self, side="B", entry=47000, sl=46985, tp=47020, mfe=2, mae=16, mode="native", regime="chop"):
    return {"side": side, "entry_price": entry, "sl": sl, "tp": tp, "mfe": mfe, "mae": mae,
            "entry_ts": "2026-10-16T10:00:00+08:00", "regime": regime, "mode": mode}

  def test_quick_stop_in_chop_widens_stop_and_switches_after_losses(self):
    now = at(2026, 10, 16, 10, 1)
    lesson = self.brain.review(now, self.pos(), 46982, "SL", -240)
    self.assertIn("假訊號", lesson["diagnosis"])
    self.assertAlmostEqual(self.brain.state["sl_mult"], 1.1)
    for _ in range(2):
      self.brain.review(now, self.pos(), 46982, "SL", -240)
    mode, why = self.brain.choose_mode("chop")
    self.assertEqual(mode, "flip")  # native kept losing in chop -> try the flipped strategy
    self.assertFalse(self.brain.paused(now + timedelta(minutes=5)))

  def test_giveback_shrinks_take_profit(self):
    lesson = self.brain.review(at(2026, 10, 16, 10, 10), self.pos(mfe=15), 46984, "SL", -220)
    self.assertIn("停利設太遠", lesson["diagnosis"])
    self.assertAlmostEqual(self.brain.state["tp_mult"], 0.9)

  def test_flip_mode_inverts_side_and_chooses_better_mode(self):
    st = self.brain.state["stats"]["chop"]
    st["native"].update(n=3, ew=-200)
    st["flip"].update(n=1, ew=-50)
    bars = bars_from([(47000, 47003, 46997, 47000 + (i % 2) * 2) for i in range(25)])
    sig, ctx = self.brain.adjust(Entry("B", 15, 20, "x"), bars)
    self.assertEqual((ctx["regime"], ctx["mode"], sig.side), ("chop", "flip", "S"))
    self.brain.daily_decay("2026-10-16")
    self.brain.daily_decay("2026-10-19")
    self.assertIsNotNone(self.brain.adjust(Entry("B", 15, 20, "x"), bars)[0])


class TrialModeTest(unittest.TestCase):

  def test_no_daily_loss_lock_when_disabled(self):
    c = cfg(enforce_daily_loss=False)
    ledger = Ledger(":memory:")
    tr = AiTrader(c, FakeStrategy(Entry("B", 400, 800, "wide")), LocalPaperBroker(c), ledger)
    bars = bars_from([(47000, 47030, 46970, 47000)] * 20)
    tr.on_bar(at(2026, 10, 16, 9, 10), "2026-10-16", "TMFJ6", "k", bars, {"last": 47000, "bid": 46999, "ask": 47000}, 200, True, False)
    tr.on_price(at(2026, 10, 16, 9, 20), "2026-10-16", "TMFJ6", {"last": 46790, "bid": 46789, "ask": 46791}, 200, False)
    self.assertIsNotNone(tr.pos)  # -2100 unrealized but no forced exit in trial mode
    self.assertIsNone(tr.locked_day())


class GuardsTest(unittest.TestCase):

  def setUp(self):
    self.cfg = cfg()
    self.ledger = Ledger(":memory:")
    self.tr = AiTrader(self.cfg, VwapReversion(), LocalPaperBroker(self.cfg), self.ledger)

  def test_no_counter_trend_entry_in_strong_5min_trend(self):
    up = [(47000 + i * 4, 47006 + i * 4, 46999 + i * 4, 47004 + i * 4) for i in range(60)]
    why = self.tr._filter(Entry("S", 30, 45, "fade"), bars_from(up))
    self.assertIn("不逆勢放空", why)
    self.assertIsNone(self.tr._filter(Entry("B", 30, 45, "with trend"), bars_from(up)))

  def test_same_direction_blocked_after_stop_until_vwap_touch(self):
    flat = [(47000, 47004, 46996, 47000 + (i % 3) - 1) for i in range(40)]
    bars = bars_from(flat)
    self.ledger.set_state("block:sigma-vwap", {"side": "S", "ts": bars[-1].end.isoformat()})
    self.assertIn("尚未回到均價", self.tr._filter(Entry("S", 30, 45, "x"), bars))
    later = bars + bars_from([(47000, 47003, 46997, 47001)], start=bars[-1].end + timedelta(minutes=1))
    self.assertIsNone(self.tr._filter(Entry("S", 30, 45, "x"), later))

  def test_stop_bar_itself_does_not_release_block(self):
    # The bar in which the stop happened (it ends after the stop) must not count as "back at VWAP".
    flat = [(47000, 47004, 46996, 47000 + (i % 3) - 1) for i in range(40)]
    bars = bars_from(flat)
    stop_ts = bars[-1].end - timedelta(seconds=30)
    self.ledger.set_state("block:sigma-vwap", {"side": "S", "ts": stop_ts.isoformat()})
    self.assertIn("尚未回到均價", self.tr._filter(Entry("S", 30, 45, "x"), bars))

  def test_block_from_previous_session_is_reset(self):
    flat = [(47000, 47004, 46996, 47000 + (i % 3) - 1) for i in range(40)]
    bars = bars_from(flat)
    old = bars[0].end - timedelta(hours=3)
    self.ledger.set_state("block:sigma-vwap", {"side": "S", "ts": old.isoformat()})
    self.assertIsNone(self.tr._filter(Entry("S", 30, 45, "x"), bars))


class ControlTest(unittest.TestCase):

  def test_owner_issue_stop_sets_kill_switch(self):
    from futures_ai.control import GitHubControl
    ledger = Ledger(":memory:")
    ctl = GitHubControl(cfg(), ledger)
    calls = []

    def fake(method, path, body=None, etag=None):
      calls.append((method, path))
      if "contents" in path:
        return 304, etag, None
      if method == "GET":
        return 200, None, [{"title": "STOP", "number": 7, "created_at": "x", "user": {"login": "NTCloudy"}},
                           {"title": "STOP", "number": 8, "created_at": "y", "user": {"login": "stranger"}}]
      return 200, None, {}
    ctl._req = fake
    ctl.poll()
    self.assertTrue(ledger.get_state("kill_switch"))
    self.assertIn(("PATCH", "/repos/NTCloudy/tw-futures-ai-trader/issues/7"), calls)
    self.assertNotIn(("PATCH", "/repos/NTCloudy/tw-futures-ai-trader/issues/8"), calls)


class TrailingStopTest(unittest.TestCase):

  def test_trailing_stop_ratchets_and_exits_in_profit(self):
    c = cfg()
    ledger = Ledger(":memory:")
    tr = AiTrader(c, FakeStrategy(Entry("B", 40, 400, "trend", 30)), LocalPaperBroker(c), ledger)
    bars = bars_from([(47000, 47030, 46970, 47000)] * 20)
    tr.on_bar(at(2026, 10, 16, 9, 10), "2026-10-16", "TMFJ6", "k", bars, {"last": 47000, "bid": 46999, "ask": 47000}, 200, True, False)
    self.assertEqual(tr.pos["sl"], 46961)
    tr.on_price(at(2026, 10, 16, 9, 11), "2026-10-16", "TMFJ6", {"last": 47090, "bid": 47089, "ask": 47091, "hi": 47101, "lo": 47080}, 200, False)
    self.assertEqual(tr.pos["sl"], 47101 - 30)  # ratcheted to best price - trail
    sl_order = ledger.order(tr.pos["sl_oid"])
    self.assertEqual(sl_order["trigger_price"], 47071)
    tr.on_price(at(2026, 10, 16, 9, 12), "2026-10-16", "TMFJ6", {"last": 47065, "bid": 47064, "ask": 47066}, 200, False)
    self.assertIsNone(tr.pos)
    t = ledger.rows("SELECT * FROM trades")[0]
    self.assertEqual(t["exit_kind"], "SL")
    self.assertGreater(t["pnl_twd"], 0)  # trailing stop locked in profit


class LearningModeTest(unittest.TestCase):

  def test_max_entries_and_live_refuses_learning_mode(self):
    from futures_ai.brokers import make_broker
    c = cfg(learning_mode=False, max_entries_per_session=3)
    ledger = Ledger(":memory:")
    st = FakeStrategy(Entry("B", 40, 80, "x"))
    st.max_entries = 2
    tr = AiTrader(c, st, LocalPaperBroker(c), ledger)
    tr.s.decide = lambda bars, n, since: Entry("B", 40, 80, "x")
    ledger.set_state("cnt:alpha-orb:k", 2)
    bars = bars_from([(47000, 47030, 46970, 47000)] * 20)
    tr.on_bar(at(2026, 10, 16, 9, 10), "2026-10-16", "TMFJ6", "k", bars, {"last": 47000, "bid": 46999, "ask": 47000}, 200, True, False)
    self.assertIsNone(tr.pos)  # strategy max_entries=2 respected
    with self.assertRaises(SystemExit):
      make_broker(cfg(mode="sinopac_live", i_understand_real_money=True, learning_mode=True), "nova-nightorb")


class TpCapTest(unittest.TestCase):

  def test_take_profit_capped_near_close(self):
    from futures_ai.trader import tp_cap
    bars = bars_from([(47000, 47040, 46960, 47000)] * 20)  # ATR ~80
    tp, why = tp_cap(150, 600, bars, 65, "x")  # 13 bars left -> 80*sqrt(13) ~ 288
    self.assertLess(tp, 600)
    self.assertGreaterEqual(tp, 225)  # never below 1.5x stop
    self.assertIn("停利上限", why)
    self.assertEqual(tp_cap(30, 60, bars, 200, "x")[0], 60)  # small targets untouched


class NightReportTest(unittest.TestCase):

  def test_night_report_deferred_until_morning(self):
    from futures_ai.notify import LineNotifier
    ledger = Ledger(":memory:")
    n = LineNotifier(cfg(), ledger)
    sent = []
    n._send = sent.append
    n.pending = ["02:10 Gamma：做多成交 47300"]
    n.defer_night("夜盤摘要")
    n.maybe_send_night(at(2026, 10, 17, 6, 0))
    self.assertEqual(sent, [])
    n.maybe_send_night(at(2026, 10, 17, 8, 30))
    self.assertEqual(len(sent), 1)
    self.assertNotIn("昨晚交易", sent[0])
    n.maybe_send_night(at(2026, 10, 17, 9, 0))
    self.assertEqual(len(sent), 1)  # only once


class ShadowBuildTest(unittest.TestCase):

  def test_shadows_and_twins_build(self):
    from futures_ai.shadows import build_shadows
    sh = build_shadows(cfg(), Ledger(":memory:"))
    ids = [t.ai for t in sh]
    self.assertEqual(len([i for i in ids if i.startswith("tp1-")]), 0)  # Take-profit twins retired.
    self.assertNotIn("sh1-nova-nightorb", ids)  # Retired legacy shadow.
    self.assertNotIn("dual-nova-nightorb", ids)  # Retired legacy shadow.
    self.assertIn("nova-us", ids)  # US-open-only Nova runs as a shadow.
    allday = [t for t in sh if t.ai.startswith("allday-")]
    self.assertEqual(sorted(t.ai for t in allday), ["allday-alpha-orb", "allday-nova-nightorb", "allday-sigma-vwap"])
    self.assertTrue(all(not t.s.hours for t in allday))

  def test_live_sigma_is_time_limited(self):
    from futures_ai.strategies import build_strategies
    sig = [s for s in build_strategies({}) if s.id == "sigma-vwap"][0]
    self.assertEqual(sig.hours, ("day_open", "day", "night_open", "night_pre_us", "us_open"))
    self.assertEqual(sig.bonus_hours, ("day_open", "night_pre_us"))
    self.assertEqual(sig.bonus_min_atr, 35.0)
    self.assertEqual(sig.day_min_atr, 100.0)
    self.assertEqual(sig.min_er, 0.30)
    self.assertEqual(sig.max_vwap_pts, 200.0)
    self.assertEqual(sig.limit_atr, 0.0)
    self.assertEqual(sig.limit_min, 5)
    self.assertEqual(sig.be_r, 1.75)
    self.assertEqual(sig.max_entries, 2)

  def test_live_nova_breakout_guards(self):
    from futures_ai.strategies import build_strategies
    nova = [s for s in build_strategies({}) if s.id == "nova-nightorb"][0]
    self.assertEqual(nova.limit_atr, -0.02)  # Limit 1-2 pts beyond the breakout close.
    self.assertEqual(nova.limit_min, 5)
    self.assertEqual(nova.rules.get("max_vwap_pts"), 300)
    self.assertEqual(nova.rules.get("max_sl"), 4)
    # Weekly lessons may add rules but the fixed guards stay.
    nova2 = [s for s in build_strategies({"lessons": {"nova-nightorb": {"rev_cool": 30}}})
             if s.id == "nova-nightorb"][0]
    self.assertEqual(nova2.rules, {"max_vwap_pts": 300, "max_sl": 4, "rev_cool": 30})

  def test_sigma_rejects_overextended_vwap_distance(self):
    from futures_ai.strategies import TrendPullback
    st = TrendPullback(max_vwap_pts=200.0, min_er=0.30)
    t0 = at(2026, 11, 6, 15, 0)
    # 5m uptrend bars around 39470, then a giant bar touching 39470 and closing at 39873 (+403 pts > 200 pts)
    bars = [Bar(t0 + timedelta(minutes=5 * k), 39400 + k * 20, 39430 + k * 20, 39390 + k * 20, 39420 + k * 20, 100)
            for k in range(1, 7)]
    bars_far = bars + [Bar(t0 + timedelta(minutes=35), 39500, 39880, 39460, 39873, 100)]
    self.assertIsNone(st.decide(bars_far, 0, None))
    # Same pullback bar but closing within 200 pts of VWAP (e.g. 39600, ~130 pts from VWAP) is accepted
    bars_ok = bars + [Bar(t0 + timedelta(minutes=35), 39500, 39610, 39460, 39600, 100)]
    sig = st.decide(bars_ok, 0, None)
    self.assertIsNotNone(sig)
    self.assertEqual(sig.side, "B")


class InverseOfTest(unittest.TestCase):

  def test_inverse_mirrors_actual_entries_only(self):
    from futures_ai.strategies import build_strategies, InverseOf
    from futures_ai.trader import ENTRY_BUS
    ids = [x.id for x in build_strategies({})]
    self.assertEqual(ids, ["alpha-orb", "sigma-vwap", "nova-nightorb", "omega-swing"])
    c = cfg()
    ledger = Ledger(":memory:")
    base = AiTrader(c, FakeStrategy(Entry("B", 40, 80, "base long")), LocalPaperBroker(c), ledger)
    inv_s = InverseOf(FakeStrategy(None), "inv", "Inv 反向", "#fff", 1.0, 1.5)
    inv = AiTrader(c, inv_s, LocalPaperBroker(c), ledger)
    bars = bars_from([(47000, 47030, 46970, 47000)] * 20)
    q = {"last": 47000, "bid": 46999, "ask": 47000}
    ENTRY_BUS.clear()
    inv.on_bar(at(2026, 10, 16, 9, 10), "2026-10-16", "TMFJ6", "k", bars, q, 200, True, False)
    self.assertIsNone(inv.pos)  # base has not traded -> no mirror
    base.on_bar(at(2026, 10, 16, 9, 10), "2026-10-16", "TMFJ6", "k", bars, q, 200, True, False)
    inv.on_bar(at(2026, 10, 16, 9, 10), "2026-10-16", "TMFJ6", "k", bars, q, 200, True, False)
    self.assertEqual(inv.pending and list(inv.pending.values())[0]["kind"], "ENTRY")  # limit at base's fill price
    inv.on_price(at(2026, 10, 16, 9, 11), "2026-10-16", "TMFJ6", {"last": 47001, "hi": 47002, "lo": 47000}, 200, False)
    self.assertEqual((base.pos["side"], inv.pos["side"]), ("B", "S"))
    self.assertEqual(inv.pos["entry_price"], base.pos["entry_price"])
    self.assertEqual(inv.pos["sl"] - inv.pos["entry_price"], 40)  # same stop distance, opposite side



class CloudyStrategyTest(unittest.TestCase):

  def test_fixed_100_50_bracket_not_scaled_or_capped(self):
    from futures_ai.strategies import FixedBracket, build_strategies
    c = cfg()
    ledger = Ledger(":memory:")
    fb = FixedBracket(FakeStrategy(Entry("B", 37, 222, "sigma long")), "cloudy-sigma", "Cloudy's strategy", "#fff")
    tr = AiTrader(c, fb, LocalPaperBroker(c), ledger)
    tr.brain.state["sl_mult"], tr.brain.state["tp_mult"] = 1.3, 0.7  # learning must not touch fixed brackets
    bars = bars_from([(47000, 47003, 46997, 47000)] * 20)  # tiny ATR would normally cap the target
    tr.on_bar(at(2026, 10, 16, 13, 30), "2026-10-16", "TMFJ6", "k", bars, {"last": 47000, "bid": 46999, "ask": 47000}, 100, True, False)
    p = tr.pos
    self.assertEqual((p["tp"] - p["entry_price"], p["entry_price"] - p["sl"]), (100, 50))


class ExportHistoryTest(unittest.TestCase):

  def test_export_with_fake_shioaji_is_read_only_and_futures_only(self):
    import sys, types, tempfile
    from pathlib import Path
    from futures_ai import export_history as ex
    calls = []

    class FakeKbars:
      def __init__(self):
        base = datetime(2026, 10, 15, 8, 45)
        self.ts = [int((base + timedelta(minutes=i)).replace(tzinfo=__import__("datetime").timezone.utc).timestamp() * 1e9) for i in range(40)]
        self.Open = self.High = self.Low = self.Close = [47000.0 + i for i in range(40)]
        self.Volume = [10] * 40

    class FakeApi:
      def __init__(self, simulation):
        calls.append(("init", simulation))
        self.Contracts = types.SimpleNamespace(Futures=types.SimpleNamespace(TMF=types.SimpleNamespace(TMFR1=types.SimpleNamespace(code="TMFR1"))))
      def login(self, **kw): calls.append(("login",))
      def kbars(self, contract, start, end): calls.append(("kbars", contract.code)); return FakeKbars()
      def logout(self): calls.append(("logout",))
      def __getattr__(self, name):  # Any trading / account call would fail the test.
        raise AssertionError(f"unexpected API call: {name}")

    sys.modules["shioaji"] = types.SimpleNamespace(Shioaji=FakeApi)
    try:
      with tempfile.TemporaryDirectory() as d, mock.patch.object(ex, "OUT_DIR", Path(d)), \
           mock.patch.dict("os.environ", {"SINOPAC_API_KEY": "k", "SINOPAC_SECRET_KEY": "s"}):
        out = ex.export_history(days=20, push=False, log=lambda *_: None)
        from futures_ai.wfo import sessions_from_kbars
        sess = sessions_from_kbars(out)
    finally:
      del sys.modules["shioaji"]
    self.assertIn(("init", True), calls)  # simulation environment by default
    self.assertTrue(all(c[0] in ("init", "login", "kbars", "logout") for c in calls))
    self.assertTrue(all(c[1] == "TMFR1" for c in calls if c[0] == "kbars"))
    self.assertEqual(len(sess), 1)
    self.assertEqual(sess[0].bars[0].end.strftime("%H:%M"), "08:46")  # start-labeled bars converted to end time


class LimitEntryTest(unittest.TestCase):

  def test_limit_entry_fills_on_touch_and_expires(self):
    c = cfg()
    ledger = Ledger(":memory:")
    st = FakeStrategy(Entry("B", 40, 80, "x"))
    st.limit_atr, st.limit_min = 0.5, 10
    tr = AiTrader(c, st, LocalPaperBroker(c), ledger)
    bars = bars_from([(47000, 47020, 46980, 47000)] * 20)  # ATR 40 -> limit 46980
    tr.on_bar(at(2026, 10, 16, 21, 40), "2026-10-19", "TMFJ6", "k", bars, {"last": 47000, "bid": 46999, "ask": 47001}, 200, True, False)
    self.assertIsNone(tr.pos)
    self.assertEqual(ledger.working_orders("alpha-orb")[0]["order_type"], "LMT")
    tr.on_price(at(2026, 10, 16, 21, 42), "2026-10-19", "TMFJ6", {"last": 46985, "lo": 46979, "hi": 46990}, 200, False)
    self.assertEqual(tr.pos["entry_price"], 46980)  # filled at the limit, no slippage

  def test_unfilled_limit_is_cancelled(self):
    c = cfg()
    ledger = Ledger(":memory:")
    st = FakeStrategy(Entry("B", 40, 80, "x"))
    st.limit_atr, st.limit_min = 0.5, 10
    tr = AiTrader(c, st, LocalPaperBroker(c), ledger)
    bars = bars_from([(47000, 47020, 46980, 47000)] * 20)
    tr.on_bar(at(2026, 10, 16, 21, 40), "2026-10-19", "TMFJ6", "k", bars, {"last": 47000, "bid": 46999, "ask": 47001}, 200, True, False)
    tr.on_price(at(2026, 10, 16, 21, 55), "2026-10-19", "TMFJ6", {"last": 47050}, 200, False)
    self.assertIsNone(tr.pos)
    self.assertEqual(tr.pending, {})
    self.assertEqual(ledger.rows("SELECT status FROM orders")[0]["status"], "CANCELLED")


class SwingHoldTest(unittest.TestCase):

  def test_swing_not_flattened_at_session_end(self):
    from futures_ai.strategies import SwingTrend
    c = cfg()
    ledger = Ledger(":memory:")
    sw = SwingTrend()
    tr = AiTrader(c, sw, LocalPaperBroker(c), ledger)
    tr.pos = {"side": "B", "qty": 1, "entry_price": 47000, "entry_ts": "2026-10-16T10:00:00+08:00", "contract": "TMFJ6",
              "trading_day": "2026-10-16", "session_key": "k", "sl": 46600, "tp": 50000, "sl_oid": 0, "tp_oid": 0,
              "entry_cost": 22, "reason": "x", "trail": 400, "mfe": 0, "mae": 0}
    tr.on_price(at(2026, 10, 16, 13, 41), "2026-10-16", "TMFJ6", {"last": 47100, "bid": 47099, "ask": 47101}, 4, False)
    self.assertIsNotNone(tr.pos)  # held overnight
    tr.on_price(at(2026, 10, 16, 13, 42), "2026-10-16", "TMFJ6", {"last": 44300, "bid": 44299, "ask": 44301}, 3, False)
    self.assertIsNone(tr.pos)  # stop (or margin guard) still protects it

  def test_roll_reentry_used_once(self):
    # Live 11/17: the post-roll re-entry flag stayed set after use, so the engine re-entered again (skipping the
    # ER filter) after the re-entry trade stopped out. The backtest uses it once; live must match.
    from futures_ai.strategies import SwingTrend
    sw = SwingTrend()
    t0 = at(2026, 11, 2, 8, 45)
    h = []
    for i in range(80):  # steady downtrend (EMA20 < EMA60) then a flat zig-zag: ER10 ~ 0, no fresh low
      c = 45000 - 40 * i if i < 70 else 42240 + (60 if i % 2 else 0)
      h.append(Bar(t0 + timedelta(hours=i), c, c + 30, c - 30, c, 1))
    sw.h60 = h[:79]
    sw.reentry_side = "S"
    e = sw.decide([], 0, None)
    self.assertEqual(e.side, "S")
    self.assertIn("換倉後", e.reason)
    self.assertIsNone(sw.reentry_side)
    sw.h60 = h[:80]
    self.assertIsNone(sw.decide([], 0, None))  # choppy, no breakout: no second re-entry

  def test_dropped_reentry_not_rearmed_after_restart(self):
    from futures_ai.strategies import SwingTrend
    c = cfg()
    led = Ledger(":memory:")
    led.set_state("reentry:omega-swing", "S")
    tr = AiTrader(c, SwingTrend(), LocalPaperBroker(c), led)
    self.assertEqual(tr.s.reentry_side, "S")  # restored from the ledger
    t0 = at(2026, 11, 2, 8, 45)
    h = [Bar(t0 + timedelta(hours=i), 40000 + 10 * i, 40030 + 10 * i, 39970 + 10 * i, 40000 + 10 * i, 1)
         for i in range(80)]  # uptrend: EMA20 > EMA60, so the short re-entry condition fails
    tr.s.h60 = h
    tr.on_bar(h[-1].end, "2026-11-05", "TMFL6", "2026-11-05-day", [h[-1]],
              {"last": h[-1].c, "bid": h[-1].c - 1, "ask": h[-1].c + 1}, 200, True, False)
    self.assertIsNone(tr.s.reentry_side)
    self.assertIsNone(led.get_state("reentry:omega-swing"))
    self.assertIsNone(AiTrader(c, SwingTrend(), LocalPaperBroker(c), led).s.reentry_side)  # restart: not re-armed


class NovaTest(unittest.TestCase):

  def test_night_open_breakout(self):
    from futures_ai.strategies import NightOpenBreakout
    start = at(2026, 10, 16, 15, 5)
    rng = [(47000, 47040, 46990, 47010)] * 6  # 15:05..15:30 range 46990-47040
    bars = bars_from(rng + [(47010, 47030, 47005, 47020), (47020, 47070, 47018, 47065)], start=start)
    bars = [Bar(start + timedelta(minutes=5 * i), b.o, b.h, b.l, b.c, b.v) for i, b in enumerate(bars)]
    e = NightOpenBreakout().decide(bars, 0, None)
    self.assertEqual(e.side, "B")
    self.assertEqual(e.tp_pts, e.sl_pts * 3)
    self.assertEqual(NightOpenBreakout().be_r, 1.75)
    # Exhausted move (prior bar after 15:30 already reached hi + 2.5 * width = 47040 + 125 = 47165): skip re-breakout.
    ex_bars = bars_from(rng + [(47010, 47170, 47005, 47020), (47020, 47070, 47018, 47065)], start=start)
    ex_bars = [Bar(start + timedelta(minutes=5 * i), b.o, b.h, b.l, b.c, b.v) for i, b in enumerate(ex_bars)]
    self.assertIsNone(NightOpenBreakout().decide(ex_bars, 0, None))
    day = [Bar(at(2026, 10, 16, 9, 0) + timedelta(minutes=5 * i), b.o, b.h, b.l, b.c, b.v) for i, b in enumerate(bars)]
    self.assertIsNone(NightOpenBreakout().decide(day, 0, None))  # day session: never


class NewsRiskTest(unittest.TestCase):

  def test_headline_scoring_direction(self):
    from futures_ai.news import score_headline
    self.assertGreater(score_headline("川普宣布對台灣晶片加徵關稅")[0], 0)
    self.assertLess(score_headline("川普突簽行政命令 豁免多項關稅")[0], 0)
    self.assertLess(score_headline("台股暴跌千點！國安基金擬召開臨時會進場護盤")[0], 0)
    self.assertLess(score_headline("台股重挫 金管會宣布祭出限空令救市")[0], 0)
    self.assertLess(score_headline("川普宣布暫緩對台加徵晶片關稅 台指期夜盤急拉300點")[0], 0)
    self.assertGreater(score_headline("外資台指期淨空單飆破4.5萬口 創歷史新高")[0], 0)
    self.assertGreater(score_headline("央行理監事會意外升息半碼 調升存準率1碼")[0], 0)
    self.assertGreater(score_headline("China launches missile drills near Taiwan Strait")[0], score_headline("Trump tariff threat")[0])
    self.assertEqual(score_headline("台積電法說會今日登場")[0], 0)
    self.assertEqual(score_headline("【觀察】赫格塞斯出任戰爭部長，談判陷入僵局")[0], 0)

  def test_news_tilt_needs_price_confirmation(self):
    from futures_ai import trader as tm
    c = cfg()
    tr = AiTrader(c, FakeStrategy(None), LocalPaperBroker(c), Ledger(":memory:"))
    flat = bars_from([(47000, 47030, 46970, 47000 + (i % 3) - 1) for i in range(60)])
    try:
      tm.RISK_STATE.update(mode="elevated", fomc_window=False)
      self.assertIsNone(tr._filter(Entry("B", 40, 80, "x"), flat))
      tm.RISK_STATE.update(mode="high", fomc_window=False)
      self.assertIsNone(tr._filter(Entry("B", 40, 80, "x"), flat))  # Intraday AIs already check VWAP in decide().
      tm.RISK_STATE.update(mode="normal", fomc_window=True)
      self.assertIn("聯準會", tr._filter(Entry("S", 40, 80, "x"), flat))
    finally:
      tm.RISK_STATE.update(mode="normal", fomc_window=False)


class AlertTest(unittest.TestCase):

  def test_followed_ais_alert_instantly(self):
    from futures_ai.notify import LineNotifier
    c = cfg()
    c.setdefault("notify", {})["alert_ais"] = ["alpha-orb", "nova-us"]
    ledger = Ledger(":memory:")
    n = LineNotifier(c, ledger)
    sent = []
    n._send = sent.append
    ledger.log("📣 訊號：做多限價 47000", "alpha-orb", "SIGNAL")
    ledger.log("做多成交 47000", "sigma-vwap", "TRADE")
    ledger.log("放空成交 47000", "nova-us", "TRADE")
    n.collect({})
    ledger.log("📣 訊號：放空", "nova-us", "SIGNAL")
    n.collect({})
    self.assertEqual(len(sent), 2)  # Signals only; fills / exits are not pushed.
    self.assertIn("訊號", sent[0])
    self.assertIn("nova-us", sent[1])

  def test_default_alerts_omega_only_with_stop_and_target(self):
    from futures_ai.notify import LineNotifier
    c = cfg()  # Built from DEFAULTS.
    self.assertEqual(c["notify"]["alert_ais"], ["omega-swing"])
    ledger = Ledger(":memory:")
    n = LineNotifier(c, ledger)
    sent = []
    n._send = sent.append
    for ai in ("alpha-orb", "sigma-vwap", "nova-nightorb"):
      ledger.log("📣 訊號：做多｜進場（限價）：41000｜停損：40900（100 點）｜停利：41300（300 點）", ai, "SIGNAL")
    ledger.log("📣 訊號：放空｜進場（限價）：41084（60 分鐘內有效）｜停損：42284（1200 點）｜停利：36165（4919 點）",
               "omega-swing", "SIGNAL")
    n.collect({})
    self.assertEqual(len(sent), 1)
    self.assertIn("omega-swing", sent[0])
    self.assertIn("停損：42284", sent[0])
    self.assertIn("停利：36165", sent[0])


class ReplayMatchTest(unittest.TestCase):

  def test_match_pairs_same_side_within_window(self):
    from futures_ai.replay import _match
    live = [{"entry": at(2026, 10, 20, 22, 1), "side": "B", "pnl": 1, "kind": "TP"},
            {"entry": at(2026, 10, 20, 23, 0), "side": "S", "pnl": -1, "kind": "SL"}]
    sim = [{"entry": at(2026, 10, 20, 22, 2), "side": "B", "pnl": 1, "kind": "TP"},
           {"entry": at(2026, 10, 21, 1, 0), "side": "S", "pnl": -1, "kind": "SL"}]
    both, lo, so = _match(live, sim)
    self.assertEqual((len(both), len(lo), len(so)), (1, 1, 1))

  def test_rule_periods_follow_live_versions(self):
    from pathlib import Path
    from futures_ai.replay import RULE_VERSIONS, rule_periods
    from futures_ai.shadows import OLD_PARAMS
    none = Path("/nonexistent/param_versions.json")  # No weekly switches yet.
    ps = rule_periods(at(2026, 10, 19, 8, 45), path=none)
    self.assertEqual(len(ps), len(RULE_VERSIONS) + 1)
    self.assertIsNone(ps[0][3])  # Legacy specs before the 11/08 upgrade.
    self.assertEqual(ps[0][1], at(2026, 11, 8, 0, 3, 52))
    self.assertIsNone(ps[-1][1])
    for (a, b, _l, _o), (c, _d, _m, _p) in zip(ps, ps[1:]):
      self.assertEqual(b, c)  # Contiguous, no gaps.
    from futures_ai.replay import PROTECTED_ON
    on = datetime.fromisoformat(PROTECTED_ON)
    self.assertIn(on, [a for a, _b, _l, _o in ps])  # protected_not_stop switch is a period start
    self.assertEqual([a >= on for a, _b, _l, _o in ps[-2:]], [False, True])
    late = rule_periods(at(2026, 11, 11, 15, 0), path=none)  # Starting inside a version keeps its rules.
    self.assertEqual(late[0][2], RULE_VERSIONS[2][1])
    # Current rules after the switch, with the parameters that were live at the switch (no back-fill).
    nv = OLD_PARAMS["nova-nightorb"]
    self.assertEqual(late[-1][3], {"alpha-orb": OLD_PARAMS["alpha-orb"], "sigma-vwap": OLD_PARAMS["sigma-vwap"],
                                   "nova-nightorb": nv, "nova-us": nv})

  def test_old_versions_disable_later_rules(self):
    from futures_ai.replay import RULE_VERSIONS, sim_specs
    p_now = sim_specs()["nova-nightorb"][1]
    self.assertEqual((p_now["limit_atr"], p_now["max_sl"]), (-0.02, 4))
    v1 = {**p_now, **RULE_VERSIONS[0][2]["nova-nightorb"]}
    self.assertEqual((v1["limit_atr"], v1["max_sl"], v1["be_r"]), (0.0, None, None))
    sg = {**sim_specs()["sigma-vwap"][1], **RULE_VERSIONS[0][2]["sigma-vwap"]}
    self.assertEqual((sg["limit_atr"], sg["min_er"], sg["max_vwap_pts"]), (0.2, None, None))
    self.assertIsNone(sg["max_sl"])  # Lesson rules were only loaded by the 11/11 13:46 restart.
    self.assertIsNone(sg["rev_cool"])
    al = {**sim_specs()["alpha-orb"][1], **RULE_VERSIONS[1][2]["alpha-orb"]}
    self.assertIsNone(al["rev_cool"])
    self.assertNotIn("rev_cool", RULE_VERSIONS[2][2]["alpha-orb"])  # From 11/11 13:46 the lesson rules apply.
    # Before the 11/18 switch every version replays the old stop cap / no night filter / old Sigma stop.
    for _ts, _lab, ov in RULE_VERSIONS[:-2]:  # last two: 11/18 switch and protected_not_stop (no overrides)
      self.assertEqual(ov["alpha-orb"]["sl_cap"], 120)
      self.assertIsNone(ov["sigma-vwap"]["night_eff"])
      self.assertEqual(ov["sigma-vwap"]["sl_atr"], 2.0)
      self.assertIsNone(ov["nova-nightorb"]["night_eff"])
    self.assertEqual(RULE_VERSIONS[-1][2], {})
    self.assertEqual(RULE_VERSIONS[-2][2], {})
    now = sim_specs()
    self.assertEqual(now["alpha-orb"][1]["sl_cap"], 160)
    self.assertEqual(now["sigma-vwap"][1]["night_eff"], (5, 0.40))
    self.assertEqual(now["nova-nightorb"][1]["night_eff"], (5, 0.40))

  def test_summary_shows_fair_and_hindsight_columns(self):
    from futures_ai.replay import summary_lines
    rep = {"ais": [{"id": "sigma-vwap", "name": "Sigma 趨勢回測 AI", "live_pnl": 9125, "sim_pnl": 6957,
                    "sim_now_pnl": 43473}]}
    txt = "\n".join(summary_lines(rep))
    self.assertIn("回測（當時規則）", txt)
    self.assertIn("事後套用", txt)
    self.assertIn("Sigma｜+9,125｜+6,957｜+43,473", txt)


def _swing_sessions():
  """20 day sessions: steady uptrend (2 pts/min), then a sharp drop on the last day to hit the trailing stop."""
  from futures_ai.wfo import Session
  out, px = [], 40000.0
  for k in range(20):
    d = date(2026, 9, 1) + timedelta(days=k)
    start = datetime(d.year, d.month, d.day, 8, 45, tzinfo=TPE)
    bars = []
    for i in range(300):
      step = -12.0 if (k == 19 and i >= 120) else 2.0
      o, c = px, px + step
      bars.append(Bar(start + timedelta(minutes=i + 1), o, max(o, c) + 3, min(o, c) - 3, c, 10))
      px = c
    out.append(Session(d, "day", "TMFU6", bars, datetime(d.year, d.month, d.day, 13, 45, tzinfo=TPE)))
  return out


class StopSlipTest(unittest.TestCase):
  P = dict(lookback=10, atr_k=3.0, direction="both", entry="market", tp_atr=None, min_er=None, be_r=None,
           max_ext_atr=None, max_sl_pts=None)

  def test_swing_sl_slip_worsens_stop_fill_only(self):
    from futures_ai.swing import simulate_swing
    c = cfg()
    sess = _swing_sessions()
    base = simulate_swing(c, dict(self.P), sess, record=True)
    same = simulate_swing(c, dict(self.P, sl_slip=1), sess, record=True)
    worse = simulate_swing(c, dict(self.P, sl_slip=10), sess, record=True)
    self.assertEqual(base["pnl"], same["pnl"])  # Default = 1 point, unchanged behaviour.
    stops = [t for t in base["log"] if t[6] == "trail_stop"]
    self.assertGreaterEqual(len(stops), 1)
    self.assertEqual(base["trades"], worse["trades"])
    self.assertAlmostEqual(base["pnl"] - worse["pnl"], 9 * c["point_value"] * len(stops), delta=2 * len(stops))

  def test_swing_schedule_switches_params_by_session(self):
    from futures_ai.swing import simulate_swing
    c = cfg()
    sess = _swing_sessions()
    slip10 = dict(self.P, sl_slip=10)
    direct = simulate_swing(c, slip10, sess)
    early = simulate_swing(c, dict(self.P), sess, schedule=[(at(2026, 8, 1, 0, 0), slip10)])
    late = simulate_swing(c, dict(self.P), sess, schedule=[(at(2026, 12, 1, 0, 0), slip10)])
    self.assertEqual(direct["pnl"], early["pnl"])
    self.assertEqual(simulate_swing(c, dict(self.P), sess)["pnl"], late["pnl"])


class SessionReportTest(unittest.TestCase):

  def test_report_uses_session_pnl_not_cumulative(self):
    from futures_ai.notify import summary_text
    st = {"curve_session": {"open": "2026-10-21T08:45:00+08:00", "close": "2026-10-21T13:45:00+08:00"},
          "ais": [{"name": "Alpha 開盤突破 AI", "session_pnl": 0, "today_pnl": 2438, "total_pnl": 2822}],
          "combined": {"session_pnl": 0, "today_pnl": 2438, "total_pnl": 14273}, "news": None}
    txt = summary_text(st, "日盤")
    self.assertIn("本盤合計：+0", txt)
    self.assertNotIn("2,438", txt)
    self.assertNotIn("累計", txt)


class NewsClusterTest(unittest.TestCase):

  def test_same_story_counted_once(self):
    from futures_ai.news import cluster_stories, score_headline
    titles = ["川普：俄烏戰爭距終戰可能已到最近時刻 - TVBS新聞網", "俄烏戰爭距終戰！川普發聲：可能已到最近時刻 - 三立新聞",
              "期中選後再轟伊朗？美以已演練空襲 川普尚未拍板 - 經濟日報"]
    items = [{"title": t, "score": 2.0} for t in titles]
    out = cluster_stories(items)
    self.assertEqual(len(out), 2)
    self.assertEqual(max(o["n"] for o in out), 2)
    self.assertLess(score_headline("川普：近達成協議不會施加制裁")[0], 0)  # Negated sanctions = relief.


class AlertWindowTest(unittest.TestCase):

  def test_nova_alert_only_in_us_open_hour(self):
    from futures_ai.notify import LineNotifier
    c = cfg()
    c.setdefault("notify", {}).update(alert_ais=["nova-nightorb"], alert_windows={"nova-nightorb": ["us_open"]})
    ledger = Ledger(":memory:")
    n = LineNotifier(c, ledger)
    sent = []
    n._send = sent.append
    ledger.log("📣 訊號：做多", "nova-nightorb", "SIGNAL", at(2026, 10, 22, 16, 0))  # Night open: no alert.
    ledger.log("📣 訊號：放空", "nova-nightorb", "SIGNAL", at(2026, 10, 22, 21, 40))  # US-open hour (DST).
    n.collect({})
    self.assertEqual(len(sent), 1)
    self.assertIn("放空", sent[0])


class FuturesOnlyGuardTest(unittest.TestCase):

  def test_refuses_key_that_sees_stock_account(self):
    from futures_ai.brokers import assert_futures_only

    class Api:
      def __init__(self, stock):
        self.stock_account, self.futopt_account = stock, "F-123"
      def logout(self):
        pass
    c = cfg()
    c["sinopac"] = {"require_futures_only": True}
    with self.assertRaises(SystemExit):
      assert_futures_only(Api("S-999"), c)
    assert_futures_only(Api(None), c)  # Futures-only key: allowed.
    assert_futures_only(Api("S-999"), c, strict=False)  # Simulation: warning only.


class MacSimConfigTest(unittest.TestCase):

  def test_run_only_builds_nova_us_with_sinopac_broker_selection(self):
    from futures_ai.cli import build_traders
    from futures_ai.config import load_config
    c = load_config(str(Path(__file__).resolve().parents[1] / "config" / "mac_sim.json"))
    self.assertEqual((c["mode"], c["broker_ai"], c["run_only"]), ("sinopac_sim", "nova-nightorb", ["nova-nightorb"]))
    self.assertIsNone(c["stop_at"])  # The user stops it by hand (Ctrl+C flattens first).
    c.update(mode="local_paper", db_path=":memory:")
    tr = build_traders(c, Ledger(":memory:"))
    self.assertEqual([t.ai for t in tr], ["nova-nightorb"])


class HolidayReplayTest(unittest.TestCase):

  def test_previous_night_chart_is_not_traded(self):
    from futures_ai.engine import Engine
    from futures_ai.feed import Snapshot

    class Feed:
      def __init__(self, snap):
        self.snap = snap
      def snapshot(self, code, name, start, now):
        return self.snap
    now = at(2026, 10, 23, 22, 25)  # Typhoon day: no night session; exchange still serves 10/22's chart.
    bars = bars_from([(46300, 46310, 46290, 46300)] * 30)
    stale = Snapshot("TMFK6", 45671.0, None, None, bars, now, None, "20261022")
    e = Engine(cfg(), Ledger(":memory:"), [], feed=Feed(stale), clock=lambda: now)
    self.assertEqual(e.step(), "holiday")
    ok = Snapshot("TMFK6", 45671.0, None, None, bars, now, None, "20261023")
    self.assertNotEqual(Engine(cfg(), Ledger(":memory:"), [], feed=Feed(ok), clock=lambda: now).step(), "holiday")


class HolidayLatchTest(unittest.TestCase):

  def test_holiday_latched_after_ten_minutes(self):
    from futures_ai.engine import Engine
    from futures_ai.feed import Snapshot

    class Feed:
      snap = None
      def snapshot(self, code, name, start, now):
        return self.snap
    f = Feed()
    clock = {"t": at(2026, 10, 27, 15, 20)}
    bars = bars_from([(46300, 46310, 46290, 46300)] * 30)
    e = Engine(cfg(), Ledger(":memory:"), [], feed=f, clock=lambda: clock["t"])
    f.snap = Snapshot("TMFK6", 45671.0, None, None, bars, clock["t"], None, "20261022")
    self.assertEqual(e.step(), "holiday")
    f.snap = Snapshot("TMFK6", 45671.0, None, None, bars, clock["t"], None, None)  # Fallback quote, no date.
    self.assertEqual(e.step(), "holiday")


class SinopacStatusTest(unittest.TestCase):

  def test_round_trips_from_deals(self):
    import io, contextlib, sys, types
    from futures_ai import sinopac_setup as m

    class D:
      def __init__(self, price, ts):
        self.price, self.quantity, self.ts = price, 1, ts
    class T:
      def __init__(self, action, price, ts):
        self.order = types.SimpleNamespace(action=action)
        self.status = types.SimpleNamespace(deals=[D(price, ts)], order_datetime=None)
    class Api:
      futopt_account, stock_account = "F", None
      def list_positions(self, a): return []
      def update_status(self, a): pass
      def list_trades(self):
        return [T("Action.Sell", 46327, 1793000000), T("Action.Buy", 46490, 1793002000),
                T("Action.Buy", 46533, 1793003000), T("Action.Sell", 46382, 1793004000)]
      def logout(self): pass
    fake = types.ModuleType("shioaji")
    old = sys.modules.get("shioaji")
    sys.modules["shioaji"] = fake
    orig = m.__dict__.get("_login")
    import futures_ai.export_history as eh
    saved = eh._login
    eh._login = lambda *a, **k: Api()
    os.environ.setdefault("SINOPAC_API_KEY", "x"); os.environ.setdefault("SINOPAC_SECRET_KEY", "x")
    buf = io.StringIO()
    try:
      c = cfg(); c["sinopac"] = {"api_key_env": "SINOPAC_API_KEY", "secret_key_env": "SINOPAC_SECRET_KEY"}
      with contextlib.redirect_stdout(buf):
        m.sinopac_status(c)
    finally:
      eh._login = saved
      if old is not None: sys.modules["shioaji"] = old
      else: del sys.modules["shioaji"]
    out = buf.getvalue()
    self.assertIn("合計 2 筆，勝 0 筆", out)
    self.assertIn("-163 點", out)


class MonthlyTest(unittest.TestCase):

  def test_last_session_of_month(self):
    from futures_ai.monthly import month_closes_after
    self.assertTrue(month_closes_after(at(2026, 10, 30, 15, 0)))  # Fri night -> next session Mon 11/2.
    self.assertFalse(month_closes_after(at(2026, 10, 30, 8, 45)))  # Fri day: the night session follows.
    self.assertFalse(month_closes_after(at(2026, 10, 29, 15, 0)))  # Thu night -> Fri 10/30.
    self.assertTrue(month_closes_after(at(2026, 9, 30, 15, 0)))  # Wed night -> Thu 10/1.


class SessionCloseReportTest(unittest.TestCase):

  def test_reports_sent_when_session_closes(self):
    import futures_ai.engine as E
    import futures_ai.notify as N
    import time as _t
    from futures_ai.cli import build_traders
    c = cfg()
    c.update(control_enabled=False, archive_history=False, stock_reports=False)
    l = Ledger(":memory:")
    times = iter([at(2026, 10, 31, 4, 59, 50)] * 3 + [at(2026, 10, 31, 5, 0, 5)] * 50)
    sent = []
    orig_send, orig_sleep = N.LineNotifier._send, _t.sleep
    N.LineNotifier._send = lambda self, t: sent.append(t)

    class Feed:
      def snapshot(self, *a):
        raise RuntimeError("offline")

    def stop(_s):
      raise KeyboardInterrupt
    e = E.Engine(c, l, build_traders(c, l), feed=Feed(), clock=lambda: next(times))
    e.refresh_margin = lambda: None
    e.shutdown = lambda kind: None
    _t.sleep = stop
    try:
      with self.assertRaises(SystemExit):
        e.run_forever()
    finally:
      N.LineNotifier._send, _t.sleep = orig_send, orig_sleep
    self.assertEqual(l.get_state("monthly_sent"), "2026-10")
    self.assertTrue(any("10 月月報" in x for x in (l.get_state("monthly_queue") or [])))
    self.assertTrue(l.get_state("night_report"))


class GapRangeTest(unittest.TestCase):

  def test_bars_missed_during_outage_count_for_stops(self):
    from futures_ai.engine import Engine
    from futures_ai.feed import Snapshot
    from futures_ai.trader import Bar
    e = Engine(cfg(), Ledger(":memory:"), [])
    t = at(2026, 11, 4, 21, 0)
    e.prev_live = (t, 47010, 46990)
    gap = [Bar(t + timedelta(minutes=k), 47000, 47020, 46800 if k == 3 else 46980, 47000, 1) for k in range(0, 6)]
    live = Bar(t + timedelta(minutes=6), 47000, 47005, 46995, 47000, 1)
    hi, lo = e._touched_range(Snapshot("TMFK6", 47000.0, None, None, gap, t, live, None))
    self.assertEqual(lo, 46800)  # The dip that happened while offline is seen.


class LessonReviewTest(unittest.TestCase):
  def test_tags_whipsaw_and_consecutive_stops(self):
    from futures_ai import lessons
    t = lambda h, m: f"2026-11-06T{h:02d}:{m:02d}:00+08:00"
    rows = [
        {"entry_ts": t(15, 5), "exit_ts": t(15, 20), "side": "S", "pnl_twd": -1500, "exit_kind": "SL"},  # early (Nova)
        {"entry_ts": t(15, 30), "exit_ts": t(16, 0), "side": "B", "pnl_twd": -1500, "exit_kind": "SL"},  # reversal + consec + early
        {"entry_ts": t(17, 30), "exit_ts": t(18, 0), "side": "B", "pnl_twd": -1500, "exit_kind": "SL"},  # consec
        {"entry_ts": t(19, 0), "exit_ts": t(20, 0), "side": "B", "pnl_twd": 5000, "exit_kind": "TP"},
    ]
    r = lessons.review(rows, "nova-nightorb")
    self.assertEqual(r["losses"], 3)
    self.assertEqual(r["tags"], {"consec_sl": 2, "reversal": 1, "early_open": 2, "giveback": 0,
                                 "overextended": 0, "weak_trend": 0, "night_chop": 0, "noise_stop": 0,
                                 "other": 0})

  def test_tags_giveback_overextended_and_weak_trend(self):
    from futures_ai import lessons
    t = lambda h, m: f"2026-11-06T{h:02d}:{m:02d}:00+08:00"
    rows = [
        {"entry_ts": t(9, 47), "exit_ts": t(10, 50), "side": "S", "pnl_twd": -16610, "exit_kind": "SL",
         "entry_reason": "[盤整盤 ER 0.14｜波段順勢] 60 分K 收盤 38559 跌破前 10 根低點 38792，EMA20 40761 < EMA60 42498｜移動停損 2375 點"},
    ]
    journal = [{"entry_ts": t(9, 47), "mfe": 500, "sl_dist": 200, "tp_dist": 800, "regime": "chop"}]
    r = lessons.review(rows, "omega-swing", journal=journal)
    self.assertEqual(r["tags"]["giveback"], 1)
    self.assertEqual(r["tags"]["overextended"], 1)
    self.assertEqual(r["tags"]["weak_trend"], 1)
    self.assertEqual(r["tags"]["other"], 0)

  def test_recurring_needs_two_weeks(self):
    from futures_ai import lessons
    wk = lambda n: {"tags": {"consec_sl": n, "reversal": 0, "early_open": 0, "other": 0}}
    self.assertEqual(lessons.recurring([wk(5), wk(0), wk(0), wk(0)]), [])
    self.assertEqual(lessons.recurring([wk(3), wk(1), wk(0), wk(0)]), ["consec_sl"])
    self.assertEqual(lessons.recurring([wk(1), wk(1), wk(0), wk(0)], min_total=2), ["consec_sl"])

  def test_rule_text(self):
    from futures_ai import lessons
    self.assertEqual(lessons.rule_zh({"max_sl": 1}), "一盤停損 1 次就休息")
    self.assertIn("單筆停損上限 1200 點", lessons.rule_zh({"max_ext_atr": 3.5, "max_sl_pts": 1200}))
    self.assertIn("距均價 ≤ 200 點才進場", lessons.rule_zh({"max_vwap_pts": 200}))


class WatchdogTest(unittest.TestCase):

  def test_problems_only_in_session_after_grace(self):
    from futures_ai.watchdog import problems
    sat = at(2026, 11, 14, 12, 0)
    self.assertEqual(problems(sat, False, None, 50.0), {})  # Market closed: no engine alerts.
    self.assertIn("disk", problems(sat, True, None, 1.0))
    early = at(2026, 11, 13, 8, 50)  # 5 minutes into the day session: warm-up grace.
    self.assertEqual(problems(early, False, None, 50.0), {})
    mid = at(2026, 11, 13, 10, 0)
    self.assertIn("engine", problems(mid, False, None, 50.0))
    self.assertEqual(problems(mid, True, mid - timedelta(minutes=2), 50.0), {})
    self.assertIn("heartbeat", problems(mid, True, mid - timedelta(minutes=25), 50.0))

  def test_run_dedups_and_reports_recovery(self):
    import tempfile
    from futures_ai import watchdog
    sent = []
    with tempfile.TemporaryDirectory() as d, \
        mock.patch.object(watchdog, "STATE", Path(d) / "wd.json"), \
        mock.patch.object(watchdog, "engine_running", return_value=False), \
        mock.patch.object(watchdog, "last_heartbeat", return_value=None):
      c = cfg()
      t = at(2026, 11, 13, 10, 0)
      watchdog.run(c, now=t, send=sent.append)
      watchdog.run(c, now=t + timedelta(minutes=5), send=sent.append)  # Same problem: no repeat yet.
      self.assertEqual(len(sent), 1)
      watchdog.run(c, now=t + timedelta(minutes=31), send=sent.append)
      self.assertEqual(len(sent), 2)
      with mock.patch.object(watchdog, "engine_running", return_value=True), \
          mock.patch.object(watchdog, "last_heartbeat", return_value=t + timedelta(minutes=35)):
        watchdog.run(c, now=t + timedelta(minutes=36), send=sent.append)
      self.assertIn("恢復", sent[-1])
      n = len(sent)
      watchdog.run(c, now=t + timedelta(minutes=40), send=sent.append)  # Re-arm the engine alert.
      watchdog.run(c, now=at(2026, 11, 13, 14, 0), send=sent.append)  # After the close: no false recovery.
      self.assertTrue(all("恢復" not in m for m in sent[n + 1:]))

  def test_disk_alert_rate_limited_with_hysteresis(self):
    import tempfile
    from futures_ai import watchdog
    sent = []
    usage = lambda gb: mock.Mock(free=gb * 1e9)
    with tempfile.TemporaryDirectory() as d, \
        mock.patch.object(watchdog, "STATE", Path(d) / "wd.json"), \
        mock.patch.object(watchdog, "engine_running", return_value=True), \
        mock.patch.object(watchdog, "last_heartbeat", return_value=None):
      c = cfg()
      t = at(2026, 11, 14, 12, 0)  # Saturday: only the disk check applies.
      with mock.patch("shutil.disk_usage", return_value=usage(2.0)):
        watchdog.run(c, now=t, send=sent.append)
        watchdog.run(c, now=t + timedelta(hours=1), send=sent.append)
      self.assertEqual(len(sent), 1)  # Disk alerts repeat at most every 6 hours.
      with mock.patch("shutil.disk_usage", return_value=usage(3.5)):  # Above 3 GB but below 3 + 1 GB.
        watchdog.run(c, now=t + timedelta(hours=2), send=sent.append)
      self.assertEqual(len(sent), 1)
      with mock.patch("shutil.disk_usage", return_value=usage(5.0)):
        watchdog.run(c, now=t + timedelta(hours=3), send=sent.append)
      self.assertIn("磁碟空間已恢復", sent[-1])

  def test_state_write_failure_does_not_crash(self):
    from futures_ai import watchdog
    bad = mock.Mock()
    bad.exists.return_value = False
    bad.write_text.side_effect = OSError(28, "No space left on device")
    with mock.patch.object(watchdog, "STATE", bad), \
        mock.patch.object(watchdog, "engine_running", return_value=False), \
        mock.patch.object(watchdog, "last_heartbeat", return_value=None):
      msgs = watchdog.run(cfg(), now=at(2026, 11, 13, 10, 0), send=lambda m: None)
    self.assertEqual(len(msgs), 1)


class WeeklyPolicyTest(unittest.TestCase):

  def test_health_lines_handles_omega_and_shows_haircut(self):
    from futures_ai import weekly
    fake = {"pnl": 1000.0, "maxdd": 100.0, "trades": 3, "wins": 1}
    with mock.patch("futures_ai.wfo.simulate", side_effect=lambda c, ai, p, s, record=False: (
        (_ for _ in ()).throw(ValueError(ai)) if ai == "omega-swing" else fake)), \
        mock.patch("futures_ai.swing.simulate_swing", return_value=fake):
      out = weekly.health_lines(cfg(), [])
    txt = "\n".join(out)
    self.assertIn("Omega：+1,000", txt)
    self.assertIn("×0.7 ≈ +700", txt)
    self.assertIn("Omega 壓測（停損滑價 47 點", txt)

  def test_slippage_lines_flag_only_with_enough_samples(self):
    from futures_ai.ledger import Ledger
    from futures_ai.slippage import slippage_lines, stats
    self.assertEqual(stats([1, 3])[0:2], (2, 2.0))
    lg = Ledger(":memory:")
    def stop(ai, side, trig, fill):
      cols = "ai, ts, trading_day, contract, kind, order_type, side, qty, trigger_price, fill_price, status, broker"
      lg.conn.execute(f"INSERT INTO orders({cols}) VALUES(?, '2026-11-01T10:00:00+08:00', '2026-11-01', 'TMFX6', "
                      "'SL', 'STOP', ?, 1, ?, NULL, 'TRIGGERED', 'local_paper')", (ai, side, trig))
      lg.conn.execute(f"INSERT INTO orders({cols}) VALUES(?, '2026-11-01T10:00:00+08:00', '2026-11-01', 'TMFX6', "
                      "'SL', 'MKT', ?, 1, NULL, ?, 'FILLED', 'local_paper')", (ai, side, fill))
    for _ in range(30):
      stop("sigma-vwap", "B", 100, 109)  # Buy-to-cover 9 points above the trigger.
    for _ in range(5):
      stop("alpha-orb", "S", 100, 96)
    txt = "\n".join(slippage_lines(lg, cfg()))
    self.assertIn("Sigma：30 筆｜平均 9.0", txt)
    self.assertIn("建議調整為 9", txt)
    self.assertIn("Alpha：5 筆｜平均 4.0", txt)
    self.assertIn("樣本不足", txt)
    self.assertIn("Nova：尚無停損", txt)

  def test_expectation_lines_per_20_days(self):
    from futures_ai.weekly import expectation_lines
    txt = "\n".join(expectation_lines({"sigma-vwap": (84147, 11, 20), "omega-swing": (276212, 10, 20)}))
    self.assertIn("Sigma ≈ +7,650", txt)
    self.assertIn("Omega ≈ +27,621", txt)
    self.assertIn("10 個交易日", txt)

  def test_propose_rejects_unsupported(self):
    from futures_ai import lessons
    self.assertFalse(lessons.propose(cfg(), "omega-swing", {"max_sl": 2}, sessions=[])["ok"])
    self.assertFalse(lessons.propose(cfg(), "sigma-vwap", {"sl_atr": 1.5}, sessions=[])["ok"])
    self.assertFalse(lessons.propose(cfg(), "sigma-vwap", {"max_sl": "2"}, sessions=[])["ok"])

  def test_propose_blocks_recently_rejected_and_active_rules(self):
    import json as _json
    import tempfile
    from futures_ai import lessons
    with tempfile.TemporaryDirectory() as d:
      state, params = Path(d) / "lessons.json", Path(d) / "params.json"
      params.write_text(_json.dumps({"lessons": {"sigma-vwap": {"max_sl": 2}}}), encoding="utf-8")
      base = _json.loads(_json.dumps(lessons._specs({"sigma-vwap": {"max_sl": 2}})["sigma-vwap"][1], sort_keys=True))
      state.write_text(_json.dumps({"trials": {}, "adopted": [], "rejected": [
          {"ai": "sigma-vwap", "rule": {"min_er": 0.25}, "at": "2026-11-14", "base": base}]}), encoding="utf-8")
      with mock.patch.object(lessons, "STATE", state), mock.patch.object(lessons, "PARAMS", params):
        r1 = lessons.propose(cfg(), "sigma-vwap", {"max_sl": 2}, sessions=[], now=at(2026, 11, 15, 10, 0))
        r2 = lessons.propose(cfg(), "sigma-vwap", {"min_er": 0.25}, sessions=[], now=at(2026, 11, 15, 10, 0))
    self.assertFalse(r1["ok"])
    self.assertIn("已經在用", r1["lines"][0])
    self.assertFalse(r2["ok"])
    self.assertIn("已被淘汰", r2["lines"][0])

  def test_sigma_backtest_uses_measured_stop_slippage(self):
    c = cfg()
    self.assertEqual(c["sim_sl_slip_pts"], {"sigma-pullback": 5, "anchor-orb": 2})  # Others keep 1.

  def test_nova_us_sim_matches_live_shadow(self):
    from futures_ai.replay import sim_specs
    from futures_ai.strategies import build_nova_us
    p = sim_specs()["nova-us"][1]
    live = build_nova_us()
    self.assertEqual((tuple(p["hours"]), p["dst_skip"], p["be_r"]), (tuple(live.hours), live.dst_skip, live.be_r))


class ProtectedExitTest(unittest.TestCase):

  def test_classification_shared_by_live_and_sim(self):
    from futures_ai import trader, wfo
    self.assertIs(wfo.is_protected_exit, trader.is_protected_exit)  # One definition for both.
    f = trader.is_protected_exit
    self.assertTrue(f("B", 44000, 44006, 44001))    # Breakeven stop +6, filled 1 point above entry.
    self.assertTrue(f("B", 44000, 44006, 43990))    # Slipped 16 points: still protected (fill >= entry - 30).
    self.assertFalse(f("B", 44000, 44006, 43960))   # Fill 40 points below entry: counted as a stop.
    self.assertFalse(f("B", 44000, 43850, 43845))   # Initial stop below entry: a real stop.
    self.assertTrue(f("S", 44000, 43994, 44005))
    self.assertFalse(f("S", 44000, 44150, 44152))

  def _trader(self, protected):
    c = cfg(protected_not_stop=protected)
    ledger = Ledger(":memory:")
    st = FakeStrategy(None)
    st.rules = {"max_sl": 2, "rev_cool": 30}
    tr = AiTrader(c, st, LocalPaperBroker(c), ledger)
    def add(ts, side, pnl, pts):
      ledger.add_trade(ai="alpha-orb", trading_day="2026-11-17", contract="TMFX6", side=side, qty=1,
                       entry_ts=ts, entry_price=44000, exit_ts=ts, exit_price=44000 + pts, points=pts,
                       gross_twd=pts * 10, cost_twd=42, pnl_twd=pnl, exit_kind="SL", entry_reason="x")
    add("2026-11-17T09:10:00+08:00", "B", -1552, -151)  # Real stop.
    add("2026-11-17T09:40:00+08:00", "B", -32, 1)        # Breakeven exit with 5 points slippage.
    ledger.set_state("protected_exits:alpha-orb", ["2026-11-17T09:40:00+08:00"])
    return tr

  def test_lesson_filter_skips_protected_exits_only_in_protected_mode(self):
    from types import SimpleNamespace
    bars = bars_from([(44000, 44005, 43995, 44000)] * 40, start=at(2026, 11, 17, 9, 0))
    sig_b = SimpleNamespace(side="B")
    self.assertIn("連續停損 2 次", self._trader(False)._lesson_filter(sig_b, bars))  # Live today.
    self.assertIsNone(self._trader(True)._lesson_filter(sig_b, bars))  # Only 1 real stop; 09:10 + 30 < 09:40.

  def test_winning_protected_exit_still_resets_like_sim(self):
    from types import SimpleNamespace
    c = cfg(protected_not_stop=True)
    ledger = Ledger(":memory:")
    st = FakeStrategy(None)
    st.rules = {"max_sl": 2}
    tr = AiTrader(c, st, LocalPaperBroker(c), ledger)
    for ts, pnl, pts in (("09:10", -1552, -151), ("09:40", 8, 5), ("10:10", -1552, -151)):
      t = f"2026-11-17T{ts}:00+08:00"
      ledger.add_trade(ai="alpha-orb", trading_day="2026-11-17", contract="TMFX6", side="B", qty=1, entry_ts=t,
                       entry_price=44000, exit_ts=t, exit_price=44000 + pts, points=pts, gross_twd=pts * 10,
                       cost_twd=42, pnl_twd=pnl, exit_kind="SL", entry_reason="x")
    ledger.set_state("protected_exits:alpha-orb", ["2026-11-17T09:40:00+08:00"])
    bars = bars_from([(44000, 44005, 43995, 44000)] * 90, start=at(2026, 11, 17, 9, 0))
    self.assertIsNone(tr._lesson_filter(SimpleNamespace(side="B"), bars))  # Consecutive real stops = 1 (sim: 1).
    from futures_ai import lessons
    rows = [dict(r) for r in ledger.rows("SELECT * FROM trades ORDER BY id")]
    r = lessons.review(rows, "alpha-orb", protected={"2026-11-17T09:40:00+08:00"})
    self.assertEqual(r["tags"]["consec_sl"], 0)

  def test_block_after_stop_skipped_for_protected_exit(self):
    c = cfg(protected_not_stop=True)
    ledger = Ledger(":memory:")
    st = FakeStrategy(None)
    st.block_after_stop = True
    tr = AiTrader(c, st, LocalPaperBroker(c), ledger)
    tr.pos = {"side": "B", "qty": 1, "entry_price": 44000, "entry_ts": "2026-11-17T09:00:00+08:00",
              "contract": "TMFX6", "trading_day": "2026-11-17", "session_key": "k", "sl": 44006, "tp": 44600,
              "sl_oid": 0, "tp_oid": 0, "entry_cost": 21, "reason": "x", "mfe": 300, "mae": 0}
    tr._close(at(2026, 11, 17, 9, 30), {"kind": "SL", "code": "TMFX6"}, 44001)
    self.assertIsNone(ledger.get_state("block:alpha-orb"))
    self.assertEqual(ledger.get_state("protected_exits:alpha-orb"), ["2026-11-17T09:30:00+08:00"])

  def test_weekly_review_ignores_protected_exits_in_protected_mode(self):
    from futures_ai import lessons
    rows = [{"entry_ts": "2026-11-17T09:00:00+08:00", "exit_ts": "2026-11-17T09:10:00+08:00", "side": "B",
             "pnl_twd": -32, "exit_kind": "SL", "entry_price": 44000, "entry_reason": ""},
            {"entry_ts": "2026-11-17T09:20:00+08:00", "exit_ts": "2026-11-17T09:30:00+08:00", "side": "B",
             "pnl_twd": -1552, "exit_kind": "SL", "entry_price": 44000, "entry_reason": ""}]
    self.assertEqual(lessons.review(rows, "sigma-vwap")["tags"]["consec_sl"], 1)
    r = lessons.review(rows, "sigma-vwap", protected={"2026-11-17T09:10:00+08:00"})
    self.assertEqual((r["tags"]["consec_sl"], r["losses"]), (0, 1))

  def test_protected_twins_and_report(self):
    from futures_ai import shadows
    tw = shadows.build_protected_twins(cfg(), Ledger(":memory:"))
    self.assertEqual(sorted(t.ai for t in tw), ["prot-alpha-orb", "prot-nova-nightorb", "prot-sigma-vwap"])
    self.assertTrue(all(t.protected_mode for t in tw))
    live, sh = Ledger(":memory:"), Ledger(":memory:")
    txt = "\n".join(shadows.protected_shadow_lines(live, sh, now=at(2026, 11, 27, 23, 0)))
    self.assertIn("第 10/10 個交易日", txt)
    self.assertIn("已滿 10 個交易日", txt)
    self.assertEqual(shadows.protected_shadow_lines(live, sh, now=at(2026, 11, 14, 12, 0)), [])


if __name__ == "__main__":
  unittest.main()


class PlanABSwitchTest(unittest.TestCase):
  """User decision 2026-11-18: live Alpha A2 (stop cap 160), Sigma A+B, Nova A; previous version as old- shadows."""

  def _night_bars(self, start, closes_range):
    """1-minute bars for one night session starting at `start` (15:00): open 100, close closes_range[0],
    high/low spanning closes_range[1]."""
    from datetime import timedelta
    from futures_ai.feed import Bar
    close, rng = closes_range
    out = []
    for m in range(1, 61):
      end = start + timedelta(minutes=m)
      c = 100 + close * m / 60
      out.append(Bar(end, 100 if m == 1 else c, max(c, 100) + (rng if m == 30 else 0), min(c, 100), c, 1))
    return out

  def test_session_efficiency_and_prior_nights(self):
    from futures_ai.regime import blocked, prior_night_effs, session_efficiency
    bars = []
    starts = [at(2026, 11, d, 15, 0) for d in (9, 10, 11, 12, 13, 16)]  # weeknights (no Saturday night session)
    for st, (close, rng) in zip(starts, [(100, 0), (10, 90), (10, 90), (10, 90), (10, 90), (10, 90)]):
      bars += self._night_bars(st, (close, rng))
    self.assertAlmostEqual(session_efficiency(bars[:60]), 1.0)
    self.assertEqual(len(prior_night_effs(bars, starts[3])), 3)  # Only nights that started before.
    effs = prior_night_effs(bars, at(2026, 11, 17, 15, 0))
    self.assertEqual(len(effs), 6)
    self.assertAlmostEqual(effs[0], 1.0)
    chop = session_efficiency(bars[60:120])  # net +10, range 95
    self.assertAlmostEqual(chop, 10 / 95)
    self.assertAlmostEqual(effs[1], chop)
    off, m = blocked(effs, (5, 0.40))
    self.assertTrue(off)
    self.assertAlmostEqual(m, chop)
    off, m = blocked(effs, (6, 0.40))
    self.assertAlmostEqual(m, (1.0 + 5 * chop) / 6)
    self.assertTrue(off)
    self.assertFalse(blocked([0.5, 0.6], (5, 0.40))[0])
    self.assertEqual(blocked([], (5, 0.40)), (False, None))  # No history: never blocks.

  def test_live_strategies_and_old_shadows(self):
    from futures_ai.shadows import build_shadows, old_strategies
    from futures_ai.strategies import build_strategies
    live = {s.id: s for s in build_strategies()}
    self.assertEqual(live["alpha-orb"].sl_cap, 160)
    self.assertEqual(live["sigma-vwap"].night_eff, (5, 0.40))
    self.assertEqual(live["nova-nightorb"].night_eff, (5, 0.40))
    self.assertIsNone(getattr(live["alpha-orb"], "night_eff", None))
    self.assertIsNone(getattr(live["omega-swing"], "night_eff", None))
    old = {s.id: s for s in old_strategies()}
    self.assertEqual(set(old), {"alpha-orb", "sigma-vwap", "nova-nightorb"})
    self.assertEqual(old["alpha-orb"].sl_cap, 120)
    self.assertEqual((old["sigma-vwap"].pull, old["sigma-vwap"].sl_atr), (0.5, 2.0))
    self.assertTrue(all(s.night_eff is None for s in old.values()))
    c = cfg()
    led = Ledger(":memory:")
    sh = {t.ai: t for t in build_shadows(c, led)}
    for ai in ("alpha-orb", "sigma-vwap", "nova-nightorb"):
      self.assertIn(f"old-{ai}", sh)
      self.assertNotIn(f"prot-{ai}", sh)  # Protected-exit comparison ended 2026-11-28 (live since then).
      self.assertEqual(sh[f"old-{ai}"].protected_mode, bool(c.get("protected_not_stop")))  # follows the config
    self.assertTrue(DEFAULTS["protected_not_stop"])
    self.assertEqual(sh["old-alpha-orb"].s.sl_cap, 120)

  def test_alpha_stop_cap_160(self):
    from futures_ai.strategies import OrbBreakout
    from futures_ai.feed import Bar
    from datetime import timedelta
    t0 = at(2026, 11, 18, 15, 0)  # Night session open: range = first three 5-minute bars.
    rng = [Bar(t0 + timedelta(minutes=5 * k), 1000, 1250, 1000, 1100, 1) for k in range(1, 4)]  # width 250
    prev = Bar(t0 + timedelta(minutes=20), 1100, 1200, 1100, 1200, 1)
    brk = Bar(t0 + timedelta(minutes=25), 1240, 1300, 1240, 1290, 1)
    bars = rng + [prev, brk]
    a = OrbBreakout(range_bars=3, sl_frac=0.75, tp_mult=3.0, max_width=260)
    a.sl_cap = 160
    e = a.decide(bars, 0, None)
    self.assertIsNotNone(e)
    self.assertEqual(e.sl_pts, 160)  # 250 x 0.75 = 187.5 -> capped at 160 (was 120)
    a.sl_cap = 120
    self.assertEqual(a.decide(bars, 0, None).sl_pts, 120)

  def test_trader_night_filter(self):
    from futures_ai import trader as tm
    from futures_ai.strategies import Entry
    from futures_ai.trader import AiTrader
    from futures_ai.brokers import LocalPaperBroker
    from futures_ai.strategies import build_strategies
    from futures_ai.shadows import old_strategies
    from futures_ai.feed import Bar
    from datetime import timedelta
    c = cfg()
    led = Ledger(":memory:")
    sig_live = [s for s in build_strategies() if s.id == "sigma-vwap"][0]
    sig_old = [s for s in old_strategies() if s.id == "sigma-vwap"][0]
    t_live = AiTrader(c, sig_live, LocalPaperBroker(c), led)
    sig_old.id = "old-sigma-vwap"
    t_old = AiTrader(c, sig_old, LocalPaperBroker(c), led)
    start = at(2026, 11, 18, 15, 0)
    bars = [Bar(start + timedelta(minutes=5 * k), 100 + k, 101 + k, 99 + k, 100 + k, 10) for k in range(1, 8)]
    e = Entry("B", 50, 100, "test")
    try:
      tm.NIGHT_STATE.clear()
      tm.NIGHT_STATE.update(start=start.isoformat(), effs=[0.2, 0.3, 0.25, 0.3, 0.35])
      why = t_live._filter(e, bars)
      self.assertIsNotNone(why)
      self.assertIn("夜盤盤勢過濾", why)
      self.assertNotIn("夜盤盤勢過濾", t_old._filter(e, bars) or "")  # The old- shadow has no night filter.
      tm.NIGHT_STATE.update(effs=[0.5, 0.45, 0.6, 0.4, 0.5])
      self.assertNotIn("夜盤盤勢過濾", t_live._filter(e, bars) or "")
      tm.NIGHT_STATE.update(start=(start - timedelta(days=1)).isoformat(), effs=[0.1] * 5)  # Stale session.
      self.assertNotIn("夜盤盤勢過濾", t_live._filter(e, bars) or "")
      day = [Bar(at(2026, 11, 18, 9, 0) + timedelta(minutes=5 * k), 100, 101, 99, 100, 10) for k in range(1, 8)]
      tm.NIGHT_STATE.update(start=start.isoformat(), effs=[0.1] * 5)
      self.assertNotIn("夜盤盤勢過濾", t_live._filter(e, day) or "")  # Day session: never filtered.
    finally:
      tm.NIGHT_STATE.clear()

  def test_wfo_night_filter_and_sigma_grid(self):
    from futures_ai import wfo
    sl = sorted({p["sl_atr"] for p in wfo.grid("sigma-pullback") if not p.get("off")})
    self.assertEqual(sl, [1.5, 2.0, 2.5, 3.0])

    class S:  # Minimal session stub: the filter must stop all decisions before any signal work.
      name, bars, td = "night", [], None
    s = S()
    s.prior_night_effs = [0.1] * 5
    self.assertTrue(wfo.regime_blocked(s.prior_night_effs, (5, 0.40))[0])

  def test_set_prior_night_effs(self):
    from datetime import timedelta
    from futures_ai import wfo
    from futures_ai.feed import Bar
    sess = []
    d0 = at(2026, 11, 9, 15, 0)
    for k, close in enumerate([100, 10, 50]):
      bars = self._night_bars(d0 + timedelta(days=k), (close, 0))
      s = wfo.Session(None, "night", "X", bars, d0 + timedelta(days=k, hours=14))
      sess.append(s)
    day = wfo.Session(None, "day", "X", [Bar(at(2026, 11, 10, 9, 0), 1, 2, 0, 1, 1)], at(2026, 11, 10, 13, 45))
    wfo.set_prior_night_effs(sess + [day])
    self.assertEqual(sess[0].prior_night_effs, [])
    self.assertEqual(len(sess[2].prior_night_effs), 2)
    self.assertIsNone(day.prior_night_effs)

  def test_weekly_lines(self):
    from futures_ai.shadows import red_light_lines, version_compare_lines, protected_shadow_lines
    live, sh = Ledger(":memory:"), Ledger(":memory:")
    now = at(2026, 11, 25, 9, 0)
    def add(led, ai, ts, pnl):
      led.conn.execute("INSERT INTO trades (ai, trading_day, contract, side, qty, entry_ts, entry_price, exit_ts, "
                       "exit_price, points, gross_twd, cost_twd, pnl_twd, exit_kind) VALUES "
                       "(?, '2026-11-20', 'TMF', 'B', 1, ?, 100, ?, 90, -10, -100, 20, ?, 'SL')", (ai, ts, ts, pnl))
    add(live, "nova-nightorb", "2026-11-17T16:00:00+08:00", -500)   # before the switch: old version live
    add(live, "nova-nightorb", "2026-11-19T16:00:00+08:00", -13000)  # new version
    add(sh, "old-nova-nightorb", "2026-11-19T16:00:00+08:00", -2000)
    add(sh, "prot-nova-nightorb", "2026-11-19T16:00:00+08:00", -1500)
    txt = "\n".join(version_compare_lines(live, sh, now))
    self.assertIn("Nova｜-13,000（1 筆）｜-2,000（1 筆）", txt)
    red = "\n".join(red_light_lines(live, now, expect_hist=[{"alpha-orb": -5}, {"alpha-orb": -3}]))
    self.assertIn("Nova：-13,000（紅線 −12,352）🔴", red)
    self.assertIn("連續 2 週樣本外預期為負", red)
    self.assertIn("Sigma：+0（紅線 −18,036）🟢", red)
    prot = "\n".join(protected_shadow_lines(live, sh, now))
    self.assertIn("Nova｜-2,500（2 筆）｜-1,500（1 筆）", prot)  # live before the switch + old- shadow after


class NightRegimeEngineSafetyTest(unittest.TestCase):

  def test_barstore_failure_does_not_stop_the_loop(self):
    from futures_ai import trader as tm
    c = cfg()
    ledger = Ledger(":memory:")
    tr = AiTrader(c, FakeStrategy(None), LocalPaperBroker(c), ledger)
    calls = []
    tr.on_price = lambda *a, **k: calls.append(a[0])

    class BadStore:
      def add(self, bars):
        pass

      def sixty_min(self, now):
        return []

      @property
      def bars(self):
        raise OSError("disk full")

    now = at(2026, 10, 20, 20, 0)  # Night session.
    eng = Engine(c, ledger, [tr], feed=FakeFeed(), clock=lambda: now)
    eng.barstore = BadStore()
    try:
      tm.NIGHT_STATE.clear()
      eng.step()
      eng.step()
      self.assertEqual(len(calls), 2)  # on_price still runs every step.
      self.assertEqual(tm.NIGHT_STATE.get("effs"), [])  # Failure = no filter (fail-open), not retried each poll.
      self.assertTrue(tm.NIGHT_STATE.get("start"))
    finally:
      tm.NIGHT_STATE.clear()


class ParamHistoryReplayTest(unittest.TestCase):

  def test_weekly_switch_splits_periods_without_backfill(self):
    import tempfile
    from pathlib import Path
    from futures_ai.replay import record_param_switch, rule_periods
    with tempfile.TemporaryDirectory() as d:
      f = Path(d) / "pv.json"
      record_param_switch("sigma-vwap", {"pull_atr": 0.5, "sl_atr": 2.5, "tp_mult": 4.0, "trail_atr": None},
                          ts="2026-11-21T22:00:00+08:00", path=f)
      per = rule_periods(at(2026, 11, 16, 8, 45), path=f)
      by_start = {a.isoformat(): ov for a, _b, _lab, ov in per}
      pre = by_start["2026-11-16T08:45:00+08:00"]
      self.assertEqual(pre["sigma-vwap"]["sl_atr"], 2.0)
      self.assertEqual(pre["alpha-orb"]["sl_cap"], 120)
      sw = by_start["2026-11-18T13:46:55+08:00"]
      self.assertEqual(sw["sigma-vwap"]["sl_atr"], 2.0)  # Parameters live at the switch (not the newest).
      self.assertNotIn("night_eff", sw["sigma-vwap"])  # New rules (from the current spec) apply after the switch.
      wk = by_start["2026-11-21T22:00:00+08:00"]
      self.assertEqual(wk["sigma-vwap"]["sl_atr"], 2.5)
      self.assertEqual(per[-1][1], None)

  def test_nova_switch_not_backfilled(self):
    import tempfile
    from pathlib import Path
    from futures_ai.replay import record_param_switch, rule_periods, sim_specs
    from futures_ai.shadows import old_strategies
    with tempfile.TemporaryDirectory() as d:
      f = Path(d) / "pv.json"
      record_param_switch("nova-nightorb", {"range_min": 30, "window": 600, "sl_frac": 1.0, "tp_mult": 4.0},
                          ts="2026-11-21T22:00:00+08:00", path=f)
      per = {a.isoformat(): ov for a, _b, _l, ov in rule_periods(at(2026, 11, 16, 8, 45), path=f)}
      for k in ("2026-11-16T08:45:00+08:00", "2026-11-18T13:46:55+08:00"):
        self.assertEqual(per[k]["nova-nightorb"]["tp_mult"], 3.0)
        self.assertEqual(per[k]["nova-us"]["tp_mult"], 3.0)
      self.assertEqual(per["2026-11-21T22:00:00+08:00"]["nova-nightorb"]["tp_mult"], 4.0)
      self.assertEqual(per["2026-11-21T22:00:00+08:00"]["nova-us"]["tp_mult"], 4.0)
    self.assertEqual(sim_specs(legacy=True)["nova-nightorb"][1]["tp_mult"], 3.0)
    nova_old = [s for s in old_strategies() if s.id == "nova-nightorb"][0]
    self.assertEqual((nova_old.range_min, nova_old.window, nova_old.sl_frac, nova_old.tp_mult), (30, 600, 1.0, 3.0))


class SwitchGuardTest(unittest.TestCase):

  def test_sigma_needs_10pct_and_2000_and_profit(self):
    from futures_ai.weekly import beats_current, switch_need
    self.assertEqual(switch_need("sigma-vwap", 100000), 110000)
    self.assertFalse(beats_current("sigma-vwap", 109999, 100000))
    self.assertTrue(beats_current("sigma-vwap", 110001, 100000))
    self.assertEqual(switch_need("sigma-vwap", 10000), 12000)  # Floor NT$2,000 when 10% is smaller.
    self.assertFalse(beats_current("sigma-vwap", 11500, 10000))
    self.assertEqual(switch_need("sigma-vwap", -20000), -18000)  # Negative current: 10% of |current|.
    self.assertFalse(beats_current("sigma-vwap", -1000, -20000))  # Must itself be profitable.
    self.assertTrue(beats_current("sigma-vwap", 500, -20000))
    self.assertEqual(switch_need("sigma-vwap", 0), 2000)  # Zero current: no division, floor applies.
    self.assertFalse(beats_current("sigma-vwap", 1999, 0))
    self.assertFalse(beats_current("sigma-vwap", 90731, 102075))  # This week's actual numbers: keep 2.0.

  def test_alpha_keeps_plain_rule(self):
    from futures_ai.weekly import beats_current
    self.assertTrue(beats_current("alpha-orb", 100001, 100000))
    self.assertFalse(beats_current("alpha-orb", 100000, 100000))


class LessonUpgradeTest(unittest.TestCase):

  def test_new_lesson_needs_minimum_gain(self):
    from futures_ai import lessons as L
    base = {"pnl": 100000, "last60": 0, "q": [1, 1, 1, 1], "maxdd": 10000, "trades": 50}

    def metrics(gain):
      return lambda c, a, p, s, d: base if p.get("x") is None else dict(base, pnl=100000 + gain, q=[2, 2, 1, 1])
    with mock.patch.object(L, "_metrics", side_effect=metrics(1500)):
      bt = L.backtest({}, "a", {}, {"x": 1}, [], [], base)
      self.assertFalse(bt["ok"]); self.assertTrue(bt["small"]); self.assertEqual(bt["need"], 2000)
      self.assertTrue(L.backtest({}, "a", {}, {"x": 1}, [], [], base, require_min_gain=False)["ok"])
    with mock.patch.object(L, "_metrics", side_effect=metrics(2000)):
      self.assertTrue(L.backtest({}, "a", {}, {"x": 1}, [], [], base)["ok"])
    self.assertEqual(L.min_gain(-300000), 6000)  # 2% of |base| when larger than 2,000
  """User request 2026-11-18: night_chop / noise_stop tags, monthly rule retirement, AI brain digest."""

  def _sessions(self):
    from datetime import timedelta
    from futures_ai import wfo
    from futures_ai.feed import Bar
    out = []
    for k, d in enumerate((9, 10, 11, 12, 13, 16)):
      st = at(2026, 11, d, 15, 0)
      bars = []
      for m in range(1, 121):
        c = 1000 + (5 if m % 2 else -5)  # flat, choppy night
        if k == 5 and m > 30:
          c = 1000 - 60 if m < 50 else 1000 + 10  # last night: drop then back above 1000
        o = 1000 if m == 1 else c
        cl = 1000 if (m == 120 and k < 5) else c  # Net move 0 over a 16-point range: efficiency 0.
        bars.append(Bar(st + timedelta(minutes=m), o, max(o, cl) + 3, min(o, cl) - 3, cl, 1))
      out.append(wfo.Session(None, "night", "X", bars, st + timedelta(hours=14)))
    wfo.set_prior_night_effs(out)
    return out

  def test_new_tags(self):
    from futures_ai.lessons import MarketCtx, review
    ctx = MarketCtx(self._sessions())
    self.assertLess(ctx.night_mean(at(2026, 11, 16, 15, 40)), 0.40)
    self.assertIsNone(ctx.night_mean(at(2026, 11, 16, 9, 40)))  # Day session: not a night.
    rows = [{"ai": "alpha-orb", "side": "B", "entry_ts": "2026-11-16T15:31:00+08:00", "entry_price": 1000,
             "exit_ts": "2026-11-16T15:35:00+08:00", "exit_kind": "SL", "pnl_twd": -800, "entry_reason": ""}]
    r = review(rows, "alpha-orb", ctx=ctx)
    self.assertEqual(r["tags"]["night_chop"], 1)
    self.assertEqual(r["tags"]["noise_stop"], 1)  # Back at 1000+ by 15:50 (within 30 min).
    rows[0]["exit_ts"] = "2026-11-16T15:10:00+08:00"
    rows[0]["entry_price"] = 1100  # Never back up to 1100.
    self.assertEqual(review(rows, "alpha-orb", ctx=ctx)["tags"]["noise_stop"], 0)
    self.assertEqual(review(rows, "alpha-orb")["tags"]["night_chop"], 0)  # Without market context: no new tags.

  def test_rule_text_and_candidates(self):
    from futures_ai.lessons import CANDIDATES, LIVE_RULE_KEYS, rule_zh
    self.assertIn("停損上限 200 點", rule_zh({"sl_cap": 200}))
    self.assertIn("前 5 個夜盤", rule_zh({"night_eff": [5, 0.40]}))
    self.assertEqual(CANDIDATES["noise_stop"]["omega-swing"], [])
    self.assertIn("sl_cap", LIVE_RULE_KEYS)

  def test_lesson_can_override_base_rules_live(self):
    import json, tempfile
    from futures_ai.strategies import build_strategies
    params = {"lessons": {"alpha-orb": {"sl_cap": 200, "night_eff": [3, 0.35]}, "nova-nightorb": {"sl_cap": 250}}}
    live = {s.id: s for s in build_strategies(params)}
    self.assertEqual(live["alpha-orb"].sl_cap, 200)  # Lesson wins over the base cap 160.
    self.assertEqual(live["alpha-orb"].night_eff, (3, 0.35))
    self.assertEqual(live["nova-nightorb"].sl_cap, 250)
    self.assertEqual(live["sigma-vwap"].sl_cap, 150)

  def test_apply_trial_add_and_retire(self):
    from futures_ai.lessons import apply_trial
    have = {"rev_cool": 30, "max_sl": 2}
    self.assertEqual(apply_trial(have, {"rule": {"min_er": 0.2}}), {"rev_cool": 30, "max_sl": 2, "min_er": 0.2})
    self.assertEqual(apply_trial(have, {"rule": {"max_sl": None}, "retire": True}), {"rev_cool": 30})
    self.assertEqual(have, {"rev_cool": 30, "max_sl": 2})  # Input untouched.

  def test_retire_check_starts_trial_only_when_removal_helps(self):
    from unittest import mock
    from futures_ai import lessons as L
    st = {"trials": {}, "rejected": [], "adopted": []}
    lines = []
    base = {"rev_cool": 30, "max_sl": 2, "min_er": 0.3}
    base_wo = {"min_er": 0.3}  # Without lessons: no rev_cool / max_sl; min_er is a base rule.
    def fake_bt(c, sim_ai, b, rule, sessions, days, base_m, require_min_gain=True):
      self.assertFalse(require_min_gain)  # retirement uses no minimum improvement
      good = rule == {"max_sl": None}
      return {"ok": good, "gain": 5000 if good else -3000, "gain60": 100, "qwin": 4, "qimp": 3, "dd": 10, "base_dd": 10,
              "trades": 1, "base_trades": 1}
    idle_lines = []
    idle = {"ok": False, "gain": 0, "gain60": 0, "qwin": 4, "qimp": 0, "dd": 10, "base_dd": 10, "trades": 5,
            "base_trades": 5}
    with mock.patch.object(L, "_metrics", return_value={}), mock.patch.object(L, "backtest", return_value=idle):
      self.assertFalse(L._retire_check({}, "sigma-vwap", "sigma-pullback", base, base_wo, {"max_sl": 2}, [], [],
                                       {"trials": {}, "rejected": []}, {}, at(2026, 12, 5, 10, 0), idle_lines))
    self.assertIn("規則目前沒有作用", idle_lines[0])
    with mock.patch.object(L, "_metrics", return_value={}), mock.patch.object(L, "backtest", side_effect=fake_bt), \
        mock.patch.object(L, "_eval_live", return_value=(12, {"pnl": 100}, {"pnl": 200})):
      started = L._retire_check({}, "sigma-vwap", "sigma-pullback", base, base_wo, {"rev_cool": 30, "max_sl": 2,
                                "min_er": 0.3}, [], [], st, {}, at(2026, 12, 5, 10, 0), lines)
    self.assertTrue(started)
    t = st["trials"]["sigma-vwap"]
    self.assertEqual((t["rule"], t["retire"]), ({"max_sl": None}, True))
    txt = "\n".join(lines)
    self.assertIn("規則仍有用，保留", txt)  # rev_cool removal did not pass.
    self.assertIn("開始實戰驗證", txt)
    self.assertNotIn("效率比", txt)  # min_er equals the base rule: removing the lesson changes nothing, not tested.
    st2 = {"trials": {}, "rejected": [], "adopted": []}
    with mock.patch.object(L, "_metrics", return_value={}), mock.patch.object(L, "backtest", side_effect=fake_bt), \
        mock.patch.object(L, "_eval_live", return_value=(12, {"pnl": 300}, {"pnl": 200})):
      self.assertFalse(L._retire_check({}, "sigma-vwap", "sigma-pullback", base, base_wo, {"max_sl": 2}, [], [],
                                       st2, {}, at(2026, 12, 5, 10, 0), []))
    self.assertEqual(st2["trials"], {})
    self.assertTrue(st2["rejected"][0]["retire"])  # Recent live bars say the rule still helps.

  def test_brain_digest(self):
    from futures_ai.lessons import brain_digest
    led = Ledger(":memory:")
    now = at(2026, 11, 21, 10, 0)
    led.set_state("brain:nova-nightorb", {"journal": [
        {"ts": "2026-11-19T16:00:00+08:00", "pnl": -1550, "diagnosis": "進場後立刻反向（1.0 分鐘就停損），趨勢盤中的假訊號"},
        {"ts": "2026-11-18T16:00:00+08:00", "pnl": -1500, "diagnosis": "走勢與判斷相反（最大浮盈 20 點）；觸發後滑價 5 點"},
        {"ts": "2026-11-18T17:00:00+08:00", "pnl": 3000, "diagnosis": "判斷正確"},
        {"ts": "2026-11-01T16:00:00+08:00", "pnl": -1500, "diagnosis": "走勢與判斷相反"}]})
    txt = "\n".join(brain_digest(led, now))
    self.assertIn("Nova：", txt)
    self.assertIn("進場就反向的假訊號 1 筆（-1,550）", txt)
    self.assertIn("方向判斷錯誤 1 筆（-1,500）", txt)  # The 11/01 trade is outside this week.
    self.assertIn("停損滑價 ≥3 點 1 次", txt)
    self.assertIn("最大一筆 -1,550", txt)
    self.assertIn("本週沒有虧損單", "\n".join(brain_digest(Ledger(":memory:"), now)))

  def test_specs_without_file_lessons(self):
    from futures_ai.lessons import _specs
    from futures_ai.replay import sim_specs
    base = _specs({}, file_lessons=False)
    self.assertIsNone(base["alpha-orb"][1].get("rev_cool"))  # Base rules only.
    self.assertEqual(base["sigma-vwap"][1]["min_er"], 0.30)  # A base rule stays.
    self.assertEqual(sim_specs(with_lessons=False)["nova-nightorb"][1]["max_sl"], 4)

  def test_candidate_equal_to_base_is_skipped(self):
    from futures_ai.lessons import _norm
    self.assertEqual(_norm([5, 0.40]), _norm((5, 0.40)))
    self.assertNotEqual(_norm([3, 0.35]), _norm((5, 0.40)))


class NewsOffLineTest(unittest.TestCase):
  """User 2026-11-18: 「股市新聞不要再傳到賴了」 -> no news on LINE (radar keeps running for the dashboard)."""

  def test_reports_have_no_news_by_default(self):
    from futures_ai.notify import summary_text
    self.assertFalse(cfg()["notify"]["news_line"])
    st = {"curve_session": {}, "ais": [{"name": "Omega 波段順勢 AI", "session_pnl": 100}],
          "combined": {"session_pnl": 100},
          "news": {"mode": "high", "fomc_window": False, "headlines": [{"title": "關稅新聞"}]}}
    self.assertNotIn("新聞", summary_text(st, "日盤"))
    self.assertIn("關稅新聞", summary_text(st, "日盤", include_news=True))

  def test_stored_night_report_is_stripped(self):
    from futures_ai.notify import LineNotifier
    c = cfg()
    led = Ledger(":memory:")
    n = LineNotifier(c, led)
    sent = []
    n._send = sent.append
    led.set_state("night_report", "【微台 AI】昨晚夜盤結果\n本盤合計：+0\n目前無留倉\n\n新聞：🔴 高風險\n・某新聞")
    n.maybe_send_night(at(2026, 11, 19, 8, 31))
    self.assertEqual(len(sent), 1)
    self.assertNotIn("新聞", sent[0])
    self.assertIn("本盤合計：+0", sent[0])

  def test_radar_gets_no_notifier_when_off(self):
    from futures_ai.news import NewsRadar
    led = Ledger(":memory:")
    r = NewsRadar(led, 10, None)
    self.assertIsNone(r.notifier)


class Be05ShadowTest(unittest.TestCase):

  def test_shadow_built_and_isolated(self):
    from futures_ai.shadows import BE05_ID, build_shadows
    from futures_ai.strategies import build_strategies
    c = cfg()
    led = Ledger(":memory:")
    sh = {t.ai: t for t in build_shadows(c, led)}
    self.assertIn(BE05_ID, sh)
    self.assertEqual(sh[BE05_ID].s.be_r, 0.5)
    self.assertEqual(sh[BE05_ID].broker.name, "local_paper")
    self.assertTrue(getattr(sh[BE05_ID].s, "hold_overnight", False))
    live = [s for s in build_strategies() if s.id == "omega-swing"][0]
    self.assertEqual(live.be_r, 0.7)  # Live Omega untouched.

  def test_breakeven_moves_at_half_initial_stop(self):
    from futures_ai.shadows import BE05_ID, build_shadows
    c = cfg()
    led = Ledger(":memory:")
    tr = {t.ai: t for t in build_shadows(c, led)}[BE05_ID]
    tr.s.h60 = []  # No ATR: trailing keeps the stored distance.
    tr.pos = {"side": "S", "qty": 1, "entry_price": 39000, "entry_ts": "2026-11-20T10:00:00+08:00", "contract": "TMFL6",
              "trading_day": "2026-11-20", "session_key": "2026-11-20-day", "sl": 40200, "tp": 34000, "sl_oid": 0,
              "tp_oid": 0, "entry_cost": 20, "reason": "t", "regime": "trend", "mode": "native", "mfe": 0.0, "mae": 0.0,
              "trail": 1200, "init_sl_pts": 1200.0}
    tr.on_price(at(2026, 11, 20, 10, 30), "2026-11-20", "TMFL6", {"last": 38400, "bid": 38399, "ask": 38400,
                                                                  "hi": 38400, "lo": 38400}, 100, False)
    self.assertEqual(tr.pos["sl"], 38994)  # 600 pts profit = 0.5 x 1200 -> stop to entry - 6 (short).

  def test_weekly_lines(self):
    from futures_ai.shadows import BE05_ID, be05_lines
    live, sh = Ledger(":memory:"), Ledger(":memory:")
    for led, ai, pts, pnl in ((live, "omega-swing", -595, -5990), (sh, BE05_ID, 0, -30)):
      led.conn.execute("INSERT INTO trades (ai, trading_day, contract, side, qty, entry_ts, entry_price, exit_ts, "
                       "exit_price, points, gross_twd, cost_twd, pnl_twd, exit_kind) VALUES (?, '2026-11-21', 'TMF', "
                       "'S', 1, '2026-11-21T10:00:00+08:00', 1, '2026-11-21T22:00:00+08:00', 1, ?, 0, 0, ?, 'SL')",
                       (ai, pts, pnl))
    txt = "\n".join(be05_lines(live, sh, at(2026, 11, 24, 9, 0)))
    self.assertIn("源頭差異 1/5 次、結果不同共 1 筆", txt)
    self.assertIn("實盤（保本 0.7）：-5,990（賺 0／打平 0／賠 1）", txt)
    self.assertIn("影子（保本 0.5）：-30（賺 0／打平 1／賠 0）", txt)


class OmegaCapShadowTest(unittest.TestCase):

  def test_shadows_built(self):
    from futures_ai.shadows import LEGACY_OMEGA_ID, NOCAP_OMEGA_ID, build_shadows
    from futures_ai.strategies import build_strategies
    c = cfg()
    sh = {t.ai: t for t in build_shadows(c, Ledger(":memory:"))}
    self.assertIsNone(sh[NOCAP_OMEGA_ID].s.max_sl_pts)
    self.assertEqual(sh[NOCAP_OMEGA_ID].s.min_er, 0.20)
    lg = sh[LEGACY_OMEGA_ID].s
    self.assertEqual((lg.min_er, lg.be_r, lg.max_ext_atr, lg.max_sl_pts), (None, None, None, None))
    self.assertEqual(sh[LEGACY_OMEGA_ID].broker.name, "local_paper")
    live = [s for s in build_strategies() if s.id == "omega-swing"][0]
    self.assertEqual(live.max_sl_pts, 1200)  # Live Omega untouched.

  def _ledgers(self, live_pnls, nocap_pnls):
    from futures_ai.shadows import NOCAP_OMEGA_ID
    live, sh = Ledger(":memory:"), Ledger(":memory:")
    for led, ai, pnls in ((live, "omega-swing", live_pnls), (sh, NOCAP_OMEGA_ID, nocap_pnls)):
      for i, pnl in enumerate(pnls):
        led.conn.execute("INSERT INTO trades (ai, trading_day, contract, side, qty, entry_ts, entry_price, exit_ts, "
                         "exit_price, points, gross_twd, cost_twd, pnl_twd, exit_kind) VALUES (?, '2026-12-01', 'TMF', "
                         "'S', 1, ?, 1, ?, 1, 0, 0, 0, ?, 'SL')",
                         (ai, f"2026-12-{i + 1:02d}T10:00:00+08:00", f"2026-12-{i + 1:02d}T12:00:00+08:00", pnl))
    return live, sh

  def test_decision_rule(self):
    from futures_ai.shadows import omega_cap_lines
    live, sh = self._ledgers([-1000] * 10, [500] * 9 + [-14000])
    txt = "\n".join(omega_cap_lines(live, sh, at(2026, 12, 20, 9, 0)))
    self.assertIn("不設上限影子：-9,500（10 筆，最大單筆虧損 -14,000）", txt)
    self.assertIn("沒有多賺", txt)  # -9,500 vs -10,000: +500 < 2,000
    live, sh = self._ledgers([-1000] * 10, [3000] * 9 + [-14000])
    self.assertIn("請先決定你能接受的 Omega 單筆最大虧損", "\n".join(omega_cap_lines(live, sh, at(2026, 12, 20, 9, 0))))
    self.assertIn("超過你核准的 -12,000", "\n".join(omega_cap_lines(live, sh, at(2026, 12, 20, 9, 0), max_loss=12000)))
    self.assertIn("建議改用", "\n".join(omega_cap_lines(live, sh, at(2026, 12, 20, 9, 0), max_loss=15000)))
    live, sh = self._ledgers([-1000] * 3, [3000] * 3)
    self.assertNotIn("→", "\n".join(omega_cap_lines(live, sh, at(2026, 12, 20, 9, 0))))  # too few, before 12/31
    self.assertIn("→", "\n".join(omega_cap_lines(live, sh, at(2026, 12, 31, 9, 0))))  # end date reached


class WeeklyBriefTest(unittest.TestCase):
  """User 2026-11-21: weekly LINE message must be short."""

  def test_brief_from_this_weeks_report(self):
    from futures_ai.weekly import line_brief
    live = ["📊 本週收益（模擬，NT$）", "AI｜本週｜美股開盤後1h｜後2h", "Alpha｜-2,500｜-2,500｜-2,500",
            "Sigma｜-1,800｜-1,550｜-1,550", "Nova｜-12,400｜-4,650｜-7,750", "Omega｜+6,390｜+0｜+0"]
    les = ["🔍 Alpha 反覆出現「停損後 30 分鐘內回到進場價」→ 試「停損上限 200 點」：一年 +1,500 → 回測通過，開始實戰驗證 10 天（新行情）才決定是否採用",
           "🔍 Nova 反覆出現「開盤急著進場」→ 試「開盤 45 分鐘內不進場」：一年 -10,156 → 回測沒變好，不採用"]
    cmp = ["", "🆚 新版實盤 vs 舊版影子", "AI｜新版（實盤）｜舊版（影子）", "Alpha｜+0（0 筆）｜+0（0 筆）",
           "Sigma｜-1,550（1 筆）｜-10,946（7 筆）", "Nova｜+0（0 筆）｜-8,088（12 筆）"]
    red = ["", "🚦 停用紅燈", "Alpha：+0（紅線 −8,622）🟢", "Sigma：-1,550（紅線 −18,036）🟢"]
    trials = ["🧪 受保護出場影子比對（11/16 起，第 5/10 個交易日）"]
    stock = ["", "— 本週股票 AI 損益 —", "🇺🇸 美股（你的持倉，USD）｜至 11/19", "Alpha：-505（-0.7%）｜累計 -791",
             "Sigma：-582（-0.9%）｜累計 -868", "📌 你（不動）：-1,046（-1.5%）｜累計 -1,331"]
    txt = line_brief(at(2026, 11, 21, 10, 7), live, les, cmp, red, trials, {}, stock_lines=stock,
                     all_lines=live + les)
    self.assertIn("本週：Omega +6,390｜Sigma -1,800｜Alpha -2,500｜Nova -12,400（合計 -10,310）", txt)
    self.assertIn("新版 vs 舊版：Sigma -1,550 vs -10,946｜Nova +0 vs -8,088", txt)
    self.assertIn("參數：維持不變", txt)
    self.assertIn("經驗規則：Alpha 試行「停損上限 200 點」10 天", txt)
    self.assertIn("紅燈：無", txt)
    self.assertNotIn("需要你決定", txt)
    self.assertIn("美股 AI（USD）：Alpha -505｜Sigma -582｜你不動 -1,046", txt)
    self.assertNotIn("計算失敗", txt)
    self.assertLessEqual(len(txt.splitlines()), 9)
    self.assertLess(len(txt), 400)

  def test_brief_asks_when_a_decision_is_due(self):
    from futures_ai.weekly import line_brief
    txt = line_brief(at(2026, 11, 28, 10, 0), [], [], [], ["Nova：-13,000（紅線 −12,352）🔴 建議停用，請決定（虧損超過紅線）"],
                     ["→ 已滿 10 個交易日，請決定是否把「受保護出場不算停損」正式上線（config protected_not_stop）"],
                     {"sigma-vwap": {"sl_atr": 2.5}},
                     all_lines=["\n📚 虧損檢討失敗：boom", "\n🩺 策略健康度計算失敗：x"])
    self.assertIn("參數：Sigma 趨勢回測 已更新", txt)
    self.assertIn("紅燈：Nova：-13,000", txt)
    self.assertIn("👉 需要你決定：已滿 10 個交易日", txt)
    self.assertIn("⚠️ 本週有 2 項計算失敗", txt)


class LiveChecklistTest(unittest.TestCase):
  """User 2026-11-24: ask all real-money questions at once before going live."""

  def test_live_mode_refuses_without_checklist_ack(self):
    from futures_ai.brokers import make_broker
    from futures_ai.live_checklist import ITEMS, LIVE_CHECKLIST_VERSION
    c = cfg(mode="sinopac_live", broker_ai="omega-swing", i_understand_real_money=True)
    with self.assertRaises(SystemExit) as ctx:
      make_broker(c, "omega-swing")
    self.assertIn("緊急停止的範圍", str(ctx.exception))
    self.assertGreaterEqual(len(ITEMS), 13)
    self.assertIn("停機時部位沒有保護", [t for t, _ in ITEMS])
    c2 = cfg(mode="sinopac_live", broker_ai="omega-swing", i_understand_real_money=True,
             live_checklist_ack="old-version")
    with self.assertRaises(SystemExit):
      make_broker(c2, "omega-swing")
    self.assertEqual(make_broker(cfg(), "omega-swing").name, "local_paper")  # Paper mode unaffected.


class SelfReviewTest(unittest.TestCase):
  """User 2026-11-27: each AI reviews what it did wrong this week (selfreview.py)."""

  def test_binomial(self):
    from futures_ai.selfreview import binom_cdf
    self.assertLess(binom_cdf(3, 27, 0.36), 0.005)  # Nova 3/27 vs 36%: clearly worse than expected.
    self.assertGreater(binom_cdf(13, 52, 0.32), 0.05)

  def test_tags(self):
    from futures_ai.lessons import MarketCtx
    from futures_ai.selfreview import tag_trades
    ctx = MarketCtx([])
    T = lambda *a: at(*a)
    tr = [dict(side="B", e=T(2026, 11, 10, 16, 5), x=T(2026, 11, 10, 17, 0), ep=100, xp=-51, net=-1550, kind="SL"),
          dict(side="S", e=T(2026, 11, 10, 20, 45), x=T(2026, 11, 10, 21, 30), ep=100, xp=251, net=-1550, kind="SL")]
    tag_trades(tr, "nova-nightorb", ctx, {}, [])
    self.assertEqual([t["tag"] for t in tr], ["whipsaw", "whipsaw"])
    om = [dict(side="S", e=T(2026, 11, 21, 2, 55), x=T(2026, 11, 23, 8, 45, 12), ep=34691, xp=34963, net=-2758,
               kind="SL"),
          dict(side="B", e=T(2026, 11, 24, 16, 4), x=T(2026, 11, 24, 18, 50), ep=40642, xp=39522, net=-11240, kind="SL",
               reason="60 分K 收盤 40808 突破前 10 根高點 40324，EMA20 39271 > EMA60 38460")]
    locked = {at(2026, 11, 24, 8, 45).isoformat(): {40324.0}}
    starts = [at(2026, 11, 24, 8, 45), at(2026, 11, 24, 15, 0)]
    tag_trades(om, "omega-swing", ctx, locked, starts)
    self.assertEqual([t["tag"] for t in om], ["gap_stop", "limit_breakout"])

  def test_build_and_brief(self):
    from unittest import mock
    from futures_ai import selfreview
    from futures_ai.weekly import line_brief
    live, sh = Ledger(":memory:"), Ledger(":memory:")
    def add(led, ai, side, e, x, ep, xp, pnl, kind="SL", reason=""):
      led.conn.execute("INSERT INTO trades (ai, trading_day, contract, side, qty, entry_ts, entry_price, exit_ts, "
                       "exit_price, points, gross_twd, cost_twd, pnl_twd, exit_kind, entry_reason) VALUES "
                       "(?, '2026-11-26', 'TMF', ?, 1, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)",
                       (ai, side, e, ep, x, xp, (xp - ep) * (1 if side == "B" else -1), pnl, kind, reason))
    for i in range(3):
      add(live, "nova-nightorb", "B" if i % 2 == 0 else "S", f"2026-11-26T15:4{i}:00+08:00", f"2026-11-26T16:0{i}:00+08:00",
          100, -51 if i % 2 == 0 else 251, -1550)
    add(sh, "old-nova-nightorb", "B", "2026-11-25T16:10:00+08:00", "2026-11-25T17:00:00+08:00", 100, 549, 4450, "TP")
    bt = [dict(e=at(2026, 6, 1, 16, 0), x=at(2026, 6, 1, 17, 0), side="B", ep=100, xp=-51, net=-1550, kind="SL")] * 6 + \
         [dict(e=at(2026, 6, 2, 16, 0), x=at(2026, 6, 2, 17, 0), side="B", ep=100, xp=549, net=4450, kind="TP")] * 4
    with mock.patch.object(selfreview, "backtest_trades", return_value=[dict(t) for t in bt]):
      lines, brief = selfreview.build(cfg(), live, [], [], at(2026, 11, 27, 10, 0), sh)
    txt = "\n".join(lines)
    self.assertIn("【Nova】本週 3 筆 -4,650", txt)
    self.assertIn("樣本不足", txt)
    self.assertIn("同一盤兩邊都假突破 3 筆", txt)
    self.assertIn("新版比舊版 -9,100", brief["nova-nightorb"])
    self.assertIn("夜盤過濾：避開 +0／錯過 +4,450", txt)
    b = line_brief(at(2026, 11, 28, 10, 0), [], [], [], [], [], {}, ai_lines=brief)
    self.assertIn("Nova：本週 3 筆 -4,650｜比預期：樣本不足｜主要問題：同一盤兩邊都假突破 3 筆", b)


  def test_alpha_same_entry_different_result(self):
    """11/27 week: live long survived with cap 160 (+1,520); the old version stopped (-1,250) then re-entered."""
    from unittest import mock
    from futures_ai import selfreview
    live, sh = Ledger(":memory:"), Ledger(":memory:")
    def add(led, ai, e, x, pts, pnl, kind):
      led.conn.execute("INSERT INTO trades (ai, trading_day, contract, side, qty, entry_ts, entry_price, exit_ts, "
                       "exit_price, points, gross_twd, cost_twd, pnl_twd, exit_kind) VALUES "
                       "(?, '2026-11-25', 'TMF', 'B', 1, ?, 100, ?, ?, ?, 0, 0, ?, ?)", (ai, e, x, 100 + pts, pts, pnl, kind))
    add(live, "alpha-orb", "2026-11-25T22:35:00+08:00", "2026-11-26T04:59:00+08:00", 156, 1520, "SESSION_END")
    add(sh, "old-alpha-orb", "2026-11-25T22:35:00+08:00", "2026-11-25T22:50:00+08:00", -121, -1250, "SL")
    add(sh, "old-alpha-orb", "2026-11-25T23:00:00+08:00", "2026-11-26T04:59:00+08:00", 156, 1520, "SESSION_END")
    add(sh, "old-sigma-vwap", "2026-11-23T15:30:00+08:00", "2026-11-23T20:18:00+08:00", 5, 12, "SL")  # breakeven
    with mock.patch.object(selfreview, "backtest_trades", return_value=[]):
      lines, brief = selfreview.build(cfg(), live, [], [], at(2026, 11, 27, 10, 0), sh)
    txt = "\n".join(lines)
    self.assertIn("新版 +1,520 vs 舊版 +270（+1,250）", txt)
    self.assertIn("同一筆結果不同（停損上限 160）+2,770", txt)
    self.assertIn("只有舊版做的單：避開 +0／錯過 +1,520", txt)
    self.assertNotIn("錯過 +12", txt)  # Breakeven exits are not "missed".
