"""Questions the user must answer, all at once, before any real-money (sinopac_live) trading.

User instruction 2026-11-24: 「你把所有上真倉的問題，到時候上之前要一次問我」. Every open question found while
running on paper is appended here. The engine refuses to start in sinopac_live until config sets
live_checklist_ack to LIVE_CHECKLIST_VERSION, i.e. after the user has answered this version of the list.
Bump the version whenever an item is added or changed.
"""

LIVE_CHECKLIST_VERSION = "2026-11-28-c"

ITEMS = [
    ("哪個 AI 上真倉",
     "建議只上 Omega（broker_ai=omega-swing），其他 AI 照舊模擬。當沖 AI（Sigma/Nova/Alpha）對急行情滑價很敏感"
     "（回測壓測：Sigma 一年 −28%～−68%、Nova −23%～−48%，Omega −5%～−10%），要等真實滑價資料再評估。"),
    ("資金與保證金",
     "建議放 10 萬（最少 8 萬）。微台原始保證金 35,050／維持 26,900（期交所 2026/08/12 公告）；"
     "永豐可能加收，上線前在 App 確認。最壞情況（停損成交不了、被拉到漲跌停）單口約虧「當時指數 × 10% × 10 元／點」（指數 40,000 時約 4 萬），放 10 萬不會被追繳。"),
    ("緊急停止的範圍",
     "現在任一 AI 的平倉市價單逾時，就會觸發全域緊急停止：所有 AI 平倉（含 Omega 正在賺的波段單）、"
     "之後要手動解除。要維持「全部停」，還是改成「只停出事的那個 AI」？"),
    ("Omega 本身的風險確認",
     "獲利集中（一年前 5 筆大單約占一半）、1,200 點停損上限短期會被洗出場（例：11/19）、"
     "隔夜／週末跳空擋不住（例：11/18）、Omega 不受每日虧損上限限制（波段單可留倉）。"),
    ("進行中試驗的結論",
     "受保護出場不算停損（protected_not_stop）已於 11/28 上線；上線當時，Omega 提早保本影子（be05）、Omega 停損上限影子、"
     "Alpha 停損上限 200 試行等，有結果的要先決定採不採用，再上線。"),
    ("真倉通知",
     "目前 LINE 只推 Omega 進場訊號；真倉時要不要加推「成交、停損移動、出場」通知？"),
    ("帳戶與連線檢查",
     "API 金鑰只綁期貨帳戶（程式已強制）、CA 憑證、python3 -m futures_ai sinopac-test 試連、"
     "上線第一天比對永豐成交與系統紀錄逐筆一致；機器重開機後確認部位有接回。"),
    ("漲跌停／價格穩定措施",
     "鎖漲跌停或委託超出動態價格範圍被退單時，程式會每次輪詢重試平倉直到成交；可以接受嗎？"),
    ("停機時部位沒有保護",
     "停損是引擎盯價後才送市價單，不是掛在交易所的條件單；引擎或機器一停，波段部位就沒有停損"
     "（已發生過：11/17 機器被重開、11/13 引擎停 12 分鐘、11/16 磁碟滿）。要 (a) 改用券商端停損／觸價單"
     "（先確認永豐對微台支援的單別），還是 (b) 接受風險、靠 watchdog 5 分鐘內自動重啟？"),
    ("執行環境",
     "引擎目前跑在公司的 Cloudtop：會被公司排程重開，而且用公司機器跑個人真錢交易可能不符合公司使用規範。"
     "請先確認，或改用個人電腦／VPS。"),
    ("換月",
     "Omega 會跨月持有；真倉換月是「平舊月＋開新月」兩筆真實委託，有成本與滑價，也可能只成交一邊。"
     "上線前確認換月流程，第一次換月人工盯盤。"),
    ("實測校正",
     "上真倉後同時跑一個 Omega 模擬對照組，比較真倉與模擬的成交價和滑價；累積 30 筆後更新回測用的滑價"
     "（sim_sl_slip_pts）與手續費（commission_per_side_twd 目前是估計值，用第一張對帳單更新）。"),
    ("鎖漲跌停後第一盤的突破",
     "前一盤鎖漲跌停時，「前 10 根高／低點」就是漲跌停價（人為上限），隔一盤解除後一站上就算突破。"
     "11/24 實例：Omega 追多虧 −11,240。樣本只有 1 次，無法驗證是延續還是反轉；目前照常交易。"
     "真倉時要照常，還是遇到這種情況先手動暫停？（同時在找更多歷史鎖漲跌停的例子研究）"),
    ("Omega 單筆最大可承受虧損",
     "Omega 移動停損＝4×ATR，目前另設上限 1,200 點（單筆約 −1.2 萬）。這個上限上線前沒有正確驗證過，"
     "正在用「不設上限」影子對照（12/31 或滿 10 筆決定）。不設上限時，ATR 660 會是 2,640 點，約 −2.6 萬再加跳空"
     "（一年回測最差一筆：上限 1,200 −12,380／不設上限 −15,282）。請決定你能接受的單筆最大虧損"
     "（config omega_max_loss_twd），影子結論也要在這個範圍內才會改。"
     "【11/28 已回答：維持現在＝上限 1,200 點、單筆約 −1.2 萬，不含跳空；隔夜或週末跳空時單筆可能超過"
     "（例：11/09 −16,610）。影子比較用「不比實盤最差一筆差」（omega_max_loss_twd=same_as_live）；"
     "上真倉前請再確認或改成固定金額】"),
]


def checklist_text() -> str:
  lines = [f"上真倉前確認清單（版本 {LIVE_CHECKLIST_VERSION}）"]
  for i, (title, body) in enumerate(ITEMS, 1):
    lines.append(f"{i}. {title}：{body}")
  return "\n".join(lines)
