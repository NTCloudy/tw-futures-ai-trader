import unittest
from datetime import date, datetime, timedelta

from futures_ai.calendar import TPE, active_month, contract_code, rollover_info, session_of, third_wednesday, trading_day
from futures_ai.config import DEFAULTS, _merge
from futures_ai.feed import Bar, Snapshot
from futures_ai.brokers import LocalPaperBroker
from futures_ai.engine import Engine
from futures_ai.ledger import Ledger
from futures_ai.strategies import Entry, OrbBreakout, VwapReversion
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
    self.bars = bars_from([(47000, 47005, 46995, 47000)] * 20)

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
    self.assertEqual(t["points"], 47083 - 47001)  # sell at bid - 1 tick
    cost = 2 * 20 + round(47001 * 10 * 0.00002) + round(47083 * 10 * 0.00002)
    self.assertAlmostEqual(t["pnl_twd"], 82 * 10 - cost)

  def test_spike_between_polls_triggers_take_profit(self):
    self.enter()
    # Last price is back below TP, but the high since the previous poll touched it.
    self.tr.on_price(at(2026, 10, 16, 9, 20), self.td, "TMFJ6",
                     {"last": 47060, "bid": 47059, "ask": 47061, "hi": 47090, "lo": 47055}, 200, False)
    self.assertIsNone(self.tr.pos)
    self.assertEqual(self.ledger.rows("SELECT exit_kind FROM trades")[0]["exit_kind"], "TP")

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
