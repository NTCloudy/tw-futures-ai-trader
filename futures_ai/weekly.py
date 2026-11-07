"""Weekly self-improvement job (cron, Saturdays 10:00 Taipei):

1. Walk-forward training on all archived TAIFEX data (practice 40 days -> exam 20 days, rolling; 10 -> 5
   until 2026-11-07, too few trades per window for Alpha's US-open-only entries).
2. Compare the latest recommended parameters with the CURRENT live parameters on the same exam
   windows. In learning mode, switch an AI only if the recommendation scored better on unseen data.
3. Review the week's losing trades and validate recurring lessons as rules (lessons.py).
4. Rank the background shadow variants by their real-time (forward) results of the last 7 days.
5. Send the summary to LINE, save data/weekly_report.json, and restart the engine to load changes.
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


def live_exec() -> dict:
  """Live execution settings of Alpha / Sigma that the grid does not search (limit entry, entry hours, adopted
  lesson rules), taken from replay.sim_specs so training, the replay check and live trading stay identical."""
  from .replay import sim_specs
  specs = sim_specs()
  out = {}
  for live_id in SIM_ID:
    p = {k: v for k, v in specs[live_id][1].items() if k not in LIVE_KEYS[live_id] and k not in ("regimes", "flip")}
    p.pop("hours_free", None)  # Historical replay detail only; training uses the current hours.
    out[live_id] = p
  return out


def health_lines(cfg: dict, sessions: list) -> list[str]:
  """One-year backtest of every live AI with its current live settings: an AI losing money over the whole
  year is flagged for a structural review (small lesson rules alone are not enough)."""
  from . import lessons, slowtrain
  from .swing import simulate_swing
  params = json.loads((ROOT / "data" / "strategy_params.json").read_text(encoding="utf-8")) \
      if (ROOT / "data" / "strategy_params.json").exists() else {}
  c = dict(cfg)
  out = ["", "🩺 策略健康度（一年回測，目前設定）"]
  rows = []
  for ai, (sim_ai, p) in lessons._specs(params.get("lessons", {})).items():
    rows.append((lessons.NAMES[ai], wfo.simulate(c, sim_ai, p, sessions)))
  om = {**slowtrain.OMEGA_BASE, **slowtrain.LIVE_DEFAULTS["omega-swing"], **params.get("omega-swing", {})}
  rows.append(("Omega", simulate_swing(c, om, sessions)))
  for name, r in rows:
    flag = "🟢" if r["pnl"] > 0 else "🔴 待結構檢討"
    out.append(f"{name}：{r['pnl']:+,.0f}｜回撤 {r['maxdd']:,.0f}｜{r['trades']} 筆 {flag}")
  return out


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


SHADOW_NAMES = {"allday-alpha-orb": "Alpha（不限時段版）", "allday-sigma-vwap": "Sigma（不限時段版）",
                "allday-nova-nightorb": "Nova（不限時段版）", "nova-us": "Nova（只在美股開盤 1h 版）"}


def live_summary(ledger: Ledger, now: datetime | None = None, days: int = 7) -> list[str]:
  """This week's P&L per AI (realized, NT$): whole week, and only trades entered within 1 h / 2 h after
  the US cash open (DST-aware); then the top three AIs of the week."""
  from .calendar import us_open_at
  from .strategies import build_strategies
  now = now or datetime.now(TPE)
  since = (now - timedelta(days=days)).isoformat()
  out = ["📊 本週收益（模擬，NT$）", "AI｜本週｜美股開盤後1h｜後2h"]
  totals = []

  def us(rs, hours):
    sel = []
    for r in rs:
      t = datetime.fromisoformat(r["entry_ts"])
      o = us_open_at(t)
      if o <= t <= o + timedelta(hours=hours, minutes=10):
        sel.append(r)
    return sel

  def tot(rs):
    return sum(r["pnl_twd"] for r in rs)

  for s in build_strategies():
    if getattr(s, "hidden", False):
      continue
    rows = ledger.rows("SELECT * FROM trades WHERE ai=? AND exit_ts>=? ORDER BY id", (s.id, since))
    name = s.name.split(" ")[0]
    out.append(f"{name}｜{tot(rows):+,.0f}｜{tot(us(rows, 1)):+,.0f}｜{tot(us(rows, 2)):+,.0f}")
    totals.append((tot(rows), name))
  totals.sort(reverse=True)
  medals = ["🥇", "🥈", "🥉"]
  out.append("")
  out.append("🏆 本週前三：" + "　".join(f"{medals[i]} {n} {v:+,.0f}" for i, (v, n) in enumerate(totals[:3])))
  return out


def shadow_summary(now: datetime | None = None, days: int = 7, ledger: Ledger | None = None) -> list[str]:
  """Background shadow versions (same rules, different time window), in plain names: win rate + total."""
  shadow_db = ROOT / "data" / "shadow.db"
  if not shadow_db.exists():
    return []
  sl = Ledger(str(shadow_db))
  try:
    since = ((now or datetime.now(TPE)) - timedelta(days=days)).isoformat()
    out = ["", "👥 影子版本（背景模擬，近 7 天）"]
    for ai, label in SHADOW_NAMES.items():
      rs = sl.rows("SELECT pnl_twd FROM trades WHERE ai=? AND exit_ts>=?", (ai, since))
      if ai == "nova-us" and ledger is not None:  # Front-stage until 10/22, then a shadow: count both.
        rs += ledger.rows("SELECT pnl_twd FROM trades WHERE ai=? AND exit_ts>=?", (ai, since))
      if not rs:
        out.append(f"{label}：本週沒有交易")
        continue
      wins = sum(1 for r in rs if r["pnl_twd"] > 0)
      out.append(f"{label}：勝率 {wins / len(rs) * 100:.0f}%｜合計 {sum(r['pnl_twd'] for r in rs):+,.0f}")
    return out
  finally:
    sl.conn.close()


def run_weekly(cfg: dict, train_days: int = 40, test_days: int = 20, restart: bool = True) -> dict:
  ledger = Ledger(cfg["db_path"])
  cfg = dict(cfg)
  sessions = wfo.load_sessions(cfg)
  exec_base = live_exec()
  cur = current_params()
  rep = wfo.walk_forward(cfg, train_days, test_days, sessions, ais=tuple(SIM_ID.values()), log=lambda *_: None,
                         base={SIM_ID[k]: v for k, v in exec_base.items()},
                         cur_params={SIM_ID[k]: cur[k] for k in SIM_ID if k in cur})
  days = sorted({s.td for s in sessions})
  by_day = {d: [s for s in sessions if s.td == d] for d in days}
  params_file = ROOT / "data" / "strategy_params.json"
  live_params = json.loads(params_file.read_text(encoding="utf-8")) if params_file.exists() else {}
  try:
    from .replay import run_replay, summary_lines
    replay_lines = ["", *summary_lines(run_replay(cfg))]
  except Exception as exc:
    replay_lines = ["", f"回測 vs 即時 對帳失敗：{exc}"]
  try:
    from .stocks import stock_lines
    stock_week = stock_lines("week", (datetime.now(TPE) - timedelta(days=1)).strftime("%Y-%m-%d"))
    if stock_week:
      stock_week = ["", "— 本週股票 AI 損益 —", *stock_week[1:]]
  except Exception as exc:
    stock_week = ["", f"股票週報讀取失敗：{exc}"]
  lines = [f"【微台 AI 每週報告】{datetime.now(TPE):%m/%d}", *live_summary(ledger), *replay_lines, *stock_week, "",
           "— 訓練（技術細節）—",
           f"資料 {rep['days'][0]} ～ {rep['days'][1]}（{len(days)} 個交易日）"]
  changes = {}
  auto_adopt = bool(cfg.get("auto_adopt_lessons", True))
  for live_id, sim_id in SIM_ID.items():
    r = rep["ais"][sim_id]
    cur_sim = {**exec_base[live_id], **cur[live_id], "regimes": DEFAULT_REGIMES[live_id], "flip": ()}
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
    if not frozen and auto_adopt and not new.get("off") and r["windows"] and r["oos_pnl"] > cur_oos:
      kw = {k: new[k] for k in LIVE_KEYS[live_id] if k in new}
      if kw != {k: cur[live_id].get(k) for k in kw}:
        live_params[live_id] = kw
        changes[live_id] = kw
        ledger.set_state(f"strategy_since:{live_id}", datetime.now(TPE).isoformat(timespec="seconds"))
        switched = True
    if switched:
      verdict = "✅ 已換成新參數"
    elif new.get("off"):
      verdict = "訓練建議暫停（最近練習期沒有任何組合賺錢）；維持目前參數繼續觀察"
    elif r["oos_pnl"] <= cur_oos:
      verdict = "目前參數考得比較好，維持"
    else:
      verdict = "維持目前參數"
    lines.append(f"\n{NAMES[live_id]}：考試期 訓練挑選 {r['oos_pnl']:+,} vs 目前參數 {cur_oos:+,.0f}"
                 f"（{r['oos_trades']} 筆，勝 {r['oos_wins']}）→ {verdict}")
  # Nova and Omega: longer rolling windows (they trade less often); same "switch only if better" rule.
  from . import slowtrain
  frozen = datetime.now(TPE).date().isoformat() <= cfg.get("params_frozen_until", "")
  for ai in ("nova-nightorb", "omega-swing"):
    try:
      r = slowtrain.train(cfg, ai, sessions, live_params.get(ai))
    except Exception as exc:
      lines.append(f"\n{slowtrain.NAMES[ai]}：訓練失敗（{exc}），維持目前參數")
      continue
    head = (f"\n{slowtrain.NAMES[ai]}：考試期 訓練挑選 {r['oos_pnl']:+,} vs 目前參數 {r['cur_oos']:+,}"
            f"（{r['trades']} 筆，勝 {r['wins']}）")
    if r["switch"] and auto_adopt and not frozen:
      live_params[ai] = r["latest"]
      changes[ai] = r["latest"]
      ledger.set_state(f"strategy_since:{ai}", datetime.now(TPE).isoformat(timespec="seconds"))
      lines.append(f"{head} → ✅ 已換成新參數 {r['latest']}")
    elif r["switch"]:
      lines.append(f"{head} → 建議改為 {r['latest']}（參數凍結中，暫不更換）")
    else:
      lines.append(f"{head} → 目前參數考得比較好，維持")
  if changes:
    params_file.write_text(json.dumps(live_params, ensure_ascii=False, indent=2), encoding="utf-8")
  # Losing-trade review -> recurring lessons -> backtested + forward-validated rules (lessons.py).
  try:
    from . import lessons
    lr = lessons.run(cfg, ledger, sessions, adopt=auto_adopt and not frozen)
    lines += lr["lines"]
    if lr["changed"]:
      changes["lessons"] = True
  except Exception as exc:
    lines.append(f"\n📚 虧損檢討失敗：{exc}")
  try:
    lines += health_lines(cfg, sessions)
  except Exception as exc:
    lines.append(f"\n🩺 策略健康度計算失敗：{exc}")

  lines += shadow_summary(ledger=ledger)

  report = {"at": datetime.now(TPE).isoformat(timespec="seconds"), "wfo": rep, "changes": changes, "text": "\n".join(lines)}
  (ROOT / "data" / "weekly_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
  ledger.log("每週訓練完成" + (f"，更新參數：{', '.join(changes)}" if changes else "，參數維持不變"), level="WARN")
  ledger.conn.close()
  cred = load_cred()
  if cred:
    try:
      push(cred, report["text"][:4900])
    except Exception as exc:
      print(f"LINE push failed: {exc}")
  if restart and changes:
    subprocess.run(["pkill", "-f", r"^python3 -m futures_ai( .*)? run(\s|$)"], check=False)
    time.sleep(5)
    subprocess.run([str(ROOT / "scripts" / "start.sh")], check=False)
  print(report["text"])
  return report
