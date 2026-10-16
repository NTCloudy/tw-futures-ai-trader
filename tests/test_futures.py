import unittest
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
    rng = [(100, 110, 95, 105)] * 15 + [(105, 109, 104, 108)]
    bars = bars_from([(p[0] + 47000, p[1] + 47000, p[2] + 47000, p[3] + 47000) for p in rng + [(108, 120, 107, 118)]])
    e = OrbBreakout().decide(bars, 0, None)
    self.assertIsNotNone(e)
    self.assertEqual(e.side, "B")
    self.assertEqual(e.tp_pts, e.sl_pts * 2)
    self.assertIsNone(OrbBreakout().decide(bars, 2, None))  # max entries per session

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
    self.tr.s.entry = Entry("B", 400, 800, "very wide")
    self.enter()
    self.tr.on_price(at(2026, 10, 16, 9, 20), self.td, "TMFJ6", {"last": 46790, "bid": 46789, "ask": 46791}, 200, False)
    self.assertEqual(self.tr.locked_day(), self.td)
    self.assertIsNone(self.tr.pos)
    self.assertEqual(self.ledger.rows("SELECT exit_kind FROM trades")[0]["exit_kind"], "RISK")
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


if __name__ == "__main__":
  unittest.main()


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
    self.assertIsNotNone(self.brain.state["pause_until"])  # 3 losses in a row -> cool-down
    self.assertTrue(self.brain.paused(now + timedelta(minutes=5)))
    self.assertFalse(self.brain.paused(now + timedelta(minutes=20)))

  def test_giveback_shrinks_take_profit(self):
    lesson = self.brain.review(at(2026, 10, 16, 10, 10), self.pos(mfe=15), 46984, "SL", -220)
    self.assertIn("停利設太遠", lesson["diagnosis"])
    self.assertAlmostEqual(self.brain.state["tp_mult"], 0.9)

  def test_flip_mode_inverts_side_and_blocks_when_both_lose(self):
    st = self.brain.state["stats"]["chop"]
    st["native"].update(n=3, ew=-200)
    st["flip"].update(n=1, ew=-50)
    bars = bars_from([(47000, 47003, 46997, 47000 + (i % 2) * 2) for i in range(25)])
    sig, ctx = self.brain.adjust(Entry("B", 15, 20, "x"), bars)
    self.assertEqual((ctx["regime"], ctx["mode"], sig.side), ("chop", "flip", "S"))
    st["flip"].update(n=3, ew=-150)
    sig, ctx = self.brain.adjust(Entry("B", 15, 20, "x"), bars)
    self.assertIsNone(sig)
    self.brain.daily_decay("2026-10-16")
    self.brain.daily_decay("2026-10-19")  # next day halves evidence -> allowed to retry
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

  def test_learning_mode_removes_caps_and_live_refuses_it(self):
    from futures_ai.brokers import make_broker
    c = cfg(learning_mode=True, max_entries_per_session=1)
    ledger = Ledger(":memory:")
    tr = AiTrader(c, FakeStrategy(Entry("B", 40, 80, "x")), LocalPaperBroker(c), ledger)
    tr.s.decide = lambda bars, n, since: Entry("B", 40, 80, "x")
    ledger.set_state("cnt:alpha-orb:k", 50)
    bars = bars_from([(47000, 47030, 46970, 47000)] * 20)
    tr.on_bar(at(2026, 10, 16, 9, 10), "2026-10-16", "TMFJ6", "k", bars, {"last": 47000, "bid": 46999, "ask": 47000}, 200, True, False)
    self.assertIsNotNone(tr.pos)  # cap of 1 ignored in learning mode
    with self.assertRaises(SystemExit):
      make_broker(cfg(mode="sinopac_live", i_understand_real_money=True, learning_mode=True), "alpha-orb")


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
    self.assertIn("昨晚交易", sent[0])
    n.maybe_send_night(at(2026, 10, 17, 9, 0))
    self.assertEqual(len(sent), 1)  # only once
