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

VARIANTS = [
    # 2026-10-21: the old Alpha / Sigma / Gamma parameter variants and the take-profit twins were
    # retired (conclusions reached). Kept: the original 2-hour Nova as a control.
    (NightOpenBreakout, {"window": 120}),
]


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


ALLDAY_IDS = ("alpha-orb", "sigma-vwap")  # Live AIs with a time-of-day filter.


def build_allday_twins(cfg: dict, ledger: Ledger) -> list[AiTrader]:
  """不限時段對照組: Alpha / Sigma with identical rules and parameters, but no time-of-day filter."""
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
    out.append(AiTrader(cfg, s, LocalPaperBroker(cfg), ledger))
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
  out = []
  for i, (cls, params) in enumerate(VARIANTS, 1):
    s = cls(**params)
    s.base_id = cls.id
    s.id = f"sh{i}-{cls.id}"
    s.name = f"影子{i} {cls.name}"
    s.params = params
    out.append(AiTrader(cfg, s, LocalPaperBroker(cfg), ledger))
  return out + build_allday_twins(cfg, ledger) + build_nova_us_shadow(cfg, ledger)
