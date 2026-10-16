"""Background pre-training on real historical TAIFEX data (not shown on the dashboard).

Downloads TAIFEX's official daily tick files (期交所「前 30 個交易日期貨每筆成交資料」), rebuilds
1-minute bars for the near-month TMF contract (day + night sessions), and replays them through
the same three AI traders, filters, bracket orders and post-trade review — but into a throw-away
in-memory ledger. Only the resulting experience (regime x strategy statistics and stop /
take-profit multipliers) is merged into the live AIs, with less weight than live experience.
No historical trades, orders or journal entries ever reach the live ledger or the dashboard.
"""

from __future__ import annotations

import copy
import io
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from .brokers import LocalPaperBroker
from .calendar import TPE, active_month, contract_code
from .config import ROOT
from .feed import Bar
from .learning import EMPTY, clamp, SL_RANGE, TP_RANGE
from .ledger import Ledger
from .strategies import build_strategies
from .trader import AiTrader

URL = "https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV/Daily_{d}.zip"
CACHE = ROOT / "data" / "history"


def download(d: date) -> bytes | None:
  CACHE.mkdir(parents=True, exist_ok=True)
  f = CACHE / f"Daily_{d:%Y_%m_%d}.zip"
  if f.exists():
    return f.read_bytes()
  req = urllib.request.Request(URL.format(d=d.strftime("%Y_%m_%d")), headers={"User-Agent": "Mozilla/5.0"})
  try:
    with urllib.request.urlopen(req, timeout=60) as r:
      data = r.read()
  except Exception:
    return None
  if not data.startswith(b"PK"):
    return None
  f.write_bytes(data)
  return data


def sessions_from_file(data: bytes, td: date, product: str, roll_days: int) -> list[tuple[str, str, list[Bar]]]:
  """Returns [(session_name, contract_code, bars)] for the night session belonging to `td` and the day session."""
  y, m = active_month(td, roll_days)
  expiry = f"{y}{m:02d}"
  code = contract_code(product, y, m)
  z = zipfile.ZipFile(io.BytesIO(data))
  text = z.read(z.namelist()[0]).decode("big5", errors="ignore")
  buckets: dict[str, dict[datetime, list]] = {"night": defaultdict(list), "day": defaultdict(list)}
  for line in text.splitlines()[1:]:
    p = [x.strip() for x in line.split(",")]
    if len(p) < 6 or p[1] != product or p[2] != expiry:
      continue
    try:
      d = datetime.strptime(p[0], "%Y%m%d").date()
      hh, mm, ss = int(p[3][:2]), int(p[3][2:4]), int(p[3][4:6])
      price, qty = float(p[4]), float(p[5])
    except ValueError:
      continue
    t = time(hh, mm, ss)
    if time(8, 45) <= t <= time(13, 45):
      name = "day"
    elif t >= time(15, 0) or t < time(5, 0):
      name = "night"
    else:
      continue
    end = datetime.combine(d, time(hh, mm), TPE) + timedelta(minutes=1)
    buckets[name][end].append((price, qty))
  out = []
  for name in ("night", "day"):
    bars = []
    for end in sorted(buckets[name]):
      ticks = buckets[name][end]
      prices = [p for p, _ in ticks]
      bars.append(Bar(end, prices[0], max(prices), min(prices), prices[-1], sum(q for _, q in ticks)))
    if len(bars) > 30:
      out.append((name, code, bars))
  return out


def replay(cfg: dict, days: int = 20, log=print) -> dict:
  """Replays the most recent `days` trading days. Returns {ai_id: brain_state} + stats."""
  ledger = Ledger(":memory:")
  traders = [AiTrader(cfg, s, LocalPaperBroker(cfg), ledger) for s in build_strategies()]
  files: list[tuple[date, bytes]] = []
  d = date.today() - timedelta(days=1)
  tries = 0
  while len(files) < days and tries < 60:
    tries += 1
    if d.weekday() < 5:
      data = download(d)
      if data:
        files.append((d, data))
    d -= timedelta(days=1)
  files.sort()
  log(f"取得 {len(files)} 個交易日的期交所逐筆成交資料（{files[0][0]} ～ {files[-1][0]}）" if files else "沒有可用的歷史資料")
  n_sessions = 0
  for td, data in files:
    for name, code, bars in sessions_from_file(data, td, cfg["product"], cfg["roll_days_before"]):
      n_sessions += 1
      session_key = f"{td}-{name}-hist"
      close = bars[-1].end
      for i, bar in enumerate(bars):
        ts = bar.end
        mtc = (close - ts).total_seconds() / 60
        q = {"last": bar.c, "bid": bar.c - 1, "ask": bar.c + 1, "hi": bar.h, "lo": bar.l}
        for tr in traders:
          tr.on_price(ts, td.isoformat(), code, q, mtc, False)
        qc = {"last": bar.c, "bid": bar.c - 1, "ask": bar.c + 1}
        for tr in traders:
          tr.on_bar(ts, td.isoformat(), code, session_key, bars[: i + 1], qc, mtc, True, False)
      for tr in traders:  # Flatten anything left at the session end.
        if tr.pos:
          tr.flatten(close, code, {"last": bars[-1].c, "bid": bars[-1].c - 1, "ask": bars[-1].c + 1}, "SESSION_END")
          tr.process_fills(close)
  summary = {}
  for tr in traders:
    rows = ledger.rows("SELECT COUNT(*) n, COALESCE(SUM(pnl_twd),0) p, COALESCE(SUM(pnl_twd>0),0) w FROM trades WHERE ai=?", (tr.ai,))[0]
    summary[tr.ai] = {"brain": tr.brain.state, "trades": rows["n"], "pnl": rows["p"], "wins": rows["w"]}
  return {"days": len(files), "sessions": n_sessions, "range": [str(files[0][0]), str(files[-1][0])] if files else None,
          "ais": summary}


def merge_into_live(live_ledger, result: dict) -> None:
  """Merges historical experience into the live brains. Live experience keeps priority."""
  now = datetime.now(TPE).isoformat(timespec="seconds")
  for ai, info in result["ais"].items():
    key = f"brain:{ai}"
    live = live_ledger.get_state(key) or copy.deepcopy(EMPTY)
    pre = info["brain"]
    old = live.get("pretrained", {}).get("stats")
    for r, modes in live["stats"].items():
      for mname, st in modes.items():
        if old:  # Re-running pre-training replaces the previous historical contribution.
          o = old[r][mname]
          st["n"] -= o["n"]
          st["wins"] -= o["wins"]
          st["pnl"] -= o["pnl"]
        p = pre["stats"][r][mname]
        live_n = st["n"]
        st["n"] += p["n"]
        st["wins"] += p["wins"]
        st["pnl"] += p["pnl"]
        if p["n"]:
          w_live = min(live_n, 5)
          st["ew"] = (st["ew"] * w_live + p["ew"] * 0.7 * 3) / (w_live + 3)
    live_total = sum(s["n"] for m in live["stats"].values() for s in m.values()) - sum(
        s["n"] for m in pre["stats"].values() for s in m.values())
    pre_total = max(1, info["trades"])
    w_pre = 0.5 * pre_total / (0.5 * pre_total + max(live_total, 0) + 1)
    live["sl_mult"] = clamp(live["sl_mult"] * (1 - w_pre) + pre["sl_mult"] * w_pre, *SL_RANGE)
    live["tp_mult"] = clamp(live["tp_mult"] * (1 - w_pre) + pre["tp_mult"] * w_pre, *TP_RANGE)
    live["pretrained"] = {"at": now, "days": result["days"], "sessions": result["sessions"], "range": result["range"],
                          "trades": info["trades"], "wins": info["wins"], "pnl": round(info["pnl"]),
                          "stats": pre["stats"]}
    live_ledger.set_state(key, live)
