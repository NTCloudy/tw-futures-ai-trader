"""Weekly self-improvement job (cron, Saturdays 10:00 Taipei):

1. Walk-forward training on all archived TAIFEX data (practice 10 days -> exam 5 days, rolling).
2. Compare the latest recommended parameters with the CURRENT live parameters on the same exam
   windows. In learning mode, switch an AI only if the recommendation scored better on unseen data.
3. Rank the background shadow variants by their real-time (forward) results of the last 7 days.
4. Send the summary to LINE, save data/weekly_report.json, and restart the engine to load changes.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timedelta

from .calendar import TPE
from .config import ROOT
from .ledger import Ledger
from .notify import load_cred, push
from .state import health
from . import wfo

SIM_ID = {"alpha-orb": "alpha-orb", "sigma-vwap": "sigma-pullback"}
LIVE_KEYS = {"alpha-orb": ("range_bars", "sl_frac", "tp_mult"),
             "sigma-vwap": ("pull_atr", "sl_atr", "tp_mult", "trail_atr"),
             "gamma-scalp": ("lookback", "sl_atr", "trail_atr")}
DEFAULT_REGIMES = {"alpha-orb": ("trend", "chop"), "sigma-vwap": ("trend",), "gamma-scalp": ("trend",)}
NAMES = {"alpha-orb": "Alpha 開盤突破", "sigma-vwap": "Sigma 趨勢回測", "gamma-scalp": "Gamma 趨勢突破"}


def current_params() -> dict:
  from .strategies import build_strategies
  out = {}
  for s in build_strategies():
    if s.id not in LIVE_KEYS:
      continue
    attrs = {"range_bars": "range_bars", "sl_frac": "sl_frac", "tp_mult": "tp_mult", "pull_atr": "pull",
             "sl_atr": "sl_atr", "trail_atr": "trail_atr", "lookback": "n"}
    out[s.id] = {k: getattr(s, attrs[k]) for k in LIVE_KEYS[s.id]}
  return out


def live_summary(ledger: Ledger, now: datetime | None = None, days: int = 7) -> list[str]:
  """Concise real-time (paper) results of the last `days` days per visible AI: whole week, and only
  trades entered within 1 h / 2 h after the US cash open (DST-aware), plus open positions."""
  from .calendar import us_open_at
  from .strategies import build_strategies
  now = now or datetime.now(TPE)
  since = (now - timedelta(days=days)).isoformat()
  out = [f"📊 近 {days} 天實戰（模擬）成績", "AI｜全部 筆/勝/損益｜美股開盤後1h｜後2h"]
  for s in build_strategies():
    if getattr(s, "hidden", False):
      continue
    rows = ledger.rows("SELECT * FROM trades WHERE ai=? AND entry_ts>=? ORDER BY id", (s.id, since))
    def agg(rs):
      return f"{len(rs)}/{sum(1 for r in rs if r['pnl_twd'] > 0)}/{sum(r['pnl_twd'] for r in rs):+,.0f}"
    def us(rs, hours):
      sel = []
      for r in rs:
        t = datetime.fromisoformat(r["entry_ts"])
        o = us_open_at(t)
        if o <= t < o + timedelta(hours=hours):
          sel.append(r)
      return sel
    name = s.name.split(" ")[0]
    line = f"{name}｜{agg(rows)}｜{agg(us(rows, 1))}｜{agg(us(rows, 2))}"
    miss = ledger.rows("SELECT COUNT(*) n FROM orders WHERE ai=? AND ts>=? AND status='CANCELLED' AND note LIKE '%逾時未成交%'",
                       (s.id, since))[0]["n"]
    if miss:
      line += f"｜限價未成交 {miss}"
    pos = ledger.get_state(f"pos:{s.id}")
    if pos:
      line += f"｜留倉 {'多' if pos['side'] == 'B' else '空'}單@{pos['entry_price']:.0f}"
    out.append(line)
  out.append("（筆/勝/損益，單位 NT$；已實現損益，不含留倉浮動）")
  return out


def run_weekly(cfg: dict, train_days: int = 10, test_days: int = 5, restart: bool = True) -> dict:
  ledger = Ledger(cfg["db_path"])
  cfg = dict(cfg, max_entries_per_session=999 if cfg.get("learning_mode") else cfg["max_entries_per_session"])
  sessions = wfo.load_sessions(cfg)
  rep = wfo.walk_forward(cfg, train_days, test_days, sessions, ais=tuple(SIM_ID.values()), log=lambda *_: None)
  days = sorted({s.td for s in sessions})
  by_day = {d: [s for s in sessions if s.td == d] for d in days}
  cur = current_params()
  params_file = ROOT / "data" / "strategy_params.json"
  live_params = json.loads(params_file.read_text(encoding="utf-8")) if params_file.exists() else {}
  try:
    from .replay import run_replay, summary_lines
    replay_lines = ["", *summary_lines(run_replay(cfg))]
  except Exception as exc:
    replay_lines = ["", f"回測 vs 即時 對帳失敗：{exc}"]
  lines = [f"【微台 AI 每週報告】{datetime.now(TPE):%m/%d}", *live_summary(ledger), *replay_lines, "",
           "— 訓練（技術細節）—",
           f"資料 {rep['days'][0]} ～ {rep['days'][1]}（{len(days)} 個交易日）"]
  changes = {}
  for live_id, sim_id in SIM_ID.items():
    r = rep["ais"][sim_id]
    cur_sim = {**cur[live_id], "regimes": DEFAULT_REGIMES[live_id], "flip": ()}
    cur_oos = 0.0
    for w in r["windows"]:
      test_days_ = [d for d in days if w["test"][0] <= str(d) <= w["test"][1]]
      cur_oos += wfo.simulate(cfg, sim_id, cur_sim, [s for d in test_days_ for s in by_day[d]])["pnl"]
    new = r["latest_params"]
    switched = False
    frozen = datetime.now(TPE).date().isoformat() <= cfg.get("params_frozen_until", "")
    if frozen and not new.get("off") and r["oos_pnl"] > cur_oos:
      kw = {k: new[k] for k in LIVE_KEYS[live_id] if k in new}
      lines.append(f"\n{NAMES[live_id]}：評選期間參數凍結（至 {cfg['params_frozen_until']}），"
                   f"建議改為 {kw}（考試期 {r['oos_pnl']:+,} vs 目前 {cur_oos:+,.0f}），評選後統一決定")
      continue
    if not frozen and cfg.get("learning_mode") and not new.get("off") and r["windows"] and r["oos_pnl"] > cur_oos:
      kw = {k: new[k] for k in LIVE_KEYS[live_id] if k in new}
      if kw != {k: cur[live_id].get(k) for k in kw}:
        live_params[live_id] = kw
        changes[live_id] = kw
        ledger.set_state(f"strategy_since:{live_id}", datetime.now(TPE).isoformat(timespec="seconds"))
        switched = True
    if switched:
      verdict = "✅ 已換成新參數"
    elif new.get("off"):
      verdict = "訓練建議暫停（最近練習期沒有任何組合賺錢）；學習模式下照常交易累積經驗"
    elif r["oos_pnl"] <= cur_oos:
      verdict = "目前參數考得比較好，維持"
    else:
      verdict = "維持目前參數"
    lines.append(f"\n{NAMES[live_id]}：考試期 訓練挑選 {r['oos_pnl']:+,} vs 目前參數 {cur_oos:+,.0f}"
                 f"（{r['oos_trades']} 筆，勝 {r['oos_wins']}）→ {verdict}")
  if changes:
    params_file.write_text(json.dumps(live_params, ensure_ascii=False, indent=2), encoding="utf-8")

  shadow_db = ROOT / "data" / "shadow.db"
  if shadow_db.exists():
    sl = Ledger(str(shadow_db))
    since = (datetime.now(TPE) - timedelta(days=7)).isoformat()
    ranks = []
    for ai in {r["ai"] for r in sl.rows("SELECT DISTINCT ai FROM trades")}:
      h = health(sl.rows("SELECT * FROM trades WHERE ai=? AND entry_ts>=? ORDER BY id", (ai, since)))
      if h["n"]:
        ranks.append((h["expectancy"] * h["n"], ai, h))
    ranks.sort(reverse=True)
    if ranks:
      lines.append("\n影子版本近 7 天即時表現（前 3 名）：")
      for total, ai, h in ranks[:3]:
        pf = "∞" if h["profit_factor"] is None else f"{h['profit_factor']:.2f}"
        lines.append(f"  {ai}：{h['n']} 筆，勝率 {h['win_rate']:.0f}%，獲利因子 {pf}，合計 {total:+,.0f}")

  report = {"at": datetime.now(TPE).isoformat(timespec="seconds"), "wfo": rep, "changes": changes, "text": "\n".join(lines)}
  (ROOT / "data" / "weekly_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
  ledger.log("每週訓練完成" + (f"，更新參數：{', '.join(changes)}" if changes else "，參數維持不變"), level="WARN")
  cred = load_cred()
  if cred:
    try:
      push(cred, report["text"][:4900])
    except Exception as exc:
      print(f"LINE push failed: {exc}")
  if restart and changes:
    subprocess.run(["pkill", "-f", "^python3 -m futures_ai run$"], check=False)
    time.sleep(5)
    subprocess.run([str(ROOT / "scripts" / "start.sh")], check=False)
  print(report["text"])
  return report
