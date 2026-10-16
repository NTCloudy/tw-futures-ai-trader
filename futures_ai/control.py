"""Remote control from the GitHub Pages dashboard: emergency stop / resume.

Two channels, both polled by the engine with the GitHub API (authenticated, ~10 s):
1. control.json on the `control` branch (one-click button, needs a token saved in the browser).
2. Open issues titled STOP / RESUME created by the repo owner (no token needed: the button opens a
   pre-filled GitHub issue; only issues by the owner are honoured, then closed automatically).
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.request
from pathlib import Path

API = "https://api.github.com"


def _token() -> str:
  t = os.environ.get("GH_TOKEN", "")
  f = Path.home() / ".config" / "tw-futures-ai" / "gh_token"
  return t or (f.read_text(encoding="utf-8").strip() if f.exists() else "")


class GitHubControl:

  def __init__(self, cfg: dict, ledger, notifier=None):
    self.repo = cfg["publish"]["repo"]
    self.owner = self.repo.split("/")[0]
    self.ledger = ledger
    self.notifier = notifier
    self.etag = None
    self.last_poll = 0.0
    self.interval = cfg.get("control_poll_seconds", 10)

  def _req(self, method: str, path: str, body: dict | None = None, etag: str | None = None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "tw-futures-ai-trader"}
    tok = _token()
    if tok:
      headers["Authorization"] = f"Bearer {tok}"
    if etag:
      headers["If-None-Match"] = etag
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
    try:
      with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.headers.get("ETag"), json.load(r) if r.status != 204 else None
    except urllib.error.HTTPError as e:
      if e.code == 304:
        return 304, etag, None
      raise

  def _apply(self, kill: bool, source: str) -> None:
    if bool(self.ledger.get_state("kill_switch", False)) == kill:
      return
    self.ledger.set_state("kill_switch", kill)
    msg = ("⛔ 緊急停止：全部平倉並停止新單" if kill else "▶️ 已解除緊急停止，恢復自動交易") + f"（來源：{source}）"
    self.ledger.log(msg, level="KILL" if kill else "WARN")
    if self.notifier:
      self.notifier.urgent(msg)

  def poll(self) -> None:
    if time.monotonic() - self.last_poll < self.interval:
      return
    self.last_poll = time.monotonic()
    try:
      code, etag, data = self._req("GET", f"/repos/{self.repo}/contents/control.json?ref=control", etag=self.etag)
      if code == 200 and data:
        self.etag = etag
        ctl = json.loads(base64.b64decode(data["content"]).decode())
        last_seen = self.ledger.get_state("control_ts")
        if ctl.get("ts") and ctl.get("ts") != last_seen:  # Only act on new commands.
          self.ledger.set_state("control_ts", ctl["ts"])
          self._apply(bool(ctl.get("kill")), "網頁一鍵按鈕")
      code, _, issues = self._req("GET", f"/repos/{self.repo}/issues?state=open&creator={self.owner}&per_page=10")
      for it in sorted(issues or [], key=lambda i: i["created_at"]):
        title = it["title"].strip().upper()
        if title in ("STOP", "RESUME") and it["user"]["login"] == self.owner:
          self._apply(title == "STOP", f"GitHub Issue #{it['number']}")
          self._req("POST", f"/repos/{self.repo}/issues/{it['number']}/comments",
                    {"body": "已執行：" + ("緊急停止，全部平倉" if title == "STOP" else "恢復自動交易")})
          self._req("PATCH", f"/repos/{self.repo}/issues/{it['number']}", {"state": "closed"})
    except Exception as exc:  # Control channel problems must never stop trading logic.
      print(f"control poll failed: {exc}")
