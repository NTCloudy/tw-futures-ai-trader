"""永豐 Shioaji simulation test required before production API access is granted:
login + place a futures order in simulation mode (weekdays, per Sinopac's published schedule)."""

from __future__ import annotations

import os
import time

from .calendar import now_tpe, rollover_info


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
  print("\n=== 今日委託／成交 ===")
  try:
    api.update_status(acct)
    trades = api.list_trades()
    if not trades:
      print("（今日無委託）")
    for t in trades:
      o, st = t.order, t.status
      print(f"{getattr(st, 'order_datetime', '')} {getattr(t.contract, 'code', '')} {o.action} {o.quantity} 口 "
            f"{o.price_type} {o.price} → {st.status}（成交 {getattr(st, 'deal_quantity', 0)} 口）")
  except Exception as exc:
    print("查詢委託失敗：", exc)
  api.logout()
  return True
