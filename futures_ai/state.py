"""Builds the dashboard state (JSON) from the ledger. Used by the local server and the publisher."""

from __future__ import annotations

from datetime import datetime, timedelta

from .calendar import TPE, now_tpe, rollover_info, session_of, trading_day
from .strategies import build_strategies
from .learning import REGIME_ZH, Brain
from .trader import KIND_ZH

MODE_LABEL = {"local_paper": "本機模擬（TAIFEX 即時行情）", "sinopac_sim": "永豐模擬帳戶", "sinopac_live": "永豐真實帳戶"}


def _equity_series(ledger, ai: str) -> list[tuple[datetime, float]]:
  out = []
  for r in ledger.rows("SELECT * FROM equity WHERE ai=? ORDER BY ts", (ai,)):
    ts = datetime.fromisoformat(r["ts"])
    out.append((ts if ts.tzinfo else ts.replace(tzinfo=TPE), r["realized_total"] + r["unrealized"]))
  return out


def _value_at(series, t: datetime) -> float:
  v = 0.0
  for ts, val in series:
    if ts <= t:
      v = val
    else:
      break
  return v


def build_state(cfg: dict, ledger, now: datetime | None = None) -> dict:
  now = now or now_tpe()
  td = trading_day(now).isoformat()
  sess, s_start, s_end = session_of(now)
  market = ledger.get_state("market", {}) or {}
  last = market.get("last") or 0.0
  cap = cfg["capital_per_ai_twd"]
  names = {s.id: s for s in build_strategies()}
  ais = []
  combined = {"today_pnl": 0.0, "total_pnl": 0.0, "pnl_1h": 0.0, "pnl_3h": 0.0, "pnl_6h": 0.0}

  for ai_id, s in names.items():
    pos = ledger.get_state(f"pos:{ai_id}")
    unreal = 0.0
    if pos and last:
      d = 1 if pos["side"] == "B" else -1
      exit_cost = cfg["commission_per_side_twd"] * pos["qty"] + round(last * cfg["point_value"] * pos["qty"] * cfg["tax_rate"])
      unreal = (last - pos["entry_price"]) * d * cfg["point_value"] * pos["qty"] - pos["entry_cost"] - exit_cost
    realized_today = ledger.realized(ai_id, td)
    realized_total = ledger.realized(ai_id)
    series = _equity_series(ledger, ai_id)
    current = realized_total + unreal
    series_now = series + [(now, current)]
    trades_today = ledger.rows("SELECT * FROM trades WHERE ai=? AND trading_day=? ORDER BY id", (ai_id, td))
    wins = sum(1 for t in trades_today if t["pnl_twd"] > 0)

    hourly = []
    start_of_day = datetime.combine(datetime.fromisoformat(td).date(), datetime.min.time(), TPE) - timedelta(hours=9)
    pts = [(t, v) for t, v in series_now if t >= start_of_day]
    if pts:
      hour = pts[0][0].replace(minute=0, second=0, microsecond=0)
      while hour <= now:
        nxt = hour + timedelta(hours=1)
        if any(hour <= t < nxt for t, _ in pts):
          hourly.append({"hour": hour.strftime("%m/%d %H:00"),
                         "pnl": round(_value_at(series_now, min(nxt, now)) - _value_at(series_now, hour - timedelta(seconds=1)))})
        hour = nxt

    item = {
        "id": ai_id, "name": s.name, "style": s.style, "color": s.color,
        "broker": cfg["mode"] if (cfg["mode"] != "local_paper" and ai_id == cfg["broker_ai"]) else "local_paper",
        "position": {**pos, "unrealized": round(unreal)} if pos else None,
        "today_pnl": round(realized_today + unreal), "today_realized": round(realized_today),
        "total_pnl": round(current), "equity": round(cap + current), "roi_pct": current / cap * 100,
        "daily_loss_used_pct": max(0.0, -(realized_today + unreal)) / cfg["max_daily_loss_twd"] * 100,
        "locked_today": ledger.get_state(f"lock:{ai_id}") == td,
        "trades_today": len(trades_today), "wins_today": wins,
        "pnl_1h": round(current - _value_at(series, now - timedelta(hours=1))),
        "pnl_3h": round(current - _value_at(series, now - timedelta(hours=3))),
        "pnl_6h": round(current - _value_at(series, now - timedelta(hours=6))),
        "hourly": hourly,
        "brain": Brain(ledger, s).summary(),
        "last_skip": ledger.get_state(f"skip:{ai_id}"),
        "curve": [{"ts": t.strftime("%H:%M"), "v": round(v)} for t, v in pts][-600:],
    }
    for k in combined:
      combined[k] += item[k]
    ais.append(item)

  orders = ledger.rows("SELECT * FROM orders WHERE trading_day=? ORDER BY id DESC LIMIT 200", (td,))
  for o in orders:
    o["ai_name"] = names[o["ai"]].name if o["ai"] in names else o["ai"]
    o["kind_zh"] = KIND_ZH.get(o["kind"], o["kind"])
  trades = ledger.rows("SELECT * FROM trades WHERE trading_day=? ORDER BY id DESC", (td,))
  for t in trades:
    t["ai_name"] = names[t["ai"]].name if t["ai"] in names else t["ai"]
    t["exit_kind_zh"] = KIND_ZH.get(t["exit_kind"], t["exit_kind"])
  events = ledger.rows("SELECT * FROM events ORDER BY id DESC LIMIT 40")
  journal = []
  for ai_id, s in names.items():
    for j in (ledger.get_state(f"brain:{ai_id}") or {}).get("journal", []):
      journal.append({**j, "ai_name": s.name, "mode_label": getattr(s, "modes", {}).get(j["mode"], j["mode"]),
                      "regime_zh": REGIME_ZH.get(j["regime"], j["regime"])})
  journal.sort(key=lambda j: j["ts"], reverse=True)

  return {
      "generated_at": now.isoformat(timespec="seconds"),
      "mode": cfg["mode"], "mode_label": MODE_LABEL.get(cfg["mode"], cfg["mode"]),
      "product": cfg["product"], "point_value": cfg["point_value"],
      "capital_per_ai": cap, "max_daily_loss": cfg["max_daily_loss_twd"],
      "initial_margin": cfg["initial_margin_twd"],
      "rollover": rollover_info(now, cfg["product"], cfg["roll_days_before"]),
      "session": {"name": sess, "open": s_start.isoformat() if s_start else None,
                  "close": s_end.isoformat() if s_end else None},
      "market": market, "kill_switch": ledger.get_state("kill_switch", False),
      "ais": ais, "combined": {k: round(v) for k, v in combined.items()},
      "orders_today": orders, "trades_today": trades, "events": events, "journal": journal[:40],
  }
