"""Publishes state.json to the `live-state` branch so the GitHub Pages dashboard can read it."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .config import ROOT
from .state import build_state


class GitPublisher:

  def __init__(self, cfg: dict, ledger):
    self.cfg = cfg
    self.ledger = ledger
    self.dir = ROOT / "data" / "publish"
    self.branch = cfg["publish"]["branch"]

  def _git(self, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=self.dir, check=True, capture_output=True, text=True).stdout

  def _ensure_repo(self) -> None:
    if (self.dir / ".git").exists():
      return
    self.dir.mkdir(parents=True, exist_ok=True)
    self._git("init", "-q", "-b", self.branch)
    self._git("config", "user.name", "tw-futures-ai-bot")
    self._git("config", "user.email", "bot@users.noreply.github.com")
    self._git("remote", "add", "origin", f"https://github.com/{self.cfg['publish']['repo']}.git")

  def publish(self) -> None:
    self._ensure_repo()
    state = build_state(self.cfg, self.ledger)
    (self.dir / "state.json").write_text(json.dumps(state, ensure_ascii=False, default=str), encoding="utf-8")
    self._git("add", "state.json")
    has_head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=self.dir, capture_output=True).returncode == 0
    # Amend a single commit so the branch never accumulates history.
    self._git("commit", "-q", "--allow-empty", "-m", f"state {state['generated_at']}", *(["--amend"] if has_head else []))
    self._git("push", "-q", "-f", "origin", f"HEAD:{self.branch}")


def write_local_state(cfg: dict, ledger, path: Path) -> None:
  path.write_text(json.dumps(build_state(cfg, ledger), ensure_ascii=False, default=str), encoding="utf-8")
