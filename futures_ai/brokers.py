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
    self.tick = cfg["tick"]
    self._ids = itertools.count(1)
    self._fills: "queue.Queue[dict]" = queue.Queue()
    self._limits: dict[str, dict] = {}

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

  def limit(self, code: str, side: str, qty: int, price: float) -> str:
    """Resting limit order (paper): filled at the limit price once the market trades through it."""
    oid = f"P{next(self._ids)}"
    self._limits[oid] = {"side": side, "price": float(price), "qty": qty}
    return oid

  def on_quote(self, quote: dict) -> None:
    lo = min(quote["last"], quote.get("lo") or quote["last"])
    hi = max(quote["last"], quote.get("hi") or quote["last"])
    for oid, o in list(self._limits.items()):
      # Conservative (queue-position aware): merely touching the limit price does not count as a fill,
      # because real orders queue behind earlier ones at that price. The market must trade through it
      # by at least one tick.
      if (o["side"] == "B" and lo <= o["price"] - self.tick) or (o["side"] == "S" and hi >= o["price"] + self.tick):
        # A gap through the limit fills at the better market price, otherwise at the limit.
        px = min(o["price"], quote["last"]) if o["side"] == "B" else max(o["price"], quote["last"])
        self._fills.put({"broker_order_id": oid, "price": px, "qty": o["qty"], "ts": datetime.now()})
        del self._limits[oid]

  def cancel(self, order_id: str) -> None:
    self._limits.pop(order_id, None)

  def cancel_all(self, code: str) -> int:
    n = len(self._limits)
    self._limits.clear()
    return n

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
    from .export_history import _login
    self.api = _login(sj, simulation=self.simulation, key=_env(s["api_key_env"]), sec=_env(s["secret_key_env"]),
                      attempts=[{"fetch_contract": True}, {}])
    assert_futures_only(self.api, self.cfg, strict=not self.simulation)
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

  def limit(self, code: str, side: str, qty: int, price: float) -> str:
    c = self.sj.constant
    order = self.api.Order(action=c.Action.Buy if side == "B" else c.Action.Sell, price=price, quantity=qty,
                           price_type=c.FuturesPriceType.LMT, order_type=c.OrderType.ROD,
                           octype=c.FuturesOCType.Auto, account=self.account)
    trade = self.api.place_order(self.contract(code), order)
    self._trades = getattr(self, "_trades", {})
    self._trades[str(trade.order.id)] = trade
    return str(trade.order.id)

  def on_quote(self, quote: dict) -> None:
    pass  # Exchange-side matching; fills arrive via the order callback.

  def cancel(self, order_id: str) -> None:
    trade = getattr(self, "_trades", {}).pop(order_id, None)
    if trade is not None:
      self.api.update_status(self.account)
      self.api.cancel_order(trade)

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


def assert_futures_only(api, cfg: dict, strict: bool = True) -> None:
  """Safety guard: refuse to run if the API key can also see a securities (stock) account.
  The key must be created with ONLY the futures account bound, so it can never trade stocks.
  strict=False (simulation environment, virtual accounts, no real money): warn instead of stopping."""
  if not cfg.get("sinopac", {}).get("require_futures_only", True):
    return
  stock = None
  try:
    stock = api.stock_account
  except Exception:
    stock = None
  if stock is not None and not strict:
    print("⚠️ 模擬環境中看得到證券帳戶（可能是永豐的虛擬帳戶）。模擬不影響真錢；"
          "但上真錢前，請確認 API 金鑰只綁定期貨帳戶，否則系統會拒絕以真實帳戶啟動。")
  elif stock is not None:
    try:
      api.logout()
    except Exception:
      pass
    raise SystemExit("⛔ 這把永豐 API 金鑰看得到證券（現股）帳戶，系統拒絕啟動。\n"
                     "請到永豐 API 管理頁面重新申請一把「只綁定期貨帳戶」的金鑰，再更新環境變數。")
  if getattr(api, "futopt_account", None) is None:
    raise SystemExit("找不到期貨帳戶：請確認 API 金鑰有綁定期貨帳戶。")


def make_broker(cfg: dict, ai_id: str):
  mode = cfg["mode"]
  if mode in ("sinopac_sim", "sinopac_live") and ai_id == cfg["broker_ai"]:
    if mode == "sinopac_live" and not cfg.get("i_understand_real_money"):
      raise SystemExit("sinopac_live 需要在 config 設定 i_understand_real_money: true")
    if mode == "sinopac_live" and (cfg.get("learning_mode") or not cfg.get("enforce_daily_loss", True)):
      raise SystemExit("真實帳戶必須恢復所有風控：請設定 learning_mode: false、enforce_daily_loss: true")
    return ShioajiBroker(cfg, simulation=(mode == "sinopac_sim"))
  return LocalPaperBroker(cfg)
