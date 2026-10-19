"""LINE notifications via the LINE Messaging API (LINE Notify was discontinued in 2025).

Setup: create a LINE Official Account + Messaging API channel, issue a channel access token, add the
account as a friend, then run `python3 -m futures_ai line-setup`. Credentials are stored in
~/.config/tw-futures-ai/line.json (owner-only permissions), never in the repo.

The free plan has a small monthly message quota, so trade events are batched into a digest
(default every 60 minutes during sessions), plus a summary when each session closes. Emergency
stops, resumes and repeated engine errors are sent immediately.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime
from pathlib import Path

CRED = Path.home() / ".config" / "tw-futures-ai" / "line.json"
PUSH_URL = "https://api.line.me/v2/bot/message/push"


def load_cred() -> dict | None:
  if os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") and os.environ.get("LINE_USER_ID"):
    return {"token": os.environ["LINE_CHANNEL_ACCESS_TOKEN"], "to": os.environ["LINE_USER_ID"]}
  return json.loads(CRED.read_text(encoding="utf-8")) if CRED.exists() else None


def save_cred(token: str, to: str) -> None:
  CRED.parent.mkdir(parents=True, exist_ok=True)
  CRED.write_text(json.dumps({"token": token.strip(), "to": to.strip()}), encoding="utf-8")
  os.chmod(CRED, 0o600)


def push(cred: dict, text: str) -> None:
  body = json.dumps({"to": cred["to"], "messages": [{"type": "text", "text": text[:4900]}]}).encode()
  req = urllib.request.Request(PUSH_URL, data=body, method="POST", headers={
      "Content-Type": "application/json", "Authorization": f"Bearer {cred['token']}"})
  with urllib.request.urlopen(req, timeout=10) as r:
    r.read()


class LineNotifier:

  def __init__(self, cfg: dict, ledger):
    self.cfg = cfg["notify"]
    self.ledger = ledger
    self.cred = load_cred()
    self.enabled = bool(self.cfg.get("line_enabled") and self.cred)
    self.last_digest = time.monotonic()
    self.last_event_id = ledger.get_state("notify_last_event", None)
    if self.last_event_id is None:
      rows = ledger.rows("SELECT COALESCE(MAX(id),0) m FROM events")
      self.last_event_id = rows[0]["m"]
    self.pending: list[str] = ledger.get_state("notify_pending", []) or []  # Survives restarts.
    self.errors = 0

  def _send(self, text: str) -> None:
    if not self.enabled:
      return
    try:
      push(self.cred, text)
      self.ledger.log("LINE 通知已送出", level="INFO")
    except Exception as exc:
      print(f"LINE push failed: {exc}")

  def urgent(self, text: str) -> None:
    self._send(f"【微台 AI】{text}")

  def collect(self, names: dict) -> None:
    rows = self.ledger.rows("SELECT * FROM events WHERE id>? ORDER BY id", (self.last_event_id,))
    for e in rows:
      self.last_event_id = e["id"]
      alert = e["ai"] in self.cfg.get("alert_ais", []) and e["level"] in ("TRADE", "SIGNAL")
      if alert:  # Real-time alert for AIs the user follows by hand (signal, fill, exit, cancel).
        self.urgent(f"{e['ts'][11:16]} {names.get(e['ai'], e['ai'])}\n{e['message'][:300]}")
      if e["level"] == "TRADE":
        t = e["ts"][11:16]
        self.pending.append(f"{t} {names.get(e['ai'], e['ai'])}：{e['message'].split('｜')[0]}")
      elif e["level"] == "ERROR":
        self.errors += 1
    if rows:
      self.ledger.set_state("notify_last_event", self.last_event_id)
      self.ledger.set_state("notify_pending", self.pending)
    if self.errors >= 5:  # Repeated engine errors: likely data feed or network problem.
      self.urgent(f"⚠️ 引擎連續出現 {self.errors} 次錯誤，請檢查（行情或網路）")
      self.errors = 0

  def maybe_digest(self, header: str) -> None:
    if self.cfg.get("digest_minutes", 0) <= 0:  # Digests disabled: trades go into the session report.
      return
    if self.pending and time.monotonic() - self.last_digest >= self.cfg["digest_minutes"] * 60:
      self._send(f"【微台 AI 交易摘要】{header}\n" + "\n".join(self.pending[-25:]))
      self.pending.clear()
      self.ledger.set_state("notify_pending", [])
      self.last_digest = time.monotonic()

  def defer_night(self, text: str) -> None:
    """Stores the night-session report (with its trades) to be sent the next morning."""
    self.pending.clear()  # Reports stay short: no per-trade list.
    self.ledger.set_state("notify_pending", [])
    self.ledger.set_state("night_report", text)

  def maybe_send_night(self, now: datetime) -> None:
    text = self.ledger.get_state("night_report")
    hh, mm = (int(x) for x in self.cfg.get("night_report_time", "08:30").split(":"))
    if text and (now.hour, now.minute) >= (hh, mm) and now.hour < 15:
      self._send(text)
      self.ledger.set_state("night_report", None)

  def session_summary(self, text: str) -> None:
    self.pending.clear()
    self.ledger.set_state("notify_pending", [])
    self._send(text)
    self.last_digest = time.monotonic()


def summary_text(state: dict, session_label: str) -> str:
  """Short report: each AI's PnL for the session, open positions, and notable news (if any)."""
  title = "昨晚夜盤" if session_label == "夜盤" else "今日日盤"
  lines = [f"【微台 AI】{title}結果（交易日 {state['rollover']['trading_day']}）"]
  for a in state["ais"]:
    lines.append(f"{a['name'].replace(' AI', '')}：{a['today_pnl']:+,}")
  c = state["combined"]
  lines.append(f"合計：{c['today_pnl']:+,}｜累計 {c['total_pnl']:+,}")
  held = [a for a in state["ais"] if a.get("position")]
  if held:
    lines.append("")
    lines.append("目前留倉：")
    for a in held:
      p = a["position"]
      lines.append(f"・{a['name'].replace(' AI', '')} {'多' if p['side'] == 'B' else '空'}單 @ {p['entry_price']:.0f}"
                   f"（停損 {p['sl']:.0f}，未實現 {p['unrealized']:+,}）")
  else:
    lines.append("目前無留倉")
  news = state.get("news") or {}
  if news.get("mode") in ("elevated", "high", "positive") or news.get("fomc_window"):
    zh = {"high": "🔴 高風險", "elevated": "🟡 風險升高", "positive": "🟢 偏多消息"}.get(news.get("mode"), "")
    lines.append("")
    lines.append(f"新聞：{zh}" + ("（聯準會決議時段）" if news.get("fomc_window") else ""))
    for h in (news.get("headlines") or [])[:3]:
      lines.append(f"・{h['title'][:50]}")
  return "\n".join(lines)
