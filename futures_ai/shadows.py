"""Background "shadow" variants: extra paper traders with different parameters, fed the same live
quotes, recorded in a separate ledger (data/shadow.db) and never shown on the dashboard. They
multiply the amount of real-time experience; the weekly job ranks them for promotion."""

from __future__ import annotations

import copy

from .brokers import LocalPaperBroker
from .config import ROOT
from .ledger import Ledger
from .strategies import OrbBreakout, TrendBreakout, TrendPullback
from .trader import AiTrader

VARIANTS = [
    (OrbBreakout, {"range_bars": 6, "sl_frac": 0.75, "tp_mult": 3.0}),
    (OrbBreakout, {"range_bars": 3, "sl_frac": 0.5, "tp_mult": 2.0}),
    (OrbBreakout, {"range_bars": 3, "sl_frac": 0.75, "tp_mult": 1.5}),
    (TrendPullback, {"pull_atr": 0.2, "sl_atr": 2.0, "tp_mult": 3.0}),
    (TrendPullback, {"pull_atr": 0.5, "sl_atr": 1.5, "tp_mult": 3.0}),
    (TrendPullback, {"pull_atr": 0.5, "sl_atr": 2.0, "tp_mult": 4.0, "trail_atr": 3.0}),
    (TrendBreakout, {"lookback": 6, "sl_atr": 2.0, "trail_atr": 4.0}),
    (TrendBreakout, {"lookback": 24, "sl_atr": 2.0, "trail_atr": 4.0}),
    (TrendBreakout, {"lookback": 12, "sl_atr": 3.0, "trail_atr": 3.0}),
]


def shadow_ledger() -> Ledger:
  return Ledger(str(ROOT / "data" / "shadow.db"))


TWIN_TP_MULT = 1.0  # "停利縮小對照組": same strategy, take-profit = 1.0x stop.


def build_tp_twins(cfg: dict, ledger: Ledger) -> list[AiTrader]:
  """One twin per live AI: identical rules/params, only the take-profit is shrunk to 1x the stop."""
  from .strategies import build_strategies
  out = []
  for s in build_strategies():
    if getattr(s, "fixed_bracket", False):
      continue  # Fixed-point brackets have no take-profit multiple to shrink.
    s.base_id, s.id, s.name = s.id, f"tp1-{s.id}", f"{s.name}（停利 {TWIN_TP_MULT:g} 倍）"
    s.tp_mult = TWIN_TP_MULT
    out.append(AiTrader(cfg, s, LocalPaperBroker(cfg), ledger))
  return out


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
  return out + build_tp_twins(cfg, ledger)
