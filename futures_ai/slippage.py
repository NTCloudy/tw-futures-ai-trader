"""Live stop-fill slippage statistics (for the monthly report).

Slippage of one stop = points between the stop trigger price and the market fill, in the adverse direction
(orders table: SL STOP 'TRIGGERED' row followed by the SL MKT fill of the same AI). The backtest setting
(config sim_sl_slip_pts, default 1) is flagged "建議調整" only when n >= MIN_N and |mean - setting| >= 1;
changes are approved by the user and take effect before a Saturday weekly run (never automatically).
"""

from __future__ import annotations

import math

SIM_ID = {"alpha-orb": "alpha-orb", "sigma-vwap": "sigma-pullback", "nova-nightorb": "anchor-orb",
          "omega-swing": "omega-swing"}
NAMES = {"alpha-orb": "Alpha", "sigma-vwap": "Sigma", "nova-nightorb": "Nova", "omega-swing": "Omega"}
MIN_N = 30
SINCE = "2026-10-19"


def stop_slippage(ledger, since: str = SINCE) -> dict[str, list[float]]:
  rows = ledger.rows("SELECT ai, kind, order_type, side, trigger_price, fill_price, status FROM orders "
                     "WHERE ts>=? AND kind='SL' ORDER BY id", (since,))
  pend, out = {}, {}
  for r in rows:
    if r["order_type"] == "STOP" and r["status"] == "TRIGGERED" and r["trigger_price"] is not None:
      pend[r["ai"]] = r
    elif r["order_type"] == "MKT" and r["fill_price"] is not None and r["ai"] in pend:
      s = pend.pop(r["ai"])
      slip = (r["fill_price"] - s["trigger_price"]) if r["side"] == "B" else (s["trigger_price"] - r["fill_price"])
      out.setdefault(r["ai"], []).append(float(slip))
  return out


def stats(v: list[float]) -> tuple[int, float, float, float]:
  """(n, mean, ci95_low, ci95_high)."""
  n = len(v)
  if n == 0:
    return 0, 0.0, 0.0, 0.0
  m = sum(v) / n
  sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1)) if n > 1 else 0.0
  h = 1.96 * sd / math.sqrt(n) if n > 1 else 0.0
  return n, m, m - h, m + h


def slippage_lines(ledger, cfg: dict) -> list[str]:
  data = stop_slippage(ledger)
  setting = cfg.get("sim_sl_slip_pts") or {}
  out = ["", f"🎯 停損滑價實測（{SINCE[5:].replace('-', '/')} 起，觸發價→成交價，點）"]
  for ai, name in NAMES.items():
    n, m, lo, hi = stats(data.get(ai, []))
    cur = setting.get(SIM_ID[ai], 1)
    if n == 0:
      out.append(f"{name}：尚無停損｜回測設定 {cur:g}")
      continue
    flag = ""
    if n >= MIN_N and abs(m - cur) >= 1:
      flag = f" → ⚠️ 建議調整為 {round(m)}（需你批准，週六訓練前生效）"
    elif n < MIN_N:
      flag = f"（樣本不足 {MIN_N} 筆，不調整）"
    out.append(f"{name}：{n} 筆｜平均 {m:.1f}（95% 區間 {lo:.1f}～{hi:.1f}）｜回測設定 {cur:g}{flag}")
  return out
