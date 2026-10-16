"""CLI: python3 -m futures_ai <command>."""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .brokers import make_broker
from .config import ROOT, load_config
from .engine import Engine
from .ledger import Ledger
from .publisher import GitPublisher
from .state import build_state
from .strategies import build_strategies
from .trader import AiTrader


def build_traders(cfg: dict, ledger: Ledger) -> list[AiTrader]:
  return [AiTrader(cfg, s, make_broker(cfg, s.id), ledger) for s in build_strategies()]


def serve(cfg: dict, ledger: Ledger, port: int) -> None:
  web = ROOT / "web"

  class H(BaseHTTPRequestHandler):

    def _send(self, code, body: bytes, ctype: str):
      self.send_response(code)
      self.send_header("Content-Type", ctype)
      self.send_header("Cache-Control", "no-store")
      self.end_headers()
      self.wfile.write(body)

    def do_GET(self):
      if self.path.startswith("/state.json"):
        self._send(200, json.dumps(build_state(cfg, ledger), ensure_ascii=False, default=str).encode(), "application/json")
      elif self.path in ("/", "/index.html"):
        self._send(200, (web / "index.html").read_bytes(), "text/html; charset=utf-8")
      else:
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
      if self.path == "/api/kill":
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        ledger.set_state("kill_switch", bool(body.get("on")))
        ledger.log(f"緊急停止 {'啟動' if body.get('on') else '解除'}（本機儀表板）", level="WARN")
        self._send(200, b'{"ok":true}', "application/json")
      else:
        self._send(404, b"not found", "text/plain")

    def log_message(self, *a):
      pass

  print(f"本機儀表板：http://127.0.0.1:{port}")
  ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


def restore(cfg: dict, force: bool) -> int:
  import urllib.request
  from pathlib import Path
  db = Path(cfg["db_path"])
  if db.exists() and not force:
    print(f"{db} 已存在，加上 --force 才會覆蓋")
    return 1
  url = f"https://raw.githubusercontent.com/{cfg['publish']['repo']}/{cfg['publish']['branch']}/backup/futures.db"
  db.parent.mkdir(parents=True, exist_ok=True)
  with urllib.request.urlopen(url, timeout=30) as r:
    db.write_bytes(r.read())
  n = Ledger(str(db)).rows("SELECT COUNT(*) n FROM trades")[0]["n"]
  print(f"已還原 {db}（{n} 筆交易，含 AI 學習紀錄）")
  return 0


def main(argv=None) -> int:
  ap = argparse.ArgumentParser(prog="futures_ai", description="TW Futures AI Trader（微台指 AI 自動交易）")
  ap.add_argument("--config")
  sub = ap.add_subparsers(dest="cmd", required=True)
  run = sub.add_parser("run", help="啟動交易引擎（依 config 的 mode）")
  run.add_argument("--publish", action="store_true", help="每分鐘把狀態推到 GitHub live-state 分支")
  sub.add_parser("status", help="印出今日狀態")
  db = sub.add_parser("dashboard", help="本機儀表板")
  db.add_argument("--port", type=int)
  k = sub.add_parser("kill", help="緊急停止：平掉所有部位並停止新單")
  k.add_argument("state", choices=["on", "off"])
  sub.add_parser("sinopac-test", help="永豐模擬環境：登入 + 期貨下單測試（開通正式環境用）")
  sub.add_parser("line-setup", help="設定 LINE 通知（Channel access token + 你的 User ID）")
  sub.add_parser("line-test", help="傳一則 LINE 測試訊息")
  rs = sub.add_parser("restore", help="從 GitHub 備份還原交易紀錄與 AI 學習資料（換電腦用）")
  rs.add_argument("--force", action="store_true", help="覆蓋本機現有資料")
  args = ap.parse_args(argv)

  cfg = load_config(args.config)
  if args.cmd == "restore":
    return restore(cfg, args.force)
  if args.cmd in ("line-setup", "line-test"):
    from .notify import load_cred, push, save_cred
    if args.cmd == "line-setup":
      import getpass
      token = getpass.getpass("LINE Channel access token（輸入時不會顯示）：")
      to = input("你的 LINE User ID（U 開頭）：")
      save_cred(token, to)
    cred = load_cred()
    if not cred:
      print("尚未設定，請先執行 line-setup")
      return 1
    push(cred, "【微台 AI】LINE 通知設定成功 ✅ 之後會收到交易摘要、收盤摘要與緊急停止通知。")
    print("已送出測試訊息，請查看 LINE")
    return 0
  ledger = Ledger(cfg["db_path"])
  if args.cmd == "run":
    if args.publish:
      cfg["publish"]["enabled"] = True
    pub = GitPublisher(cfg, ledger) if cfg["publish"]["enabled"] else None
    Engine(cfg, ledger, build_traders(cfg, ledger), publisher=pub).run_forever()
  elif args.cmd == "status":
    st = build_state(cfg, ledger)
    print(f"{st['mode_label']}｜合約 {st['rollover']['contract']}（結算 {st['rollover']['settlement_date']}）｜最新價 {st['market'].get('last')}")
    for a in st["ais"]:
      pos = a["position"]
      ptxt = f"{'多' if pos['side'] == 'B' else '空'}單 @ {pos['entry_price']:.0f} 停利 {pos['tp']:.0f} 停損 {pos['sl']:.0f}" if pos else "空手"
      print(f"  {a['name']}: 今日 {a['today_pnl']:+,} ｜ 累計 {a['total_pnl']:+,} ｜ 近1h {a['pnl_1h']:+,} ｜ {ptxt}")
    print(f"  合計：今日 {st['combined']['today_pnl']:+,} ｜ 累計 {st['combined']['total_pnl']:+,}")
  elif args.cmd == "dashboard":
    serve(cfg, ledger, args.port or cfg["dashboard_port"])
  elif args.cmd == "kill":
    ledger.set_state("kill_switch", args.state == "on")
    ledger.log(f"緊急停止 {'啟動' if args.state == 'on' else '解除'}（CLI）", level="WARN")
    print(f"kill_switch = {args.state}")
  elif args.cmd == "sinopac-test":
    from .sinopac_setup import sinopac_test
    return 0 if sinopac_test(cfg) else 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
