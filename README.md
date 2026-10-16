# 📈 TW Futures AI Trader — 微台指 AI 自動交易

兩位 AI 操盤手即時交易**微型臺指期貨（TMF）**，一進場就掛好停利／停損觸價單（OCO），每盤收盤前全數平倉。

- **線上儀表板**：https://ntcloudy.github.io/tw-futures-ai-trader/ （每分鐘更新：今日下單狀況、今日／累計／近 1、3、6 小時收益、每小時損益、持倉與觸價單）
- **目前模式**：`local_paper`，也就是本機模擬：用 TAIFEX 即時行情，在本機模擬成交，不需要任何帳戶。

## 三種模式（改 `config/local.json` 的 `mode` 切換）

| mode | 行情 | 成交 | 需要 |
| --- | --- | --- | --- |
| `local_paper` | TAIFEX 即時（mis.taifex.com.tw） | 本機模擬：以委賣／委買價 ± 1 tick 成交 | 不需要帳戶 |
| `sinopac_sim` | TAIFEX 即時 | 永豐 Shioaji **模擬帳戶** | `pip install shioaji`、API Key |
| `sinopac_live` | TAIFEX 即時 | 永豐**真實帳戶** | 再加上憑證，以及 `i_understand_real_money: true` |

在 `sinopac_*` 模式下，只有 `broker_ai` 指定的那位 AI 會下單到永豐帳戶；另一位 AI 繼續用本機模擬。原因是同一個期貨帳戶的多空部位會互相抵銷，兩位 AI 共用一個帳戶會打架。

## 交易規則

- **商品**：微型臺指 TMF，每點 NT$10，每次 1 口。
- **資金**：每位 AI 的資金是 **1.5 × 原始保證金**（NT$35,050 × 1.5 = NT$52,575）。保證金調整時，請更新 `initial_margin_twd`。
- **停利／停損觸價單**：進場一成交，就同時掛好停利和停損兩張觸價單。**任一張觸發，系統會先刪除所有未成交委託，再用市價（IOC）平倉**，然後等待下一次進場。
  - Shioaji 沒有交易所端的期貨停損單，所以觸價單由程式盯盤執行。程式必須一直開著才會生效。
- **每日虧損上限**：當日已實現加未實現虧損達 NT$2,000，就立刻平倉，當天不再進場。
- **收盤**：收盤前 20 分鐘不再進場，收盤前 5 分鐘強制平倉，不留倉過夜。
- **換倉**：自動計算結算日（每月第三個星期三）。從結算日前 1 個營業日起改做次月合約；如果還持有舊月合約，會自動平倉（`ROLL`）。
- **緊急停止**：執行 `python3 -m futures_ai kill on`，或按本機儀表板上的按鈕，會立刻平倉並停止下單。

## 兩位 AI

| AI | 策略 |
| --- | --- |
| **Alpha 開盤突破 AI** | 開盤 15 分鐘區間突破，並用 VWAP 確認方向。停損是區間寬度的一半（30–80 點），停利是停損的 2 倍。每盤最多進場 2 次 |
| **Sigma VWAP 回歸 AI** | 價格偏離 VWAP 超過 2.5 倍 ATR，且出現反轉 K 棒時，逆勢進場。停損是 1.5 倍 ATR（25–70 點）。每盤最多進場 4 次 |

## 指令

```bash
cp config/example.json config/local.json
python3 -m unittest discover -s tests      # 單元測試
python3 -m futures_ai run --publish        # 啟動引擎（並每分鐘發布到線上儀表板）
scripts/start.sh                           # 背景執行
python3 -m futures_ai status               # 終端機查看今日狀態
python3 -m futures_ai dashboard            # 本機儀表板 http://127.0.0.1:8766（含緊急停止鍵）
python3 -m futures_ai sinopac-test         # 永豐模擬環境：登入 + 期貨下單測試
```

## 搬到自己的電腦執行（不需要 Cloudtop）

1. **停掉舊機器的引擎**：在舊機器執行 `bash scripts/stop.sh`。兩台同時跑會互相覆蓋線上儀表板。
2. **在新電腦下載專案**：`gh repo clone NTCloudy/tw-futures-ai-trader && cd tw-futures-ai-trader`。
3. **還原交易紀錄和 AI 學習資料**：執行 `python -m futures_ai restore`。舊機器每 10 分鐘會自動備份一次到 GitHub 的 `live-state` 分支。
4. **一次性設定**：
   - Windows：`powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1`
   - macOS／Linux：`bash scripts/setup_unix.sh`
   - 設定腳本會安裝需要的工具、建立 GitHub 權杖檔（讓線上儀表板能更新），並設定開機自動執行，以及每 5 分鐘檢查一次、沒在跑就自動重啟。
5. **把電腦設成不會自動睡眠**。睡眠期間不會交易。
6. **要停止時**：Windows 執行 `scripts\stop_windows.ps1`，macOS／Linux 執行 `bash scripts/stop.sh`。

## 永豐帳戶設定（之後切換用）

需要設定的環境變數：`SINOPAC_API_KEY`、`SINOPAC_SECRET_KEY`。真實帳戶另外需要 `SINOPAC_CA_PASSWORD`、`SINOPAC_PERSON_ID`，並把憑證放在 `certs/Sinopac.pfx`。這些資料都不會進 git。
