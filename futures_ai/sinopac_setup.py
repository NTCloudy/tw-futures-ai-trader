"""永豐 Shioaji simulation test required before production API access is granted:
login + place a futures order in simulation mode (weekdays, per Sinopac's published schedule)."""

from __future__ import annotations

import os
import time

from .calendar import now_tpe, rollover_info


def sinopac_test(cfg: dict) -> bool:
  import shioaji as sj

  s = cfg["sinopac"]
  api = sj.Shioaji(simulation=True)
  accounts = api.login(api_key=os.environ[s["api_key_env"]], secret_key=os.environ[s["secret_key_env"]],
                       fetch_contract=True)
  print("登入成功，帳號：", accounts)
  code = rollover_info(now_tpe(), cfg["product"], cfg["roll_days_before"])["contract"]
  contract = [c for c in getattr(api.Contracts.Futures, cfg["product"]) if c.code == code][0]
  ref = float(contract.reference)
  c = sj.constant
  order = api.Order(action=c.Action.Buy, price=round(ref * 0.97), quantity=1,
                    price_type=c.FuturesPriceType.LMT, order_type=c.OrderType.ROD,
                    octype=c.FuturesOCType.Auto, account=api.futopt_account)
  trade = api.place_order(contract, order)
  time.sleep(2)
  api.update_status(api.futopt_account)
  print("模擬下單結果：", trade.status)
  try:
    api.cancel_order(trade)
  except Exception as exc:
    print("刪單：", exc)
  api.logout()
  print("完成。永豐會依這次模擬登入與下單紀錄開通正式環境（請以永豐通知為準）。")
  return True
