"""News & event risk radar (rule-based, transparent).

Sources (free, no API key): Google News RSS searches in Traditional Chinese and English for
Trump / tariffs / war / Taiwan Strait / chips / Fed, plus the Fed's FOMC meeting calendar.

Each headline (last 12 h) gets a score; headlines about the same story (similar titles from several
outlets) are merged and counted once:
  + risk words (加徵關稅、戰爭、飛彈、制裁、出口管制、暴跌、升息 …)
  - relief words (豁免、暫停關稅、協議、停火、降息、大漲 …)
  x1.5 Taiwan / chips / US stocks, x1.2 US-China policy, x0.8 US policy, x0.3 China only, x0.15 other;
  x0.4 for opinion pieces; x1.2 if it mentions Trump;
  decayed with a 3-hour half-life. The total sets the market risk mode:
  compared with the median of the past 7 days (so ever-present background news is ignored):
  normal / elevated (+8 above baseline) / high (+15) / positive (-8 below). Needs ~2 h to build a baseline.

Limitations: keyword rules can misread a headline, RSS arrives minutes after the news, and the
effect cannot be back-tested (no historical headline archive). It is a risk filter, not a predictor.
"""

from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime

from .calendar import TPE

QUERIES = [
    ("川普 關稅 OR 川普 制裁 OR 川普 台灣 OR 對等關稅 OR 晶片關稅", "zh"),
    ("台海 OR 共軍 OR 軍演 OR 戰爭 OR 飛彈 OR 圍台", "zh"),
    ("台股 暴跌 OR 台股 重挫 OR 台積電 法說 OR 台積電 ADR OR 半導體 出口管制", "zh"),
    ("國安基金 OR 進場護盤 OR 限空令 OR 禁空令 OR 八大官股 OR 救市 when:2d", "zh"),
    ("聯準會 OR 美國 CPI OR 非農就業 OR 台灣央行 升息 OR 存準率 OR 美股 重挫", "zh"),
    ("台指期 外資 空單 OR 外資 賣超 OR 新台幣 重貶 OR 融資 斷頭 OR MSCI 調整", "zh"),
    ("Trump tariff OR Trump sanctions OR Taiwan Strait OR China Taiwan military", "en"),
    ("stock market selloff OR Nasdaq plunge OR chip export controls OR Fed rate OR Nvidia earnings", "en"),
]

STRONG_RISK = ["開戰", "戰爭", "入侵", "飛彈", "導彈", "封鎖", "攻擊", "空襲", "核武", "圍台軍演", "實彈射擊",
               "熔斷", "閃崩", "war ", "invasion", "missile", "blockade", "airstrike", "attack on"]
RISK = ["加徵", "課徵", "提高關稅", "新關稅", "關稅戰", "對等關稅", "晶片關稅", "232調查", "實體清單",
        "報復", "制裁", "禁令", "出口管制", "暴跌", "崩盤", "崩跌", "重挫", "大跌", "跳水", "下殺", "跌停",
        "下修財測", "資本支出下調", "adr重挫", "adr大跌", "輝達重挫", "外資大賣", "外資狂砍", "淨空單飆",
        "空單暴增", "空單創", "空單破", "台幣重貶", "台幣急貶", "股匯雙殺", "融資斷頭", "追繳令", "權重雙降",
        "cpi高於預期", "通膨升溫", "非農爆冷", "鷹派", "調升存準率", "升準", "信用管制", "殖利率飆",
        "升息", "衰退", "違約", "破產", "暴雷", "軍演", "緊張", "威脅", "tariff hike", "impose", "new tariffs",
        "retaliat", "sanction", "export control", "ban ", "plunge", "selloff", "sell-off", "tumble", "rate hike",
        "recession", "default", "threat", "escalat", "tension"]
RESCUE_POLICY = ["國安基金", "進場護盤", "啟動護盤", "護盤", "限空令", "禁空令", "平盤下禁止放空",
                 "調高融券保證金", "救市", "八大官股", "官股護盤", "國家隊", "四大基金進場", "安定市場",
                 "暫緩關稅", "關稅豁免", "取消關稅", "暫停關稅"]
POLICY_NEGATED = ["不進場", "暫不進場", "不護盤", "暫不護盤", "無須護盤", "未達護盤", "不召開臨時會", "退場", "否認護盤"]
DROP_WORDS = {"暴跌", "崩盤", "崩跌", "大跌", "重挫", "跳水", "下殺", "跌停", "plunge", "selloff", "sell-off", "tumble"}
BEARISH_SUBJECTS = ("空單", "淨空單", "賣超", "殖利率", "通膨", "失業", "貶", "斷頭", "違約", "vix", "恐慌")
HIGH_WORDS = {"創高", "創新高", "創歷史新高", "record high"}
NEG_PREFIXES = ("不", "無", "未", "否認", "駁斥", "排除", "暫緩", "暫停", "取消", "豁免", "解除", "放寬")
RELIEF = ["豁免", "降關稅", "協議", "停火", "和談", "降息", "大漲", "急拉", "強彈", "飆漲", "大反彈", "止跌",
          "創高", "創新高", "創歷史新高", "緩和", "解除", "法說報喜", "上修財測", "營收創高", "優於預期",
          "adr大漲", "輝達大漲", "通膨降溫", "cpi低於預期", "鴿派", "軟著陸", "外資大買", "外資回補",
          "空單大減", "台幣強升", "權重調升", "waive", "exempt", "pause tariff", "deal", "agreement",
          "ceasefire", "truce", "rate cut", "rally", "record high", "ease", "lifts"]
# Relevance tiers (what actually moves TAIEX futures): Taiwan / chips / US stocks / policy / futures chips directly,
# or US policy toward China / the world. China news without a US link and unrelated geopolitics are heavily discounted.
TAIWAN_MKT = ["台灣", "台股", "台指", "期貨", "夜盤", "台積電", "台積", "2330", "adr", "鴻海", "聯發科", "廣達",
              "半導體", "晶片", "CoWoS", "cowos", "台海", "美股", "那斯達克", "那指", "費半", "費城半導體", "道瓊",
              "標普", "輝達", "nvidia", "nvda", "vix", "國安基金", "金管會", "財政部", "央行", "理監事會", "存準率",
              "外資", "官股", "八大", "國家隊", "空單", "未平倉", "融資", "新台幣", "台幣", "匯率", "msci", "富時",
              "結算", "限空令", "禁空令", "護盤", "救市", "法說", "taiwan", "tsmc", "semiconductor", "chip",
              "nasdaq", "s&p", "sox", "wall street", "stocks"]
US_POLICY = ["美國", "美方", "白宮", "川普", "聯準會", "鮑爾", "fomc", "cpi", "非農", "pce", "美債", "殖利率",
             "u.s.", "us ", "white house", "trump", "fed "]
CHINA = ["中國", "中共", "北京", "共軍", "解放軍", "china", "beijing", "chinese"]
OPINION = ["專欄", "解析", "為何", "分析", "評論", "社論", "觀點", "專家", "opinion", "analysis", "commentary", "explainer"]
RELEVANT = TAIWAN_MKT + CHINA  # Kept for compatibility.
TRUMP = ["川普", "trump"]

FED_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August",
                                      "September", "October", "November", "December"], 1)}


def _get(url: str, timeout: int = 15) -> str:
  req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 tw-futures-ai-trader"})
  with urllib.request.urlopen(req, timeout=timeout) as r:
    return r.read().decode("utf-8", errors="ignore")


# Phrases that negate or reverse a risk word ("no sanctions", "eases sanctions", "end of the war"):
# they count as relief and the risk word inside them is not scored.
NEGATED = ["不會施加制裁", "不會制裁", "不制裁", "放寬制裁", "暫緩制裁", "解除制裁", "放寬對", "暫緩對", "結束戰爭", "戰爭結束",
           "終戰", "停戰", "no sanctions", "ease sanctions", "eases sanctions", "lift sanctions", "end the war", "end of the war"]
STORY_SIM = 0.3  # Character-bigram Jaccard similarity above which two headlines are the same story.


def _story_key(title: str) -> set[str]:
  t = re.sub(r"\s+-\s+[^-]+$", "", title)  # Drop the " - Source" suffix.
  t = re.sub(r"[【\[].*?[】\]]", "", t)  # Drop topic tags such as 【俄烏戰爭】.
  t = re.sub(r"[\W_]+", "", t.lower())
  return {t[i:i + 2] for i in range(len(t) - 1)}


def _similar(a: set[str], b: set[str]) -> bool:
  return bool(a and b) and len(a & b) / len(a | b) >= STORY_SIM


def cluster_stories(items: list[dict]) -> list[dict]:
  """Merges headlines about the same story (several outlets, reworded titles) into one entry so a
  story is scored once, not once per outlet. Keeps the strongest headline; `n` = number of reports."""
  out: list[dict] = []
  for h in sorted(items, key=lambda x: -abs(x["score"])):
    k = _story_key(h["title"])
    for c in out:
      if any(_similar(k, m) for m in c["_keys"]):
        c["n"] += 1
        c["_keys"].append(k)
        break
    else:
      out.append({**h, "n": 1, "_keys": [k]})
  for c in out:
    del c["_keys"]
  return out


def score_headline(title: str) -> tuple[float, list[str]]:
  t_orig = re.sub(r"[【\[].*?[】\]]", " ", title.lower())
  for role in ("戰爭部長", "department of war", "secretary of war"):
    t_orig = t_orig.replace(role, " ")
  t = t_orig
  stalled = any(w in t for w in ("相距甚遠", "破局", "卡關", "破裂", "無果", "難產", "far from", "stalled", "deadlock", "collapses", "fails"))
  hits, s = [], 0.0
  for pn in POLICY_NEGATED:
    if pn.lower() in t:
      t = t.replace(pn.lower(), " ")
  has_rescue = False
  for w in RESCUE_POLICY:
    wl = w.lower()
    idx = t.find(wl)
    if idx >= 0:
      prefix = t[max(0, idx - 6):idx]
      if any(np in prefix for np in ("不", "無", "未", "否認", "駁斥", "排除")):
        t = t.replace(wl, " ")
        continue
      s -= 2.5
      hits.append(f"-{w.strip()}")
      has_rescue = True
      t = t.replace(wl, " ")
  for w in NEGATED:
    if w.lower() in t:
      if stalled and w in ("結束戰爭", "戰爭結束", "終戰", "停戰", "end the war", "end of the war"):
        t = t.replace(w.lower(), " ")
        continue
      s -= 2
      hits.append(f"-{w}")
      t = t.replace(w.lower(), " ")
  for w in STRONG_RISK:
    wl = w.lower()
    if wl in t:
      idx0 = t_orig.find(wl)
      prefix = t_orig[max(0, idx0 - 6):idx0] if idx0 >= 0 else ""
      if any(np in prefix for np in NEG_PREFIXES):
        s -= 1.0
        hits.append(f"~否定{w.strip()}")
      elif has_rescue and w.strip().lower() in DROP_WORDS:
        hits.append(f"~背景{w.strip()}")
      else:
        s += 3
        hits.append(f"+{w.strip()}")
  for w in RISK:
    wl = w.lower()
    if wl in t:
      idx0 = t_orig.find(wl)
      prefix = t_orig[max(0, idx0 - 6):idx0] if idx0 >= 0 else ""
      if any(np in prefix for np in NEG_PREFIXES):
        s -= 1.0
        hits.append(f"~否定{w.strip()}")
      elif has_rescue and w.strip().lower() in DROP_WORDS:
        hits.append(f"~背景{w.strip()}")
      else:
        s += 2
        hits.append(f"+{w.strip()}")
  bearish_subj = any(bs in t_orig for bs in BEARISH_SUBJECTS)
  for w in RELIEF:
    wl = w.lower()
    if wl in t:
      if bearish_subj and w.strip().lower() in HIGH_WORDS:
        continue
      s -= 2
      hits.append(f"-{w.strip()}")
  if s:
    us = any(w in t_orig for w in US_POLICY)
    if any(w in t_orig for w in TAIWAN_MKT):
      s *= 1.5
    elif us and any(w in t_orig for w in CHINA):
      s *= 1.2  # US-China policy.
    elif us:
      s *= 0.8  # US policy toward the world.
    elif any(w in t_orig for w in CHINA):
      s *= 0.3  # China without a US / Taiwan link (e.g. China vs Norway).
    else:
      s *= 0.15  # Unrelated geopolitics.
    if any(w in t_orig for w in OPINION):
      s *= 0.4  # Commentary is not news.
  if s and any(w in t_orig for w in TRUMP):
    s *= 1.2
  return s, hits


def fetch_headlines(now: datetime, hours: int = 12) -> list[dict]:
  seen, out = set(), []
  for q, lang in QUERIES:
    loc = "hl=zh-TW&gl=TW&ceid=TW:zh-Hant" if lang == "zh" else "hl=en-US&gl=US&ceid=US:en"
    q_full = q if "when:" in q else f"{q} when:1d"
    url = f"https://news.google.com/rss/search?q={urllib.parse.quote(q_full)}&{loc}"
    try:
      xml = _get(url)
    except Exception:
      continue
    for title, link, pub in re.findall(r"<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>.*?<pubDate>(.*?)</pubDate>", xml, re.S):
      title = html.unescape(title).strip()
      key = re.sub(r"\W", "", title)[:24].lower()
      if key in seen:
        continue
      seen.add(key)
      try:
        ts = parsedate_to_datetime(pub).astimezone(TPE)
      except Exception:
        continue
      if now - ts > timedelta(hours=hours):
        continue
      s, hits = score_headline(title)
      if s == 0 and not hits:
        continue
      age_h = max(0.0, (now - ts).total_seconds() / 3600)
      out.append({"title": title, "link": html.unescape(link), "ts": ts.isoformat(timespec="minutes"),
                  "raw": s, "score": round(s * 0.5 ** (age_h / 3), 2), "hits": hits[:5]})
  return cluster_stories(out)  # Sorted by |score|, one entry per story.


def fomc_decision_days(page: str) -> list[date]:
  """Parses FOMC meeting dates (decision = last day of the meeting) from the Fed calendar page."""
  days = []
  for year_m in re.finditer(r"(\d{4}) FOMC Meetings", page):
    year = int(year_m.group(1))
    nxt = page.find("FOMC Meetings", year_m.end())
    block = page[year_m.end(): nxt if nxt > 0 else len(page)]
    for m, d in re.findall(r'fomc-meeting__month[^>]*>\s*<strong>([A-Za-z/]+)</strong>.*?fomc-meeting__date[^>]*>([\d\-–*]+)', block, re.S):
      month = m.split("/")[-1]
      day = re.findall(r"\d+", d)
      if month in MONTHS and day:
        try:
          days.append(date(year, MONTHS[month], int(day[-1])))
        except ValueError:
          pass
  return sorted(set(days))


def fomc_window(decision: date) -> tuple[datetime, datetime]:
  """FOMC statement at 14:00 New York time -> Taipei 02:00 (US DST) / 03:00 next day. Window = +/- 60 min."""
  from .calendar import us_dst
  hour = 2 if us_dst(decision) else 3
  t = datetime.combine(decision + timedelta(days=1), time(hour, 0), TPE)
  return t - timedelta(minutes=60), t + timedelta(minutes=60)


class NewsRadar:

  def __init__(self, ledger, interval_min: int = 10, notifier=None):
    self.ledger = ledger
    self.interval = timedelta(minutes=interval_min)
    self.notifier = notifier
    self.last = None
    self.fomc: list[date] = []
    self.fomc_loaded = None

  def _mode(self, delta: float | None) -> str:
    """Mode from the change versus the recent baseline (there is always some background war / tariff news)."""
    if delta is None:
      return "normal"
    if delta >= 15:
      return "high"
    if delta >= 8:
      return "elevated"
    if delta <= -8:
      return "positive"
    return "normal"

  def poll(self, now: datetime) -> dict | None:
    if self.last and now - self.last < self.interval:
      return None
    self.last = now
    if self.fomc_loaded != now.date():
      try:
        self.fomc = fomc_decision_days(_get(FED_URL))
        self.fomc_loaded = now.date()
      except Exception:
        pass
    heads = fetch_headlines(now)
    risk = sorted((h["score"] for h in heads if h["score"] > 0), reverse=True)[:10]
    relief = sorted((h["score"] for h in heads if h["score"] < 0))[:10]
    total = round(sum(risk) + sum(relief), 1)  # Top-10 each side: the number of articles cannot inflate it.
    hist = [x for x in (self.ledger.get_state("news_hist") or [])
            if now - datetime.fromisoformat(x[0]) <= timedelta(days=7)]
    hist.append([now.isoformat(timespec="seconds"), total])
    self.ledger.set_state("news_hist", hist[-1200:])
    older = sorted(v for ts_, v in hist if now - datetime.fromisoformat(ts_) >= timedelta(hours=2))
    baseline = older[len(older) // 2] if len(older) >= 12 else None  # Median; needs ~2 h of history.
    delta = round(total - baseline, 1) if baseline is not None else None
    mode = self._mode(delta)
    upcoming = [d for d in self.fomc if d >= now.date() - timedelta(days=1)][:3]
    in_fomc = any(a <= now <= b for a, b in (fomc_window(d) for d in upcoming))
    prev = self.ledger.get_state("news") or {}
    state = {"updated": now.isoformat(timespec="seconds"), "total": total, "baseline": baseline, "delta": delta,
             "warming_up": baseline is None, "mode": mode, "fomc_window": in_fomc,
             "upcoming_fomc": [str(d) for d in upcoming], "headlines": heads[:12]}
    self.ledger.set_state("news", state)
    if prev.get("mode") != mode:
      zh = {"high": "🔴 高風險", "elevated": "🟡 風險升高", "positive": "🟢 偏多消息", "normal": "⚪ 正常"}[mode]
      self.ledger.log(f"新聞風險模式：{zh}（分數 {total}，比基準 {delta:+}）" if delta is not None else f"新聞風險模式：{zh}（建立基準中）", level="WARN")
      if self.notifier and mode == "high" and prev:  # Only real spikes are pushed; the rest is on the dashboard.
        pos_heads = [h for h in heads if h["score"] > 0][:3]
        lines = "\n".join(f"・{h['title'][:60]}（{h['score']:+.1f}）" for h in pos_heads)
        self.notifier.urgent(f"新聞風險：{zh}（比平常高 {delta:+}）\n{lines}\n→ AI 照常依策略交易（新聞僅供風險提醒，不擋單）")
    return state
