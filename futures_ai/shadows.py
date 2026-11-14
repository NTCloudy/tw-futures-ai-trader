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


PROT_IDS = ("alpha-orb", "sigma-vwap", "nova-nightorb")
PROT_START = "2026-11-16T08:45:00+08:00"  # 10-trading-day comparison: 11/16 day session .. 11/27 night session.
PROT_DAYS = 10


def build_protected_twins(cfg: dict, ledger: Ledger) -> list[AiTrader]:
  """受保護出場對照組: identical live rules, but protected stop exits (stop at/beyond entry, fill within 30 pts)
  are not counted as stop-losses (max_sl / rev_cool / Sigma stop-block). Compared with the live AIs."""
  from .strategies import build_strategies
  out = []
  for s in build_strategies():
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
         "AI｜實盤（現行）｜影子（受保護出場不算停損）｜受保護出場次數"]
  for ai in PROT_IDS:
    a = live.rows("SELECT pnl_twd FROM trades WHERE ai=? AND entry_ts>=?", (ai, PROT_START))
    b = sh.rows("SELECT pnl_twd FROM trades WHERE ai=? AND entry_ts>=?", (f"prot-{ai}", PROT_START))
    n_prot = sum(1 for t in (sh.get_state(f"protected_exits:prot-{ai}") or []) if t >= PROT_START)
    out.append(f"{ai.split('-')[0].capitalize()}｜{sum(r['pnl_twd'] for r in a):+,.0f}（{len(a)} 筆）"
               f"｜{sum(r['pnl_twd'] for r in b):+,.0f}（{len(b)} 筆）｜{n_prot}")
  kills = live.rows("SELECT COUNT(*) n FROM events WHERE level='KILL' AND ts>=?", (PROT_START,))[0]["n"]
  if kills:  # Shadows keep trading during an emergency stop, so that period is not a fair comparison.
    out.append(f"⚠️ 期間曾啟動緊急停止 {kills} 次（實盤停單、影子照跑），該段比較不公平，判讀時請扣除")
  if n_days >= PROT_DAYS:
    out.append("→ 已滿 10 個交易日，請決定是否把「受保護出場不算停損」正式上線（config protected_not_stop）")
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
  return build_allday_twins(cfg, ledger) + build_nova_us_shadow(cfg, ledger) + build_protected_twins(cfg, ledger)
