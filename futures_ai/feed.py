"""Market data feeds.

TaifexFeed polls the public TAIFEX MIS endpoint used by https://mis.taifex.com.tw (real-time
quote + 1-minute bars of the current session). ShioajiFeed (sinopac modes) can replace it.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from .calendar import TPE

MIS_CHART_URL = "https://mis.taifex.com.tw/futures/api/getChartData1M"


@dataclass
class Bar:
  end: datetime  # bar covers (end - 1 minute, end]
  o: float
  h: float
  l: float
  c: float
  v: float


@dataclass
class Snapshot:
  code: str
  last: float
  bid: float | None
  ask: float | None
  bars: list[Bar] = field(default_factory=list)  # completed bars only
  quote_time: datetime | None = None  # exchange time of the latest quote
  live_bar: Bar | None = None  # in-progress bar (its high/low reveal prices touched between polls)
  cdate: str | None = None  # exchange date of the quote (YYYYMMDD); used for holiday detection


def _post(url: str, body: dict, timeout: int = 5) -> dict:
  req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
      "Content-Type": "application/json", "User-Agent": "Mozilla/5.0 tw-futures-ai-trader"})
  with urllib.request.urlopen(req, timeout=timeout) as resp:
    return json.load(resp)


def parse_chart(payload: dict, code: str, session_start: datetime, now: datetime) -> Snapshot:
  data = payload["RtData"]
  q = data.get("Quote") or {}
  fields = data.get("Field") or ["T", "O", "H", "L", "C", "V"]
  idx = {f: i for i, f in enumerate(fields)}
  bars: list[Bar] = []
  for row in data.get("Ticks") or []:
    try:  # Skip malformed rows instead of failing the whole snapshot.
      hhmmss = str(row[idx["T"]]).strip().zfill(6)[:6]
      t = datetime.combine(session_start.date(), time(int(hhmmss[:2]), int(hhmmss[2:4]), int(hhmmss[4:6])), TPE)
      float(row[idx["C"]])
    except (ValueError, IndexError, TypeError):
      continue
    if t < session_start:  # Night session crosses midnight.
      t += timedelta(days=1)
    bars.append(Bar(t, float(row[idx["O"]]), float(row[idx["H"]]), float(row[idx["L"]]),
                    float(row[idx["C"]]), float(row[idx["V"]])))
  newest = bars[-1].end if bars else None
  completed = [b for b in bars if b.end <= now]

  def num(key):
    v = q.get(key)
    return float(v) if v not in (None, "") else None

  last = num("CLastPrice") or (bars[-1].c if bars else 0.0)
  live = bars[-1] if bars and bars[-1].end > now else None
  qt = newest - timedelta(minutes=1) if newest else None  # start of the newest bar, conservative
  ctime = q.get("CTime")
  if ctime and len(ctime) >= 6 and ctime[:6].isdigit():
    t = datetime.combine(now.date(), time(int(ctime[:2]), int(ctime[2:4]), int(ctime[4:6])), TPE)
    qt = t - timedelta(days=1) if t > now + timedelta(minutes=5) else t
  return Snapshot(code, last, num("CBidPrice1"), num("CAskPrice1"), completed, qt, live, q.get("CDate") or None)


MIS_QUOTE_URL = "https://mis.taifex.com.tw/futures/api/getQuoteList"


class TaifexFeed:
  """Primary: 1-minute chart endpoint (bars + quote). Each poll retries once; if the chart endpoint
  still fails, the quote-list endpoint supplies a fresh last/bid/ask on top of the cached bars."""

  def __init__(self):
    self.cache: dict[str, Snapshot] = {}
    self.night_suffix: str | None = None

  def _chart(self, code: str, session: str, session_start: datetime, now: datetime) -> Snapshot:
    if session == "day":
      suffixes = ["-F"]
    else:  # The after-hours symbol suffix is detected once and remembered.
      suffixes = [self.night_suffix] if self.night_suffix else ["-M", "-F"]
    last_err = None
    for sfx in suffixes:
      for attempt in range(2):
        try:
          payload = _post(MIS_CHART_URL, {"SymbolID": f"{code}{sfx}"}, timeout=4)
          if payload.get("RtCode") == "0" and payload.get("RtData"):
            snap = parse_chart(payload, code, session_start, now)
            if snap.bars:
              if session == "night":
                self.night_suffix = sfx
              return snap
          break  # Valid response without bars: try the next suffix.
        except Exception as exc:
          last_err = exc
    raise RuntimeError(f"chart endpoint failed: {last_err}")

  def _quote_fallback(self, code: str, session: str, now: datetime) -> Snapshot:
    payload = _post(MIS_QUOTE_URL, {"MarketType": "0" if session == "day" else "1", "SymbolType": "F",
                                    "KindID": "1", "CID": code[:3], "ExpireMonth": "", "RowSize": "全部",
                                    "PageNo": "", "SortColumn": "", "AscDesc": "A"}, timeout=4)
    rows = [q for q in payload["RtData"]["QuoteList"] if q.get("SymbolID", "").startswith(code + "-")]
    if not rows or not rows[0].get("CLastPrice"):
      raise RuntimeError("quote fallback: symbol not found")
    q = rows[0]
    ct = q.get("CTime") or ""
    qt = datetime.combine(now.date(), time(int(ct[:2]), int(ct[2:4]), int(ct[4:6])), TPE) if len(ct) >= 6 else now
    if qt > now + timedelta(minutes=5):
      qt -= timedelta(days=1)
    cached = self.cache.get(code)
    num = lambda k: float(q[k]) if q.get(k) not in (None, "") else None
    return Snapshot(code, float(q["CLastPrice"]), num("CBidPrice1"), num("CAskPrice1"),
                    cached.bars if cached else [], qt, None, q.get("CDate") or None)

  def snapshot(self, code: str, session: str, session_start: datetime, now: datetime) -> Snapshot:
    try:
      snap = self._chart(code, session, session_start, now)
      self.cache[code] = snap
      return snap
    except Exception as chart_err:
      try:
        return self._quote_fallback(code, session, now)
      except Exception as exc:
        raise RuntimeError(f"TAIFEX 行情取得失敗：{chart_err}；備援也失敗：{exc}")
