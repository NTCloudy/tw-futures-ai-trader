"""Questions the user must answer, all at once, before any real-money (sinopac_live) trading.

User instruction 2026-11-24: 「你把所有上真倉的問題，到時候上之前要一次問我」. Every open question found while
running on paper is appended here. The engine refuses to start in sinopac_live until config sets
live_checklist_ack to LIVE_CHECKLIST_VERSION, i.e. after the user has answered this version of the list.
Bump the version whenever an item is added or changed.
"""

LIVE_CHECKLIST_VERSION = "2026-11-24"

ITEMS = [
    ("哪個 AI 上真倉",
     "建議只上 Omega（broker_ai=omega-swing），其他 AI 照舊模擬。當沖 AI（Sigma/Nova/Alpha）對急行情滑價很敏感"
     "（回測壓測：Sigma 一年 −28%～−68%、Nova −23%～−48%，Omega −5%～−10%），要等真實滑價資料再評估。"),
    ("資金與保證金",
     "建議放 10 萬（最少 8 萬）。微台原始保證金 35,050／維持 26,900（期交所 2026/08/12 公告）；"
     "永豐可能加收，上線前在 App 確認。最壞情況（停損成交不了、被拉到漲跌停）單口約虧 4 萬，10 萬不會被追繳。"),
    ("緊急停止的範圍",
     "現在任一 AI 的平倉市價單逾時，就會觸發全域緊急停止：所有 AI 平倉（含 Omega 正在賺的波段單）、"
     "之後要手動解除。要維持「全部停」，還是改成「只停出事的那個 AI」？"),
    ("Omega 本身的風險確認",
     "獲利集中（一年前 5 筆大單約占一半）、1,200 點停損上限短期會被洗出場（例：11/19）、"
     "隔夜／週末跳空擋不住（例：11/18）、Omega 不受每日虧損上限限制（波段單可留倉）。"),
    ("進行中試驗的結論",
     "上線當時，受保護出場不算停損（protected_not_stop）、Omega 提早保本影子（be05）、"
     "Alpha 停損上限 200 試行等，有結果的要先決定採不採用，再上線。"),
    ("真倉通知",
     "目前 LINE 只推 Omega 進場訊號；真倉時要不要加推「成交、停損移動、出場」通知？"),
    ("帳戶與連線檢查",
     "API 金鑰只綁期貨帳戶（程式已強制）、CA 憑證、python3 -m futures_ai sinopac-test 試連、"
     "上線第一天比對永豐成交與系統紀錄逐筆一致；機器重開機後確認部位有接回。"),
    ("漲跌停／價格穩定措施",
     "鎖漲跌停或委託超出動態價格範圍被退單時，程式會每次輪詢重試平倉直到成交；可以接受嗎？"),
]


def checklist_text() -> str:
  lines = [f"上真倉前確認清單（版本 {LIVE_CHECKLIST_VERSION}）"]
  for i, (title, body) in enumerate(ITEMS, 1):
    lines.append(f"{i}. {title}：{body}")
  return "\n".join(lines)
