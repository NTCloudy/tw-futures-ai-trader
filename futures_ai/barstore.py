"""Persistent 1-minute bar history across sessions (for the swing AI's 60-minute bars).

Sources (deduplicated by bar end time): the Sinopac 1-year export (data/kbars_repo/TMF_1min.csv),
and live bars the engine appends after each poll (data/live_1m.csv). 60-minute bars are aligned to
each session's start (day 08:45, night 15:00), exactly like the swing simulator.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path

from .calendar import TPE, session_of
from .config import ROOT
from .feed import Bar

HIST_FILES = [ROOT / "data" / "kbars_repo" / "TMF_1min.csv", ROOT / "data" / "kbars" / "TMF_1min.csv"]
LIVE_FILE = ROOT / "data" / "live_1m.csv"
DAILY_DIR = ROOT / "data" / "history"
DAILY_CACHE_FILE = DAILY_DIR / ".daily_tmf_1m.json"
_ZIP_CACHE: dict[str, list[tuple[str, str, list[Bar]]]] = {}
_DISK_LOADED = False


def sessions_from_zip_cached(path: Path, product: str = "TMF", roll_days: int = 1) -> list[tuple[str, str, list[Bar]]]:
  global _DISK_LOADED
  if not _DISK_LOADED and DAILY_CACHE_FILE.exists():
    _DISK_LOADED = True
    try:
      raw = json.loads(DAILY_CACHE_FILE.read_text(encoding="utf-8"))
      for k, sess_list in raw.items():
        _ZIP_CACHE[k] = [
            (nm, cd, [Bar(datetime.fromisoformat(b[0]), b[1], b[2], b[3], b[4], b[5]) for b in bs])
            for nm, cd, bs in sess_list
        ]
    except Exception:
      pass
  _DISK_LOADED = True
  try:
    mtime = round(path.stat().st_mtime, 2)
  except OSError:
    return []
  key = f"{path.name}:{mtime}:{product}:{roll_days}"
  if key not in _ZIP_CACHE:
    from .pretrain import sessions_from_file
    try:
      td = datetime.strptime(path.stem[6:], "%Y_%m_%d").date()
      _ZIP_CACHE[key] = list(sessions_from_file(path.read_bytes(), td, product, roll_days))
      try:
        serial = {
            k: [[nm, cd, [[b.end.isoformat(), b.o, b.h, b.l, b.c, b.v] for b in bs]] for nm, cd, bs in v]
            for k, v in _ZIP_CACHE.items()
        }
        DAILY_CACHE_FILE.write_text(json.dumps(serial), encoding="utf-8")
      except Exception:
        pass
    except Exception:
      _ZIP_CACHE[key] = []
  return _ZIP_CACHE[key]


def _bars_from_zip(path: Path, product: str = "TMF", roll_days: int = 1) -> list[Bar]:
  out: list[Bar] = []
  for _, _, bars in sessions_from_zip_cached(path, product, roll_days):
    out.extend(bars)
  return out


class BarStore:

  def __init__(self, days: int = 45):
    self.bars: dict[datetime, Bar] = {}
    raw_cutoff = datetime.now(TPE) - timedelta(days=days)
    _, s_start, _ = session_of(raw_cutoff)
    cutoff = s_start if s_start else raw_cutoff.replace(hour=0, minute=0, second=0, microsecond=0)
    # Priority at 1-minute bar.end level: Daily_*.zip (official) > HIST_FILES (kbars) > LIVE_FILE.
    if LIVE_FILE.exists():
      self._load(LIVE_FILE, cutoff, end_labeled=True)
    for f in HIST_FILES:
      if f.exists():
        self._load(f, cutoff, end_labeled=True)
    if DAILY_DIR.exists():
      min_stem = f"Daily_{(cutoff - timedelta(days=5)):%Y_%m_%d}"
      for zf in sorted(DAILY_DIR.glob("Daily_*.zip")):
        if zf.stem >= min_stem:
          for b in _bars_from_zip(zf):
            if b.end >= cutoff:
              self.bars[b.end] = b
    self._saved_live: dict[datetime, Bar] = {}
    if LIVE_FILE.exists():
      with LIVE_FILE.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
          ts = datetime.strptime(r["ts_taipei"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TPE)
          self._saved_live[ts] = Bar(ts, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r["volume"]))
    self._last_saved: datetime | None = max(self._saved_live) if self._saved_live else None

  def _load(self, path: Path, cutoff: datetime, end_labeled: bool) -> None:
    with path.open(encoding="utf-8") as fh:
      for r in csv.DictReader(fh):
        ts = datetime.strptime(r["ts_taipei"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TPE)
        if ts >= cutoff:
          self.bars[ts] = Bar(ts, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r["volume"]))

  def add(self, bars: list[Bar]) -> None:
    """Adds completed live 1-minute bars and appends/updates data/live_1m.csv without duplicate timestamps."""
    appended: list[Bar] = []
    updated = False
    for b in bars:
      self.bars[b.end] = b
      prev = self._saved_live.get(b.end)
      if prev is None:
        if self._last_saved is None or b.end > self._last_saved:
          appended.append(b)
          self._saved_live[b.end] = b
        else:
          self._saved_live[b.end] = b
          updated = True
      elif (prev.o, prev.h, prev.l, prev.c, prev.v) != (b.o, b.h, b.l, b.c, b.v):
        self._saved_live[b.end] = b
        updated = True
    if not appended and not updated:
      return
    LIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if updated:
      tmp = LIVE_FILE.with_suffix(".csv.tmp")
      with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts_taipei", "open", "high", "low", "close", "volume"])
        for ts in sorted(self._saved_live):
          b = self._saved_live[ts]
          w.writerow([b.end.strftime("%Y-%m-%d %H:%M:%S"), b.o, b.h, b.l, b.c, b.v])
      tmp.replace(LIVE_FILE)
    elif appended:
      first = not LIVE_FILE.exists()
      with LIVE_FILE.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if first:
          w.writerow(["ts_taipei", "open", "high", "low", "close", "volume"])
        for b in sorted(appended, key=lambda b: b.end):
          w.writerow([b.end.strftime("%Y-%m-%d %H:%M:%S"), b.o, b.h, b.l, b.c, b.v])
    if self._saved_live:
      self._last_saved = max(self._saved_live)

  def sixty_min(self, now: datetime | None = None) -> list[Bar]:
    """Completed 60-minute bars aligned to session starts, across all stored sessions (chronological)."""
    out: list[Bar] = []
    cur_key = None
    agg = None
    for ts in sorted(self.bars):
      b = self.bars[ts]
      name, start, s_end = session_of(ts - timedelta(seconds=1))
      if name is None:
        continue
      idx = int(((ts - start).total_seconds() - 1) // 3600)
      bucket_end = min(start + timedelta(hours=idx + 1), s_end)
      key = (start, idx, bucket_end)
      if key != cur_key:
        if agg is not None:
          out.append(agg)
        agg, cur_key = Bar(bucket_end, b.o, b.h, b.l, b.c, b.v), key
      else:
        agg = Bar(bucket_end, agg.o, max(agg.h, b.h), min(agg.l, b.l), b.c, agg.v + b.v)
    if agg is not None:
      _, _, bucket_end = cur_key
      if now is None or now >= bucket_end:
        out.append(agg)  # Keep the last bar only once its hour bucket has actually ended.
    return out
