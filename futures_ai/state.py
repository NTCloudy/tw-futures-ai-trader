"""Builds the dashboard state (JSON) from the ledger. Used by the local server and the publisher."""

from __future__ import annotations

from datetime import datetime, timedelta

from .calendar import TPE, now_tpe, rollover_info, session_of, trading_day
from .strategies import build_strategies
from .learning import REGIME_ZH, Brain
from .notify import load_cred
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


def health(trades: list[dict]) -> dict:
  """Strategy health metrics from closed trades."""
  n = len(trades)
  if not n:
    return {"n": 0}
  pnls = [t["pnl_twd"] for t in trades]
  wins = [p for p in pnls if p > 0]
  losses = [p for p in pnls if p <= 0]
  eq = peak = mdd = 0.0
  for p in pnls:
    eq += p
    peak = max(peak, eq)
    mdd = max(mdd, peak - eq)
  gross_win, gross_loss = sum(wins), -sum(losses)
  return {"n": n, "win_rate": len(wins) / n * 100,
          "avg_win": gross_win / len(wins) if wins else 0, "avg_loss": -gross_loss / len(losses) if losses else 0,
          "profit_factor": (gross_win / gross_loss) if gross_loss else None,
          "expectancy": sum(pnls) / n, "max_dd": mdd,
          "avg_points": sum(t["points"] for t in trades) / n}


def build_state(cfg: dict, ledger, now: datetime | None = None) -> dict:
  now = now or now_tpe()
  td = trading_day(now).isoformat()
  sess, s_start, s_end = session_of(now)
  market = ledger.get_state("market", {}) or {}
  last = market.get("last") or 0.0
  cap = cfg["capital_per_ai_twd"]
  names = {s.id: s for s in build_strategies() if not getattr(s, "hidden", False)}
  # Session shown on the intraday chart: the current one, or the most recent one while the market is closed.
  cs_name, cs_start, cs_end = sess, s_start, s_end
  probe = now
  while cs_start is None and probe > now - timedelta(days=4):
    probe -= timedelta(minutes=30)
    cs_name, cs_start, cs_end = session_of(probe)
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
        "locked_today": cfg.get("enforce_daily_loss", True) and ledger.get_state(f"lock:{ai_id}") == td,
        "trades_today": len(trades_today), "wins_today": wins,
        "pnl_1h": round(current - _value_at(series, now - timedelta(hours=1))),
        "pnl_3h": round(current - _value_at(series, now - timedelta(hours=3))),
        "pnl_6h": round(current - _value_at(series, now - timedelta(hours=6))),
        "hourly": hourly,
        "brain": Brain(ledger, s).summary(),
        "health": health(ledger.rows("SELECT * FROM trades WHERE ai=? AND entry_ts>=? ORDER BY id",
                                     (ai_id, ledger.get_state(f"strategy_since:{ai_id}", "2000")))),
        "health_since": ledger.get_state(f"strategy_since:{ai_id}"),
        "last_skip": ledger.get_state(f"skip:{ai_id}"),
        # P&L of this session only (realized + unrealized), measured from the session open.
        "curve": ([{"ts": t.strftime("%H:%M"), "v": round(v - _value_at(series_now, cs_start - timedelta(seconds=1)))}
                   for t, v in series_now if cs_start <= t <= cs_end][-900:] if cs_start else []),
    }
    for k in combined:
      combined[k] += item[k]
    ais.append(item)

  orders = ledger.rows("SELECT * FROM orders WHERE trading_day=? ORDER BY id DESC LIMIT 200", (td,))
  for o in orders:
    o["ai_name"] = names[o["ai"]].name if o["ai"] in names else o["ai"]
    o["kind_zh"] = KIND_ZH.get(o["kind"], o["kind"])
  trades = [t for t in ledger.rows("SELECT * FROM trades WHERE trading_day=? ORDER BY id DESC", (td,))
            if t["ai"] in names]  # Hidden / retired AIs are not shown or counted.
  for t in trades:
    t["ai_name"] = names[t["ai"]].name if t["ai"] in names else t["ai"]
    t["exit_kind_zh"] = KIND_ZH.get(t["exit_kind"], t["exit_kind"])
  events = ledger.rows("SELECT * FROM events ORDER BY id DESC LIMIT 40")
  # Per trading day x session history (日盤 / 夜盤) for every AI.
  hist: dict[tuple, dict] = {}
  for t in ledger.rows("SELECT ai, trading_day, entry_ts, pnl_twd FROM trades ORDER BY id"):
    if t["ai"] not in names:
      continue
    hh = t["entry_ts"][11:16]
    sess_name = "day" if "08:45" <= hh < "13:45" else "night"
    row = hist.setdefault((t["trading_day"], sess_name), {"trading_day": t["trading_day"], "session": sess_name,
                                                          "pnl": {}, "trades": {}, "total": 0.0})
    row["pnl"][t["ai"]] = row["pnl"].get(t["ai"], 0.0) + t["pnl_twd"]
    row["trades"][t["ai"]] = row["trades"].get(t["ai"], 0) + 1
    row["total"] += t["pnl_twd"]
  daily_history = sorted(hist.values(), key=lambda r: (r["trading_day"], r["session"] == "day"), reverse=True)
  journal = []
  for ai_id, s in names.items():
    for j in (ledger.get_state(f"brain:{ai_id}") or {}).get("journal", []):
      journal.append({**j, "ai_name": s.name, "mode_label": getattr(s, "modes", {}).get(j["mode"], j["mode"]),
                      "regime_zh": REGIME_ZH.get(j["regime"], j["regime"])})
  journal.sort(key=lambda j: j["ts"], reverse=True)

  # 停利縮小對照組: live AI vs its twin (same rules, take-profit = 1x stop), counted from the same start time.
  tp_compare = []
  from .config import ROOT
  shadow_path = ROOT / "data" / "shadow.db"
  if shadow_path.exists() and cfg["db_path"] != ":memory:":
    from .ledger import Ledger as _Ledger
    sl = _Ledger(str(shadow_path))
    since = sl.get_state("twin_since")
    if since:
      for ai_id, strat in names.items():
        live_h = health(ledger.rows("SELECT * FROM trades WHERE ai=? AND entry_ts>=? ORDER BY id", (ai_id, since)))
        twin_h = health(sl.rows("SELECT * FROM trades WHERE ai=? AND entry_ts>=? ORDER BY id", (f"tp1-{ai_id}", since)))
        live_pnl = sum(t["pnl_twd"] for t in ledger.rows("SELECT pnl_twd FROM trades WHERE ai=? AND entry_ts>=?", (ai_id, since)))
        twin_pnl = sum(t["pnl_twd"] for t in sl.rows("SELECT pnl_twd FROM trades WHERE ai=? AND entry_ts>=?", (f"tp1-{ai_id}", since)))
        tp_compare.append({"id": ai_id, "name": strat.name, "color": strat.color,
                           "current_tp": getattr(strat, "tp_mult", None), "live": {**live_h, "pnl": live_pnl},
                           "twin": {**twin_h, "pnl": twin_pnl}})

  # 不限時段對照組: live Alpha / Sigma (time-of-day filter) vs the same AI trading at any time.
  hours_compare = []
  if shadow_path.exists() and cfg["db_path"] != ":memory:":
    since2 = sl.get_state("allday_since")
    if since2:
      for ai_id in ("alpha-orb", "sigma-vwap"):
        if ai_id not in names:
          continue
        strat = names[ai_id]
        lr = ledger.rows("SELECT * FROM trades WHERE ai=? AND entry_ts>=? ORDER BY id", (ai_id, since2))
        sr = sl.rows("SELECT * FROM trades WHERE ai=? AND entry_ts>=? ORDER BY id", (f"allday-{ai_id}", since2))
        hours_compare.append({"id": ai_id, "name": strat.name, "color": strat.color,
                              "hours": list(getattr(strat, "hours", None) or []),
                              "live": {**health(lr), "pnl": sum(t["pnl_twd"] for t in lr)},
                              "allday": {**health(sr), "pnl": sum(t["pnl_twd"] for t in sr)}})

  return {
      "tp_compare": tp_compare,
      "hours_compare": hours_compare,
      "news": ledger.get_state("news"),
      "generated_at": now.isoformat(timespec="seconds"),
      "mode": cfg["mode"], "mode_label": MODE_LABEL.get(cfg["mode"], cfg["mode"]),
      "product": cfg["product"], "point_value": cfg["point_value"],
      "capital_per_ai": cap, "max_daily_loss": cfg["max_daily_loss_twd"],
      "enforce_daily_loss": cfg.get("enforce_daily_loss", True) and not cfg.get("learning_mode"),
      "learning_mode": bool(cfg.get("learning_mode")),
      "initial_margin": cfg["initial_margin_twd"],
      "margin_live": ledger.get_state("margin"),
      "line_enabled": bool(cfg["notify"].get("line_enabled")) and load_cred() is not None,
      "rollover": rollover_info(now, cfg["product"], cfg["roll_days_before"]),
      "curve_session": {"name": cs_name, "open": cs_start.isoformat() if cs_start else None,
                        "close": cs_end.isoformat() if cs_end else None},
      "session": {"name": sess, "open": s_start.isoformat() if s_start else None,
                  "close": s_end.isoformat() if s_end else None},
      "market": market, "kill_switch": ledger.get_state("kill_switch", False),
      "ais": ais, "combined": {k: round(v) for k, v in combined.items()},
      "orders_today": orders, "trades_today": trades, "events": events, "journal": journal[:40],
      "daily_history": daily_history[:120],
  }
