"""Configuration. Broker secrets come from environment variables only."""

from __future__ import annotations

import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict = {
    # local_paper : real-time TAIFEX quotes + local simulated fills (no account needed)
    # sinopac_sim : 永豐 Shioaji simulation account (simulation=True)
    # sinopac_live: 永豐 Shioaji real account (real money)
    "mode": "local_paper",
    "broker_ai": "alpha-orb",  # In sinopac modes only this AI routes orders to the broker account.
    "i_understand_real_money": False,
    "product": "TMF",
    "point_value": 10,  # NT$ per index point (微型臺指)
    "tick": 1,
    "initial_margin_twd": 35050,  # TAIFEX 原始保證金 for TMF; update when TAIFEX changes it.
    "capital_multiple": 1.5,  # Capital per AI = 1.5 x initial margin.
    "qty": 1,
    "max_daily_loss_twd": 2000,
    "commission_per_side_twd": 20,
    "tax_rate": 0.00002,  # 期交稅 十萬分之二 per side
    "paper_slippage_ticks": 1,
    "roll_days_before": 1,
    "trade_night_session": True,
    "no_entry_minutes_before_close": 20,
    "flatten_minutes_before_close": 5,
    "poll_seconds": 3,
    "stale_seconds": 90,
    "sinopac": {
        "api_key_env": "SINOPAC_API_KEY",
        "secret_key_env": "SINOPAC_SECRET_KEY",
        "ca_path": "certs/Sinopac.pfx",
        "ca_password_env": "SINOPAC_CA_PASSWORD",
        "person_id_env": "SINOPAC_PERSON_ID",
    },
    "publish": {
        "enabled": False,
        "repo": "NTCloudy/tw-futures-ai-trader",
        "branch": "live-state",
        "interval_seconds": 60,
    },
    "db_path": "data/futures.db",
    "dashboard_port": 8766,
}


def _merge(base: dict, override: dict) -> dict:
  out = copy.deepcopy(base)
  for k, v in override.items():
    out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
  return out


def load_config(path: str | Path | None = None) -> dict:
  path = Path(path) if path else ROOT / "config" / "local.json"
  cfg = copy.deepcopy(DEFAULTS)
  if path.exists():
    cfg = _merge(cfg, json.loads(path.read_text(encoding="utf-8")))
  db = Path(cfg["db_path"])
  cfg["db_path"] = str(db if db.is_absolute() or cfg["db_path"] == ":memory:" else ROOT / db)
  cfg["capital_per_ai_twd"] = round(cfg["initial_margin_twd"] * cfg["capital_multiple"])
  return cfg
