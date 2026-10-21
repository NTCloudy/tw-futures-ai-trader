import unittest
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
    self.assertIn("sh1-nova-nightorb", ids)  # 2-hour Nova control.
    allday = [t for t in sh if t.ai.startswith("allday-")]
    self.assertEqual(sorted(t.ai for t in allday), ["allday-alpha-orb", "allday-sigma-vwap"])
    self.assertTrue(all(not t.s.hours for t in allday))


class InverseOfTest(unittest.TestCase):

  def test_inverse_mirrors_actual_entries_only(self):
    from futures_ai.strategies import build_strategies, InverseOf
    from futures_ai.trader import ENTRY_BUS
    ids = [x.id for x in build_strategies({})]
    self.assertEqual(ids, ["gamma-scalp", "alpha-orb", "sigma-vwap", "nova-nightorb", "nova-us",
                           "delta-inverse", "omega-swing"])
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
    day = [Bar(at(2026, 10, 16, 9, 0) + timedelta(minutes=5 * i), b.o, b.h, b.l, b.c, b.v) for i, b in enumerate(bars)]
    self.assertIsNone(NightOpenBreakout().decide(day, 0, None))  # day session: never


class NewsRiskTest(unittest.TestCase):

  def test_headline_scoring_direction(self):
    from futures_ai.news import score_headline
    self.assertGreater(score_headline("川普宣布對台灣晶片加徵關稅")[0], 0)
    self.assertLess(score_headline("川普突簽行政命令 豁免多項關稅")[0], 0)
    self.assertGreater(score_headline("China launches missile drills near Taiwan Strait")[0], score_headline("Trump tariff threat")[0])
    self.assertEqual(score_headline("台積電法說會今日登場")[0], 0)

  def test_news_tilt_needs_price_confirmation(self):
    from futures_ai import trader as tm
    c = cfg()
    tr = AiTrader(c, FakeStrategy(None), LocalPaperBroker(c), Ledger(":memory:"))
    flat = bars_from([(47000, 47030, 46970, 47000 + (i % 3) - 1) for i in range(60)])
    rally = bars_from([(47000 + i * 6, 47010 + i * 6, 46998 + i * 6, 47008 + i * 6) for i in range(60)])
    try:
      tm.RISK_STATE.update(mode="elevated", fomc_window=False)
      self.assertIsNone(tr._filter(Entry("B", 40, 80, "x"), flat))  # Elevated news: just keep an eye on it.
      tm.RISK_STATE.update(mode="high", fomc_window=False)
      dip = bars_from([(47000, 47030, 46970, 47000)] * 59 + [(47000, 47000, 46940, 46950)])
      self.assertIn("先不進", tr._filter(Entry("B", 40, 80, "x"), dip))  # Below VWAP during a spike.
      self.assertIsNone(tr._filter(Entry("S", 40, 80, "x"), flat))
      self.assertIsNone(tr._filter(Entry("B", 40, 80, "x"), rally))
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


class ReplayMatchTest(unittest.TestCase):

  def test_match_pairs_same_side_within_window(self):
    from futures_ai.replay import _match
    live = [{"entry": at(2026, 10, 20, 22, 1), "side": "B", "pnl": 1, "kind": "TP"},
            {"entry": at(2026, 10, 20, 23, 0), "side": "S", "pnl": -1, "kind": "SL"}]
    sim = [{"entry": at(2026, 10, 20, 22, 2), "side": "B", "pnl": 1, "kind": "TP"},
           {"entry": at(2026, 10, 21, 1, 0), "side": "S", "pnl": -1, "kind": "SL"}]
    both, lo, so = _match(live, sim)
    self.assertEqual((len(both), len(lo), len(so)), (1, 1, 1))


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
