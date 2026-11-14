"""Engine watchdog alerts (cron, every 5 minutes; runs as its own process, never touches the engine).

scripts/start.sh (cron) already restarts the engine when the process is gone. This watchdog adds the missing
alerts so an outage is noticed during a session:
- engine process missing during a session (start.sh restarts it; the alert says so),
- engine process alive but no equity heartbeat for HEARTBEAT_MIN minutes during a session (hung / DB write
  errors such as "database or disk is full"); no automatic kill, a human decides,
- free disk space below DISK_MIN_GB (a full disk stopped the engine's DB writes before),
- recovery message once the problem is gone (disk: only after free space is back above DISK_MIN_GB + 1 GB).
Alerts go out through the existing LINE push (notify.py; the free plan has a small monthly quota, so the disk
alert repeats at most every 6 hours). The DB is opened read-only. State (de-duplication) lives in
data/watchdog_state.json.
Known limit: the engine also writes equity while quotes are stale, so "feed down but process alive" is not
caught here; the engine's own stale-feed messages cover that case.

Run: python3 -m futures_ai watchdog
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta

from .calendar import TPE, session_of
from .config import ROOT

STATE = ROOT / "data" / "watchdog_state.json"
HEARTBEAT_MIN = 10
GRACE_MIN = 10  # No alerts in the first minutes of a session (engine warm-up).
DISK_MIN_GB = 3.0
REALERT_MIN = {"engine": 30, "heartbeat": 30, "disk": 360}
DISK_HYST_GB = 1.0
ENGINE_PATTERN = r"^python3 -m futures_ai( .*)? run(\s|$)"


def engine_running() -> bool:
  return subprocess.run(["pgrep", "-f", ENGINE_PATTERN], capture_output=True).returncode == 0


def last_heartbeat(db_path: str) -> datetime | None:
  try:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
      row = conn.execute("SELECT MAX(ts) FROM equity").fetchone()
    finally:
      conn.close()
  except sqlite3.Error:
    return None
  if not row or not row[0]:
    return None
  dt = datetime.fromisoformat(row[0])
  return dt if dt.tzinfo else dt.replace(tzinfo=TPE)


def problems(now: datetime, running: bool, heartbeat: datetime | None, free_gb: float) -> dict[str, str]:
  """Pure check: {problem_key: message}."""
  out = {}
  if free_gb < DISK_MIN_GB:
    out["disk"] = f"⚠️ 磁碟剩 {free_gb:.1f} GB（低於 {DISK_MIN_GB:g} GB），交易紀錄可能寫不進去，請清理空間"
  name, start, _end = session_of(now)
  if name is None or now < start + timedelta(minutes=GRACE_MIN):
    return out
  label = "日盤" if name == "day" else "夜盤"
  if not running:
    out["engine"] = f"🚨 {label}進行中但交易引擎沒有在跑（排程 start.sh 每 5 分鐘會自動重啟），請留意部位"
  elif heartbeat is None or now - heartbeat > timedelta(minutes=HEARTBEAT_MIN):
    age = "從未" if heartbeat is None else f"{int((now - heartbeat).total_seconds() // 60)} 分鐘"
    out["heartbeat"] = (f"🚨 {label}進行中，交易引擎還在但已 {age}沒有更新帳面（可能卡住或資料庫寫不進去），"
                        "請檢查 logs/engine.log，必要時手動重啟")
  return out


def run(cfg: dict, now: datetime | None = None, send=None) -> list[str]:
  """One watchdog pass. Returns the messages sent."""
  now = now or datetime.now(TPE)
  try:
    st = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
  except (OSError, ValueError):
    st = {}
  free_gb = shutil.disk_usage(ROOT).free / 1e9
  found = problems(now, engine_running(), last_heartbeat(cfg["db_path"]), free_gb)
  msgs = []
  for key, msg in found.items():
    last = st.get(key)
    if last is None or now - datetime.fromisoformat(last) >= timedelta(minutes=REALERT_MIN.get(key, 30)):
      msgs.append(msg)
      st[key] = now.isoformat(timespec="seconds")
  hold = {"disk"} if free_gb < DISK_MIN_GB + DISK_HYST_GB else set()  # Hysteresis: no flapping recovery.
  name, start, _end = session_of(now)
  if name is None or now < start + timedelta(minutes=GRACE_MIN):
    hold |= {"engine", "heartbeat"}  # Not checked outside a session: decide recovery in the next session.
  for key in [k for k in list(st) if k not in found and k not in hold]:
    msgs.append({"engine": "✅ 交易引擎已恢復運作", "heartbeat": "✅ 交易引擎帳面已恢復更新",
                 "disk": "✅ 磁碟空間已恢復"}.get(key, f"✅ {key} 已恢復"))
    del st[key]
  if msgs:
    if send is None:
      from .notify import load_cred, push
      cred = load_cred()
      send = (lambda text: push(cred, text)) if cred and cfg.get("notify", {}).get("line_enabled") else print
    for m in msgs:
      try:
        send("【微台 AI 監控】" + m)
      except Exception as exc:  # Alerting must never crash the cron job.
        print(f"watchdog send failed: {exc}")
  try:
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
  except OSError as exc:  # Disk full: keep the cron job alive (alerts may repeat until space is freed).
    print(f"watchdog state write failed: {exc}")
  return msgs
