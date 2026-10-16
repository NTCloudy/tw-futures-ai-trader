"""Market data feeds.

TaifexFeed polls the public TAIFEX MIS endpoint used by https://mis.taifex.com.tw (real-time
quote + 1-minute bars of the current session). ShioajiFeed (sinopac modes) can replace it.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta

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
  quote_time: datetime | None = None  # end time of the newest (possibly in-progress) bar


def _post(url: str, body: dict, timeout: int = 10) -> dict:
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
    hhmmss = row[idx["T"]]
    t = datetime.combine(session_start.date(), datetime.strptime(hhmmss, "%H%M%S").time(), TPE)
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
  return Snapshot(code, last, num("CBidPrice1"), num("CAskPrice1"), completed, newest)


class TaifexFeed:

  def snapshot(self, code: str, session: str, session_start: datetime, now: datetime) -> Snapshot:
    # Day session uses "<code>-F"; the after-hours session uses "<code>-M".
    suffixes = ["-F"] if session == "day" else ["-M", "-F"]
    last_err = None
    for sfx in suffixes:
      try:
        payload = _post(MIS_CHART_URL, {"SymbolID": f"{code}{sfx}"})
        if payload.get("RtCode") == "0" and payload.get("RtData"):
          snap = parse_chart(payload, code, session_start, now)
          if snap.bars or sfx == suffixes[-1]:
            return snap
      except Exception as exc:  # Try the next suffix, then surface the error.
        last_err = exc
    raise RuntimeError(f"TAIFEX 行情取得失敗：{last_err}")
