"""Publishes state.json to the `live-state` branch so the GitHub Pages dashboard can read it."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

from .config import ROOT
from .state import build_state


class GitPublisher:

  def __init__(self, cfg: dict, ledger):
    self.cfg = cfg
    self.ledger = ledger
    self.dir = ROOT / "data" / "publish"
    self.branch = cfg["publish"]["branch"]
    self._last_backup = 0.0

  def _git(self, *args: str) -> str:
    """Runs git without any interactive prompt. Auth comes from GH_TOKEN or the token file
    ~/.config/tw-futures-ai/gh_token, so it works under cron / Task Scheduler on any OS."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    extra = []
    token = os.environ.get("GH_TOKEN", "")
    token_file = Path.home() / ".config" / "tw-futures-ai" / "gh_token"
    if not token and token_file.exists():
      token = token_file.read_text(encoding="utf-8").strip()
    if token:
      basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
      extra = ["-c", "credential.helper=", "-c", f"http.https://github.com/.extraheader=Authorization: Basic {basic}"]
    return subprocess.run(["git", *extra, *args], cwd=self.dir, check=True, capture_output=True, text=True,
                          env=env, timeout=60).stdout

  def _ensure_repo(self) -> None:
    if (self.dir / ".git").exists():
      return
    self.dir.mkdir(parents=True, exist_ok=True)
    self._git("init", "-q", "-b", self.branch)
    self._git("config", "user.name", "tw-futures-ai-bot")
    self._git("config", "user.email", "bot@users.noreply.github.com")
    self._git("remote", "add", "origin", f"https://github.com/{self.cfg['publish']['repo']}.git")

  def publish(self) -> None:
    """raw.githubusercontent.com caches each path ~5 minutes and ignores query strings, so every
    publish also writes a brand-new path slots/<epoch // interval>.json that the page computes from
    the clock. A new path is always fetched fresh from origin."""
    self._ensure_repo()
    interval = int(self.cfg["publish"]["interval_seconds"])
    state = build_state(self.cfg, self.ledger)
    state["publish_interval"] = interval
    body = json.dumps(state, ensure_ascii=False, default=str)
    slot = int(time.time() // interval)
    slots = self.dir / "slots"
    slots.mkdir(exist_ok=True)
    (slots / f"{slot}.json").write_text(body, encoding="utf-8")
    for f in slots.glob("*.json"):
      if f.stem.isdigit() and int(f.stem) < slot - 8:
        f.unlink()
    (self.dir / "state.json").write_text(body, encoding="utf-8")
    if time.time() - self._last_backup >= 600:  # Every 10 min: DB snapshot so another computer can restore.
      self._last_backup = time.time()
      (self.dir / "backup").mkdir(exist_ok=True)
      dst = sqlite3.connect(self.dir / "backup" / "futures.db")
      with self.ledger.lock:
        self.ledger.conn.backup(dst)
      dst.close()
    self._git("add", "-A")
    has_head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=self.dir, capture_output=True).returncode == 0
    # Amend a single commit so the branch never accumulates history.
    self._git("commit", "-q", "--allow-empty", "-m", f"state {state['generated_at']}", *(["--amend"] if has_head else []))
    self._git("push", "-q", "-f", "origin", f"HEAD:{self.branch}")


def write_local_state(cfg: dict, ledger, path: Path) -> None:
  path.write_text(json.dumps(build_state(cfg, ledger), ensure_ascii=False, default=str), encoding="utf-8")
