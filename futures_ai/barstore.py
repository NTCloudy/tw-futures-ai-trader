"""Persistent 1-minute bar history across sessions (for the swing AI's 60-minute bars).

Sources (deduplicated by bar end time): the Sinopac 1-year export (data/kbars_repo/TMF_1min.csv),
and live bars the engine appends after each poll (data/live_1m.csv). 60-minute bars are aligned to
each session's start (day 08:45, night 15:00), exactly like the swing simulator.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

from .calendar import TPE, session_of
from .config import ROOT
from .feed import Bar

HIST_FILES = [ROOT / "data" / "kbars_repo" / "TMF_1min.csv", ROOT / "data" / "kbars" / "TMF_1min.csv"]
LIVE_FILE = ROOT / "data" / "live_1m.csv"


class BarStore:

  def __init__(self, days: int = 45):
    self.bars: dict[datetime, Bar] = {}
    cutoff = datetime.now(TPE) - timedelta(days=days)
    for f in HIST_FILES + [LIVE_FILE]:
      if f.exists():
        self._load(f, cutoff, end_labeled=True)
    self._last_saved: datetime | None = max(self.bars) if self.bars else None

  def _load(self, path: Path, cutoff: datetime, end_labeled: bool) -> None:
    with path.open(encoding="utf-8") as fh:
      for r in csv.DictReader(fh):
        ts = datetime.strptime(r["ts_taipei"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TPE)
        if ts >= cutoff:
          self.bars[ts] = Bar(ts, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r["volume"]))

  def add(self, bars: list[Bar]) -> None:
    """Adds completed live 1-minute bars and appends new ones to data/live_1m.csv."""
    new = [b for b in bars if b.end not in self.bars]
    for b in bars:
      self.bars[b.end] = b
    new = [b for b in new if self._last_saved is None or b.end > self._last_saved]
    if not new:
      return
    LIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    first = not LIVE_FILE.exists()
    with LIVE_FILE.open("a", newline="", encoding="utf-8") as fh:
      w = csv.writer(fh)
      if first:
        w.writerow(["ts_taipei", "open", "high", "low", "close", "volume"])
      for b in sorted(new, key=lambda b: b.end):
        w.writerow([b.end.strftime("%Y-%m-%d %H:%M:%S"), b.o, b.h, b.l, b.c, b.v])
    self._last_saved = max(b.end for b in new)

  def sixty_min(self, now: datetime | None = None) -> list[Bar]:
    """Completed 60-minute bars aligned to session starts, across all stored sessions (chronological)."""
    out: list[Bar] = []
    cur_key = None
    agg = None
    for ts in sorted(self.bars):
      b = self.bars[ts]
      name, start, _ = session_of(ts - timedelta(seconds=1))
      if name is None:
        continue
      idx = int(((ts - start).total_seconds() - 1) // 3600)
      key = (start, idx)
      if key != cur_key:
        if agg is not None:
          out.append(agg)
        agg, cur_key = Bar(ts, b.o, b.h, b.l, b.c, b.v), key
      else:
        agg = Bar(ts, agg.o, max(agg.h, b.h), min(agg.l, b.l), b.c, agg.v + b.v)
    if agg is not None:
      start, idx = cur_key
      _, _, s_end = session_of(start + timedelta(minutes=1))
      bucket_end = min(start + timedelta(hours=idx + 1), s_end)
      if now is None or agg.end >= bucket_end or now >= s_end:
        out.append(agg)  # Keep the last bar only once its hour (or its session) is complete.
    return out
