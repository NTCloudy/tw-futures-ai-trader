"""Export ~1 year of Micro TAIEX futures (TMF) 1-minute K bars from Sinopac Shioaji — READ-ONLY.

Safety by design:
- Uses only the market-data API (api.kbars). Never calls activate_ca, place_order or any account /
  stock API. Recommended API key permissions: 行情/資料 only (no 交易, no 帳務), futures account only.
- Futures only: the contract is resolved from api.Contracts.Futures.TMF (continuous near month TMFR1).
- Credentials are typed at the prompt (hidden) or read from env vars; never written to disk.

Output: data/kbars/TMF_1min.csv (ts_taipei, open, high, low, close, volume), then (optionally) pushed
to the PRIVATE GitHub repo NTCloudy/tw-futures-data so the training machine can use it.
"""

from __future__ import annotations

import csv
import getpass
import os
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import ROOT

OUT_DIR = ROOT / "data" / "kbars"
DATA_REPO = "NTCloudy/tw-futures-data"


def _creds() -> tuple[str, str]:
  key = os.environ.get("SINOPAC_API_KEY") or getpass.getpass("永豐 API Key（輸入時不顯示）：").strip()
  sec = os.environ.get("SINOPAC_SECRET_KEY") or getpass.getpass("永豐 Secret Key（輸入時不顯示）：").strip()
  return key, sec


def _login(sj, simulation: bool, key: str, sec: str):
  api = sj.Shioaji(simulation=simulation)
  try:
    api.login(api_key=key, secret_key=sec, fetch_contract=True, subscribe_trade=False)
  except TypeError:  # Older versions without subscribe_trade.
    api.login(api_key=key, secret_key=sec, fetch_contract=True)
  return api


def _tmf_contract(api):
  group = api.Contracts.Futures.TMF  # Futures only — stocks are never touched.
  for attr in ("TMFR1",):
    c = getattr(group, attr, None)
    if c is not None:
      return c
  for c in group:  # Fallback: continuous near-month code.
    if getattr(c, "code", "").endswith("R1"):
      return c
  raise RuntimeError("找不到微台指連續近月合約（TMFR1）")


def fetch_kbars(api, contract, start: date, end: date, log=print) -> list[tuple]:
  """Downloads month by month to stay well within request limits. Returns [(ts_taipei, o, h, l, c, v)]."""
  rows: list[tuple] = []
  cur = start
  while cur <= end:
    nxt = min(end, cur + timedelta(days=30))
    kb = api.kbars(contract=contract, start=cur.isoformat(), end=nxt.isoformat())
    n = len(kb.ts)
    for i in range(n):
      # Shioaji kbar timestamps are Taipei wall-clock time encoded as epoch nanoseconds.
      ts = datetime.fromtimestamp(kb.ts[i] / 1e9, tz=timezone.utc).replace(tzinfo=None)
      rows.append((ts, float(kb.Open[i]), float(kb.High[i]), float(kb.Low[i]), float(kb.Close[i]), float(kb.Volume[i])))
    log(f"  {cur} ～ {nxt}：{n} 根 1 分K")
    cur = nxt + timedelta(days=1)
  rows.sort(key=lambda r: r[0])
  dedup, seen = [], set()
  for r in rows:
    if r[0] not in seen:
      seen.add(r[0])
      dedup.append(r)
  return dedup


def write_csv(rows: list[tuple], path: Path) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open("w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["ts_taipei", "open", "high", "low", "close", "volume"])
    for ts, o, h, l, c, v in rows:
      w.writerow([ts.strftime("%Y-%m-%d %H:%M:%S"), o, h, l, c, v])


def push_to_private_repo(csv_path: Path, log=print) -> None:
  repo_dir = ROOT / "data" / "kbars_repo"
  if not (repo_dir / ".git").exists():
    subprocess.run(["gh", "repo", "clone", DATA_REPO, str(repo_dir)], check=True)
  subprocess.run(["git", "-C", str(repo_dir), "pull", "-q"], check=False)
  target = repo_dir / csv_path.name
  target.write_bytes(csv_path.read_bytes())
  subprocess.run(["git", "-C", str(repo_dir), "add", csv_path.name], check=True)
  subprocess.run(["git", "-C", str(repo_dir), "commit", "-q", "-m", f"TMF 1-min kbars {datetime.now():%Y-%m-%d}"], check=False)
  subprocess.run(["git", "-C", str(repo_dir), "push", "-q"], check=True)
  log(f"已上傳到私人資料庫 github.com/{DATA_REPO}")


def export_history(days: int = 365, production: bool = False, push: bool = True, log=print) -> Path:
  import shioaji as sj

  key, sec = _creds()
  end = date.today()
  start = end - timedelta(days=days)
  log(f"登入永豐（{'正式' if production else '模擬'}環境，只使用行情資料 API，不會下單）…")
  api = _login(sj, simulation=not production, key=key, sec=sec)
  try:
    contract = _tmf_contract(api)
    log(f"下載 {getattr(contract, 'code', 'TMFR1')} {start} ～ {end} 的 1 分K：")
    rows = fetch_kbars(api, contract, start, end, log)
  finally:
    try:
      api.logout()
    except Exception:
      pass
  if not rows:
    raise SystemExit("沒有取得任何 K 棒。模擬環境可能不提供歷史資料，請改用：python3 -m futures_ai export-history --production"
                     "（API Key 需勾選「正式環境」與「行情/資料」，不需勾選交易）")
  out = OUT_DIR / "TMF_1min.csv"
  write_csv(rows, out)
  log(f"完成：{len(rows):,} 根 1 分K（{rows[0][0]:%Y-%m-%d} ～ {rows[-1][0]:%Y-%m-%d}）→ {out}")
  if push:
    push_to_private_repo(out, log)
  return out
