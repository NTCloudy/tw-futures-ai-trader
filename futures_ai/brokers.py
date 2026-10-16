"""Brokers: LocalPaperBroker (simulated fills) and ShioajiBroker (永豐 simulation or real account).

Stop-loss / take-profit are 觸價單 managed by the trader on every price update, because the
Shioaji API has no exchange-side stop order for futures. When a trigger fires, the trader cancels
every working order at the broker first, then sends a market (IOC) order to close.
"""

from __future__ import annotations

import itertools
import os
import queue
from datetime import datetime
from pathlib import Path

from .config import ROOT


class LocalPaperBroker:
  name = "local_paper"

  def __init__(self, cfg: dict):
    self.slip = cfg["paper_slippage_ticks"] * cfg["tick"]
    self._ids = itertools.count(1)
    self._fills: "queue.Queue[dict]" = queue.Queue()

  def connect(self) -> None:
    pass

  def market(self, code: str, side: str, qty: int, quote: dict) -> str:
    oid = f"P{next(self._ids)}"
    if side == "B":
      price = (quote.get("ask") or quote["last"]) + self.slip
    else:
      price = (quote.get("bid") or quote["last"]) - self.slip
    self._fills.put({"broker_order_id": oid, "price": float(price), "qty": qty, "ts": datetime.now()})
    return oid

  def cancel_all(self, code: str) -> int:
    return 0  # No resting orders at a paper broker; triggers live in the ledger.

  def poll_fills(self) -> list[dict]:
    out = []
    while not self._fills.empty():
      out.append(self._fills.get())
    return out

  def close(self) -> None:
    pass


class ShioajiBroker:
  """永豐金 Shioaji futures broker. simulation=True uses the Sinopac simulation account."""

  def __init__(self, cfg: dict, simulation: bool):
    self.cfg = cfg
    self.simulation = simulation
    self.name = "sinopac_sim" if simulation else "sinopac_live"
    self._fills: "queue.Queue[dict]" = queue.Queue()
    self._contracts: dict = {}
    self.api = None
    self.sj = None

  def connect(self) -> None:
    import shioaji as sj  # Optional dependency: pip install shioaji

    s = self.cfg["sinopac"]
    self.sj = sj
    self.api = sj.Shioaji(simulation=self.simulation)
    self.api.login(api_key=_env(s["api_key_env"]), secret_key=_env(s["secret_key_env"]), fetch_contract=True)
    if not self.simulation:
      ca = Path(s["ca_path"])
      ca = ca if ca.is_absolute() else ROOT / ca
      kwargs = {"ca_path": str(ca), "ca_passwd": _env(s["ca_password_env"])}
      if os.environ.get(s["person_id_env"]):
        kwargs["person_id"] = os.environ[s["person_id_env"]]
      self.api.activate_ca(**kwargs)
    self.account = self.api.futopt_account
    self.api.set_order_callback(self._on_order)

  def contract(self, code: str):
    if code not in self._contracts:
      group = getattr(self.api.Contracts.Futures, self.cfg["product"])
      match = [c for c in group if getattr(c, "code", "") == code]
      if not match:
        raise RuntimeError(f"Shioaji 找不到合約 {code}")
      self._contracts[code] = match[0]
    return self._contracts[code]

  def _on_order(self, stat, msg) -> None:
    if stat == self.sj.constant.OrderState.FuturesDeal:
      self._fills.put({"broker_order_id": str(msg.get("trade_id")), "price": float(msg["price"]),
                       "qty": int(msg["quantity"]), "ts": datetime.now()})

  def market(self, code: str, side: str, qty: int, quote: dict) -> str:
    c = self.sj.constant
    order = self.api.Order(
        action=c.Action.Buy if side == "B" else c.Action.Sell,
        price=0, quantity=qty,
        price_type=c.FuturesPriceType.MKT,
        order_type=c.OrderType.IOC,
        octype=c.FuturesOCType.Auto,
        account=self.account)
    trade = self.api.place_order(self.contract(code), order)
    return str(trade.order.id)

  def cancel_all(self, code: str) -> int:
    self.api.update_status(self.account)
    live = {"PendingSubmit", "PreSubmitted", "Submitted", "PartFilled"}
    n = 0
    for t in self.api.list_trades():
      if getattr(t.contract, "code", None) == code and str(t.status.status).split(".")[-1] in live:
        self.api.cancel_order(t)
        n += 1
    return n

  def poll_fills(self) -> list[dict]:
    out = []
    while not self._fills.empty():
      out.append(self._fills.get())
    return out

  def close(self) -> None:
    if self.api is not None:
      self.api.logout()


def _env(name: str) -> str:
  v = os.environ.get(name, "")
  if not v:
    raise RuntimeError(f"請設定環境變數 {name}")
  return v


def make_broker(cfg: dict, ai_id: str):
  mode = cfg["mode"]
  if mode in ("sinopac_sim", "sinopac_live") and ai_id == cfg["broker_ai"]:
    if mode == "sinopac_live" and not cfg.get("i_understand_real_money"):
      raise SystemExit("sinopac_live 需要在 config 設定 i_understand_real_money: true")
    return ShioajiBroker(cfg, simulation=(mode == "sinopac_sim"))
  return LocalPaperBroker(cfg)
