"""永豐 Shioaji simulation test required before production API access is granted:
login + place a futures order in simulation mode (weekdays, per Sinopac's published schedule)."""

from __future__ import annotations

import os
import time
from datetime import datetime

from .calendar import TPE, now_tpe, rollover_info


def sinopac_test(cfg: dict) -> bool:
  import shioaji as sj

  s = cfg["sinopac"]
  from .export_history import _login
  # Version-adaptive login (Shioaji 1.7+ no longer accepts fetch_contract); keep trade reports on.
  api = _login(sj, simulation=True, key=os.environ[s["api_key_env"]], sec=os.environ[s["secret_key_env"]],
               attempts=[{"fetch_contract": True}, {}])
  from .brokers import assert_futures_only
  assert_futures_only(api, cfg, strict=False)  # Simulation: warn only (virtual accounts, no real money).
  print("登入成功，期貨帳號：", api.futopt_account)
  code = rollover_info(now_tpe(), cfg["product"], cfg["roll_days_before"])["contract"]
  contract = [c for c in getattr(api.Contracts.Futures, cfg["product"]) if c.code == code][0]
  ref = float(getattr(contract, "reference", 0) or getattr(contract, "limit_down", 0) or 0)
  if not ref:
    raise SystemExit("取不到合約參考價，請稍後再試")
  print(f"測試合約 {code}，參考價 {ref:.0f}；下一張遠低於市價的限價單（不會成交），再刪單")
  c = sj.constant
  order = api.Order(action=c.Action.Buy, price=round(ref * 0.97), quantity=1,
                    price_type=c.FuturesPriceType.LMT, order_type=c.OrderType.ROD,
                    octype=c.FuturesOCType.Auto, account=api.futopt_account)
  trade = api.place_order(contract, order)
  time.sleep(2)
  api.update_status(api.futopt_account)
  print("模擬下單結果：", getattr(trade, "status", trade))
  try:
    api.cancel_order(trade)
  except Exception as exc:
    print("刪單：", exc)
  api.logout()
  print("完成。永豐會依這次模擬登入與下單紀錄開通正式環境（請以永豐通知為準）。")
  return True


def sinopac_status(cfg: dict) -> bool:
  """Read-only view of the Sinopac SIMULATION futures account: open positions and today's orders.
  (Simulation orders do not appear in the Sinopac mobile app; this is where to see them.)"""
  import shioaji as sj
  from .brokers import assert_futures_only
  from .export_history import _login

  s = cfg["sinopac"]
  api = _login(sj, simulation=True, key=os.environ[s["api_key_env"]], sec=os.environ[s["secret_key_env"]],
               attempts=[{"fetch_contract": True}, {}])
  assert_futures_only(api, cfg, strict=False)
  acct = api.futopt_account
  print("\n=== 永豐模擬帳戶：期貨持倉 ===")
  try:
    pos = api.list_positions(acct)
    if not pos:
      print("（無持倉）")
    for p in pos:
      print(f"{getattr(p, 'code', '?')} {getattr(p, 'direction', '?')} {getattr(p, 'quantity', '?')} 口 "
            f"均價 {getattr(p, 'price', '?')} 現價 {getattr(p, 'last_price', '?')} 損益 {getattr(p, 'pnl', '?')}")
  except Exception as exc:
    print("查詢持倉失敗：", exc)
  print("\n=== 今日每筆交易（依永豐成交回報配對進出場）===")
  try:
    api.update_status(acct)
    fills = []
    for t in api.list_trades():
      for d in (getattr(t.status, "deals", None) or []):
        side = 1 if "Buy" in str(t.order.action) else -1
        ts = getattr(d, "ts", 0)
        when = datetime.fromtimestamp(ts, TPE) if isinstance(ts, (int, float)) and ts else getattr(t.status, "order_datetime", None)
        fills.append((when, side, float(d.price), int(d.quantity)))
    fills.sort(key=lambda f: f[0])
    pos, entry, entry_t, total, n, wins = 0, 0.0, None, 0.0, 0, 0
    pv, fee, tax = cfg["point_value"], cfg["commission_per_side_twd"], cfg["tax_rate"]
    for when, side, price, qty in fills:
      cost = fee * qty + round(price * pv * qty * tax)
      if pos == 0:
        pos, entry, entry_t, entry_cost = side * qty, price, when, cost
        continue
      if side == -1 * (1 if pos > 0 else -1):  # Closing fill.
        pts = (price - entry) * (1 if pos > 0 else -1)
        pnl = pts * pv * abs(pos) - entry_cost - cost
        total += pnl
        n += 1
        wins += pnl > 0
        print(f"{entry_t:%m/%d %H:%M} {'做多' if pos > 0 else '放空'} {entry:.0f} → {when:%H:%M} 出場 {price:.0f}"
              f"｜{pts:+.0f} 點｜{pnl:+,.0f} 元")
        pos = 0
    if pos:
      print(f"{entry_t:%m/%d %H:%M} {'多' if pos > 0 else '空'}單 {entry:.0f} 尚未平倉")
    if n:
      print(f"合計 {n} 筆，勝 {wins} 筆，損益 {total:+,.0f} 元（已扣手續費 {fee} 元/邊與期交稅估計）")
    elif not fills:
      print("（今日沒有成交）")
  except Exception as exc:
    print("查詢成交失敗：", exc)
  api.logout()
  return True
