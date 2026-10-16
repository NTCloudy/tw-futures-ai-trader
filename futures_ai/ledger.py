"""SQLite ledger for orders (including 觸價單 triggers), round-trip trades and equity points."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ai TEXT NOT NULL, ts TEXT NOT NULL, trading_day TEXT NOT NULL, contract TEXT NOT NULL,
  kind TEXT NOT NULL,          -- ENTRY / TP / SL / SESSION_END / RISK / KILL / ROLL
  order_type TEXT NOT NULL,    -- MKT / STOP(停損觸價) / TAKE(停利觸價)
  side TEXT NOT NULL, qty INTEGER NOT NULL,
  trigger_price REAL, fill_price REAL,
  status TEXT NOT NULL,        -- WORKING / SUBMITTED / FILLED / TRIGGERED / CANCELLED / REJECTED
  broker TEXT NOT NULL, broker_order_id TEXT, note TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ai TEXT NOT NULL, trading_day TEXT NOT NULL, contract TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL,
  entry_ts TEXT NOT NULL, entry_price REAL NOT NULL, exit_ts TEXT NOT NULL, exit_price REAL NOT NULL,
  points REAL NOT NULL, gross_twd REAL NOT NULL, cost_twd REAL NOT NULL, pnl_twd REAL NOT NULL,
  exit_kind TEXT NOT NULL, entry_reason TEXT
);
CREATE TABLE IF NOT EXISTS equity (
  ts TEXT NOT NULL, ai TEXT NOT NULL, realized_total REAL NOT NULL, unrealized REAL NOT NULL,
  PRIMARY KEY (ts, ai)
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, ai TEXT, level TEXT NOT NULL, message TEXT NOT NULL);
"""


class Ledger:

  def __init__(self, path: str):
    if path != ":memory:":
      Path(path).parent.mkdir(parents=True, exist_ok=True)
    self.conn = sqlite3.connect(path, check_same_thread=False)
    self.conn.row_factory = sqlite3.Row
    self.lock = threading.RLock()
    self.conn.executescript(SCHEMA)
    self.conn.commit()

  def _exec(self, sql: str, params=()):
    with self.lock:
      cur = self.conn.execute(sql, params)
      self.conn.commit()
      return cur

  def rows(self, sql: str, params=()) -> list[dict]:
    with self.lock:
      return [dict(r) for r in self.conn.execute(sql, params)]

  # state / events
  def get_state(self, key: str, default=None):
    r = self.rows("SELECT value FROM state WHERE key=?", (key,))
    return json.loads(r[0]["value"]) if r else default

  def set_state(self, key: str, value) -> None:
    self._exec("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value, ensure_ascii=False, default=str)))

  def log(self, message: str, ai: str | None = None, level: str = "INFO", ts: datetime | None = None) -> None:
    self._exec("INSERT INTO events(ts,ai,level,message) VALUES(?,?,?,?)",
               ((ts or datetime.now()).isoformat(timespec="seconds"), ai, level, message))

  # orders
  def add_order(self, **o) -> int:
    o = {"trigger_price": None, "fill_price": None, "broker_order_id": None, "note": None, **o}
    o["updated_at"] = o["ts"]
    cur = self._exec(
        """INSERT INTO orders(ai,ts,trading_day,contract,kind,order_type,side,qty,trigger_price,fill_price,status,broker,broker_order_id,note,updated_at)
           VALUES(:ai,:ts,:trading_day,:contract,:kind,:order_type,:side,:qty,:trigger_price,:fill_price,:status,:broker,:broker_order_id,:note,:updated_at)""", o)
    return cur.lastrowid

  def update_order(self, oid: int, ts: str, **fields) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    self._exec(f"UPDATE orders SET {sets}, updated_at=? WHERE id=?", (*fields.values(), ts, oid))

  def working_orders(self, ai: str) -> list[dict]:
    return self.rows("SELECT * FROM orders WHERE ai=? AND status IN ('WORKING','SUBMITTED') ORDER BY id", (ai,))

  def order(self, oid: int) -> dict | None:
    r = self.rows("SELECT * FROM orders WHERE id=?", (oid,))
    return r[0] if r else None

  # trades / equity
  def add_trade(self, **t) -> None:
    self._exec(
        """INSERT INTO trades(ai,trading_day,contract,side,qty,entry_ts,entry_price,exit_ts,exit_price,points,gross_twd,cost_twd,pnl_twd,exit_kind,entry_reason)
           VALUES(:ai,:trading_day,:contract,:side,:qty,:entry_ts,:entry_price,:exit_ts,:exit_price,:points,:gross_twd,:cost_twd,:pnl_twd,:exit_kind,:entry_reason)""", t)

  def realized(self, ai: str, trading_day: str | None = None) -> float:
    if trading_day:
      r = self.rows("SELECT COALESCE(SUM(pnl_twd),0) s FROM trades WHERE ai=? AND trading_day=?", (ai, trading_day))
    else:
      r = self.rows("SELECT COALESCE(SUM(pnl_twd),0) s FROM trades WHERE ai=?", (ai,))
    return float(r[0]["s"])

  def add_equity(self, ts: str, ai: str, realized_total: float, unrealized: float) -> None:
    self._exec("INSERT OR REPLACE INTO equity(ts,ai,realized_total,unrealized) VALUES(?,?,?,?)",
               (ts, ai, realized_total, unrealized))
