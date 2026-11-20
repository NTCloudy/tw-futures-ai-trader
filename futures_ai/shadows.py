"""Background "shadow" variants: extra paper traders with different parameters, fed the same live
quotes, recorded in a separate ledger (data/shadow.db) and never shown on the dashboard. They
multiply the amount of real-time experience; the weekly job ranks them for promotion."""

from __future__ import annotations

import copy

from .brokers import LocalPaperBroker
from .config import ROOT
from .ledger import Ledger
from .strategies import NightOpenBreakout
from .trader import AiTrader

VARIANTS: list = []


def shadow_ledger() -> Ledger:
  return Ledger(str(ROOT / "data" / "shadow.db"))


TWIN_TP_MULT = 1.0  # "停利縮小對照組": same strategy, take-profit = 1.0x stop.


def build_tp_twins(cfg: dict, ledger: Ledger) -> list[AiTrader]:
  """One twin per live AI: identical rules/params, only the take-profit is shrunk to 1x the stop."""
  from .strategies import build_strategies
  out = []
  for s in build_strategies():
    if getattr(s, "fixed_bracket", False) or getattr(s, "hidden", False):
      continue  # Fixed-point brackets have no take-profit multiple to shrink.
    s.base_id, s.id, s.name = s.id, f"tp1-{s.id}", f"{s.name}（停利 {TWIN_TP_MULT:g} 倍）"
    s.tp_mult = TWIN_TP_MULT
    out.append(AiTrader(cfg, s, LocalPaperBroker(cfg), ledger))
  return out


ALLDAY_IDS = ("alpha-orb", "sigma-vwap", "nova-nightorb")  # All-day / full-night control twins.


def build_allday_twins(cfg: dict, ledger: Ledger) -> list[AiTrader]:
  """不限時段對照組: Alpha / Sigma / Nova with identical rules and parameters, but no time-of-day skip."""
  from datetime import datetime
  from .calendar import TPE
  from .strategies import build_strategies
  if not ledger.get_state("allday_since"):
    ledger.set_state("allday_since", datetime.now(TPE).isoformat(timespec="seconds"))
  out = []
  for s in build_strategies():
    if s.id not in ALLDAY_IDS:
      continue
    s.base_id, s.id, s.name = s.id, f"allday-{s.id}", f"{s.name}（不限時段）"
    s.hours = None
    s.bonus_hours = None
    s.day_min_atr = None
    s.skip_window = None
    s.dst_skip = None
    out.append(AiTrader(cfg, s, LocalPaperBroker(cfg), ledger))
  return out


# ---- Previous live version ("old-" shadows), user decision 2026-11-18 ----
# Live switched to plan A2 (Alpha stop cap 160), A+B (Sigma night filter + sl_atr 2.5/3.0 in the weekly grid) and
# A (Nova night filter). The version that was live until then keeps running as a shadow with its parameters frozen
# as they were at the switch, so the new live version can be compared forward on the same quotes.
SWITCH_TS = "2026-11-18T13:46:55+08:00"  # Engine restart that put the new version live.
OLD_PARAMS = {
    "alpha-orb": {"range_bars": 3, "sl_frac": 0.75, "tp_mult": 3.0},
    "sigma-vwap": {"pull_atr": 0.5, "sl_atr": 2.0, "tp_mult": 4.0, "trail_atr": None},
    "nova-nightorb": {"range_min": 30, "window": 600, "sl_frac": 1.0, "tp_mult": 3.0},
    "lessons": {"alpha-orb": {"rev_cool": 30},
                "sigma-vwap": {"min_er": 0.3, "max_vwap_pts": 200, "rev_cool": 30, "max_sl": 2}},
}
OLD_IDS = ("alpha-orb", "sigma-vwap", "nova-nightorb")
# Red light (weekly report): rolling live loss over the last 20 trading days (new version only) beyond the
# one-year backtest max drawdown of the new version -> "🔴 建議停用、交給使用者決定".
RED_DD = {"alpha-orb": 8622, "sigma-vwap": 18036, "nova-nightorb": 12352}
RED_DAYS = 20


def old_strategies() -> list:
  """The live Alpha / Sigma / Nova exactly as they were before SWITCH_TS (frozen parameters, old rules)."""
  import copy as _copy
  from .strategies import build_strategies
  out = []
  for s in build_strategies(_copy.deepcopy(OLD_PARAMS)):
    if s.id not in OLD_IDS:
      continue
    if s.id == "alpha-orb":
      s.sl_cap = 120
    s.night_eff = None
    out.append(s)
  return out


def build_old_twins(cfg: dict, ledger: Ledger) -> list[AiTrader]:
  out = []
  for s in old_strategies():
    s.base_id, s.id, s.name = s.id, f"old-{s.id}", f"{s.name}（舊版對照）"
    out.append(AiTrader(cfg, s, LocalPaperBroker(cfg), ledger))
  return out


def version_compare_lines(live: Ledger, shadow: Ledger | None = None, now=None) -> list[str]:
  """Weekly report: new live version vs the previous version (old- shadows) since SWITCH_TS."""
  from datetime import datetime
  from .calendar import TPE
  now = now or datetime.now(TPE)
  if now < datetime.fromisoformat(SWITCH_TS):
    return []
  sh = shadow or shadow_ledger()
  out = ["", f"🆚 新版實盤 vs 舊版影子（{SWITCH_TS[5:10].replace('-', '/')} 起，同一段行情）", "AI｜新版（實盤）｜舊版（影子）"]
  for ai in OLD_IDS:
    a = live.rows("SELECT pnl_twd FROM trades WHERE ai=? AND entry_ts>=?", (ai, SWITCH_TS))
    b = sh.rows("SELECT pnl_twd FROM trades WHERE ai=? AND entry_ts>=?", (f"old-{ai}", SWITCH_TS))
    out.append(f"{ai.split('-')[0].capitalize()}｜{sum(r['pnl_twd'] for r in a):+,.0f}（{len(a)} 筆）"
               f"｜{sum(r['pnl_twd'] for r in b):+,.0f}（{len(b)} 筆）")
  out.append("註：舊版影子的參數與經驗規則凍結在改版當時；之後新版若再換參數或採用新經驗規則，兩者差異就不只 A/A2/B。"
             "不限時段版與 Nova 美股開盤影子自 11/18 起也跟著換成新規則，它們的前後比較在 11/18 斷開。")
  return out


def red_light_lines(live: Ledger, now=None, expect_hist: list | None = None) -> list[str]:
  """Weekly report: agreed stop criteria for the new version: rolling 20 trading days vs backtest max DD, or the
  out-of-sample expectation negative in the last two weekly reports (expect_hist: [{ai: per-20-days}, ...])."""
  from datetime import datetime, timedelta
  from .calendar import TPE, is_business_day
  now = now or datetime.now(TPE)
  d, days = now.date(), []
  while len(days) < RED_DAYS:
    if is_business_day(d):
      days.append(d)
    d -= timedelta(days=1)
  since = max(SWITCH_TS, days[-1].isoformat())
  out = ["", f"🚦 停用紅燈（新版實盤近 {RED_DAYS} 個交易日虧損超過一年回測最大回落 → 建議停用、交給你決定）"]
  for ai in OLD_IDS:
    rs = live.rows("SELECT pnl_twd FROM trades WHERE ai=? AND entry_ts>=?", (ai, since))
    tot = sum(r["pnl_twd"] for r in rs)
    last2 = [h.get(ai) for h in (expect_hist or [])[-2:]]
    neg2 = len(last2) == 2 and all(v is not None and v < 0 for v in last2)
    why = []
    if tot <= -RED_DD[ai]:
      why.append("虧損超過紅線")
    if neg2:
      why.append("連續 2 週樣本外預期為負")
    flag = f"🔴 建議停用，請決定（{'、'.join(why)}）" if why else "🟢"
    out.append(f"{ai.split('-')[0].capitalize()}：{tot:+,.0f}（紅線 −{RED_DD[ai]:,}）{flag}")
  return out


# ---- Omega early-breakeven shadow (user decision 2026-11-20, not recommended by the backtest) ----
# Same live Omega, but the stop moves to breakeven (+6) once the open profit reaches 0.5 x the initial stop
# (~600 pts) instead of 0.7 x (~840 pts). One-year backtest: wins 49->46, breakeven 8->20, losses 26->20,
# -34k/year, DD 18.6k->25.3k. Paper only. The two versions differ only when a trade's open profit reaches 600-840 pts
# and then turns back (rare), so the comparison runs until BE05_MIN_DIFF trades differ or BE05_DAYS trading days.
BE05_ID = "be05-omega-swing"
BE05_START = "2026-11-20T13:47:19+08:00"  # Engine restart that added the shadow (updated to the actual time).
BE05_DAYS = 40
BE05_MIN_DIFF = 5


def build_be05_omega(cfg: dict, ledger: Ledger) -> list[AiTrader]:
  from .strategies import build_strategies
  s = [x for x in build_strategies() if x.id == "omega-swing"][0]
  s.base_id, s.id, s.name = s.id, BE05_ID, f"{s.name}（提早保本 0.5 影子）"
  s.be_r = 0.5
  return [AiTrader(cfg, s, LocalPaperBroker(cfg), ledger)]


def be05_lines(live: Ledger, shadow: Ledger | None = None, now=None) -> list[str]:
  """Weekly report: live Omega vs the early-breakeven shadow since BE05_START (win / breakeven / loss)."""
  from datetime import datetime
  from .calendar import TPE, is_business_day
  now = now or datetime.now(TPE)
  start = datetime.fromisoformat(BE05_START)
  if now < start:
    return []
  sh = shadow or shadow_ledger()
  d, n_days = start.date(), 0
  while d <= now.date():
    n_days += is_business_day(d)
    d = d.fromordinal(d.toordinal() + 1)

  def row(rows):
    pts = [r["points"] for r in rows]
    w = sum(p > 10 for p in pts); b = sum(-10 <= p <= 10 for p in pts); l = sum(p < -10 for p in pts)
    return f"{sum(r['pnl_twd'] for r in rows):+,.0f}（賺 {w}／打平 {b}／賠 {l}）"
  a = live.rows("SELECT entry_ts, points, pnl_twd FROM trades WHERE ai='omega-swing' AND entry_ts>=?", (BE05_START,))
  b = sh.rows("SELECT entry_ts, points, pnl_twd FROM trades WHERE ai=? AND entry_ts>=?", (BE05_ID, BE05_START))
  # A differing outcome: same entry minute with a different result, or a trade only one side has.
  ka = {r["entry_ts"][:16]: r["points"] for r in a}
  kb = {r["entry_ts"][:16]: r["points"] for r in b}
  n_diff = sum(1 for k in set(ka) | set(kb) if ka.get(k) != kb.get(k))
  # Root differences: same entry minute, different exit. After one, later re-entries may drift apart and be
  # counted in n_diff too, so the decision uses the root count.
  n_root = sum(1 for k in set(ka) & set(kb) if ka[k] != kb[k])
  out = ["", f"🧪 Omega 提早保本影子（{BE05_START[5:10].replace('-', '/')} 起，第 {n_days} 個交易日，"
             f"源頭差異 {n_root}/{BE05_MIN_DIFF} 次、結果不同共 {n_diff} 筆）",
         f"實盤（保本 0.7）：{row(a)}", f"影子（保本 0.5）：{row(b)}",
         "註：兩版只在浮盈 600～840 點後回頭時才不同；只比較起始日之後的新進場單。"]
  if n_root >= BE05_MIN_DIFF or n_days >= BE05_DAYS:
    out.append("→ 比較已足夠（差異滿 5 筆或滿 40 個交易日），請決定是否改用（回測不建議：一年約少賺 34k、回撤較大）")
  elif n_days >= 10 and n_root == 0:
    out.append("→ 已 10 個交易日但兩版尚無差異，繼續觀察")
  return out


PROT_IDS = ("alpha-orb", "sigma-vwap", "nova-nightorb")
PROT_START = "2026-11-16T08:45:00+08:00"  # 10-trading-day comparison: 11/16 day session .. 11/27 night session.
PROT_DAYS = 10


def build_protected_twins(cfg: dict, ledger: Ledger) -> list[AiTrader]:
  """受保護出場對照組: the rules that were live when the comparison started (since SWITCH_TS = the old version,
  so the comparison is not disturbed by the 11/18 switch), but protected stop exits (stop at/beyond entry, fill
  within 30 pts) are not counted as stop-losses (max_sl / rev_cool / Sigma stop-block). Compared with the live
  AIs before SWITCH_TS and with the old- shadows after it."""
  out = []
  for s in old_strategies():
    if s.id not in PROT_IDS:
      continue
    s.base_id, s.id, s.name = s.id, f"prot-{s.id}", f"{s.name}（受保護出場不算停損）"
    s.protected_not_stop = True
    out.append(AiTrader(cfg, s, LocalPaperBroker(cfg), ledger))
  return out


def protected_shadow_lines(live: Ledger, shadow: Ledger | None = None, now=None) -> list[str]:
  """Weekly report: live AI vs its prot- twin since PROT_START (same quotes, only the stop classification differs)."""
  from datetime import datetime
  from .calendar import TPE, is_business_day
  now = now or datetime.now(TPE)
  start = datetime.fromisoformat(PROT_START)
  if now < start:
    return []
  sh = shadow or shadow_ledger()
  d, n_days = start.date(), 0
  while d <= now.date():
    n_days += is_business_day(d)
    d = d.fromordinal(d.toordinal() + 1)
  out = ["", f"🧪 受保護出場影子比對（{PROT_START[5:10].replace('-', '/')} 起，第 {min(n_days, PROT_DAYS)}/{PROT_DAYS} 個交易日）",
         "AI｜原版（11/18 改版前＝實盤，之後＝舊版影子）｜影子（受保護出場不算停損）｜受保護出場次數"]
  for ai in PROT_IDS:
    a = live.rows("SELECT pnl_twd FROM trades WHERE ai=? AND entry_ts>=? AND entry_ts<?", (ai, PROT_START, SWITCH_TS))
    a += sh.rows("SELECT pnl_twd FROM trades WHERE ai=? AND entry_ts>=?", (f"old-{ai}", SWITCH_TS))
    b = sh.rows("SELECT pnl_twd FROM trades WHERE ai=? AND entry_ts>=?", (f"prot-{ai}", PROT_START))
    n_prot = sum(1 for t in (sh.get_state(f"protected_exits:prot-{ai}") or []) if t >= PROT_START)
    out.append(f"{ai.split('-')[0].capitalize()}｜{sum(r['pnl_twd'] for r in a):+,.0f}（{len(a)} 筆）"
               f"｜{sum(r['pnl_twd'] for r in b):+,.0f}（{len(b)} 筆）｜{n_prot}")
  kills = live.rows("SELECT COUNT(*) n FROM events WHERE level='KILL' AND ts>=?", (PROT_START,))[0]["n"]
  if kills:  # Shadows keep trading during an emergency stop, so that period is not a fair comparison.
    out.append(f"⚠️ 期間曾啟動緊急停止 {kills} 次（實盤停單、影子照跑），該段比較不公平，判讀時請扣除")
  if n_days >= PROT_DAYS:
    out.append("→ 已滿 10 個交易日，請決定是否把「受保護出場不算停損」正式上線（config protected_not_stop）；"
               "注意：這是在改版前的規則上比出來的，上線前需在新版規則上再回測確認")
  return out


def build_nova_us_shadow(cfg: dict, ledger: Ledger) -> list[AiTrader]:
  """Nova US (entries only in the first hour after the US cash open) as a background shadow."""
  from .strategies import build_nova_us
  s = build_nova_us()
  s.base_id = "nova-nightorb"
  return [AiTrader(cfg, s, LocalPaperBroker(cfg), ledger)]


def build_shadows(cfg: dict, ledger: Ledger | None = None) -> list[AiTrader]:
  ledger = ledger or shadow_ledger()
  cfg = copy.deepcopy(cfg)
  cfg["mode"] = "local_paper"
  return (build_allday_twins(cfg, ledger) + build_nova_us_shadow(cfg, ledger) + build_protected_twins(cfg, ledger)
          + build_old_twins(cfg, ledger) + build_be05_omega(cfg, ledger))
