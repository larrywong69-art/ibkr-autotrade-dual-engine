# AutoTrade 版本更新日誌 (Changelog)

所有重要變更、功能升級與安全修復皆會記錄於此檔案。  
其他使用者與雲端主機可透過 `./update.sh` 一鍵同步最新穩定版本（詳見 [README.md](README.md)）。

## [v9.3.13] - 2026-11-19

- 新增 Google Apps Script Webhook 雲端無人值守雙表直連推送通道（免 GCP 服務帳戶/免安裝 CLI）
- 支援遠端 Linux 雲端主機透過標準 HTTPS POST 秒級同步「帳戶結餘表」與「詳細交易紀錄分析總表」
- 隨附開箱即用之接收端代碼 `google_apps_script_sync.js` 與詳細 3 分鐘部署指引

## [v9.3.12] - 2026-11-18

- 支援 Google Sheets「V9.3 累積表現」雙表自動建立與排程同步（帳戶結餘表 + 交易紀錄分析總表含折線圖與逐筆成交流水）
- 每日清晨 06:00 結算自動追加最新淨值與成交，並整合 Telegram 推播一鍵直達

## [v9.3.11] - 2026-11-18

- 新增 ushmds 連續逾時自動重連自癒與每 15 分鐘 5m 策略掃描攔截原因日誌

## [v9.3.10] - 2026-11-18

- 處理 IBKR 代碼 10197 跨裝置行情搶佔警告節流、自動暫停盲目開倉及前端紅色狀態提示

## [v9.3.9] - 2026-11-18

- 修復 Telegram 推播遇 Connection reset 瞬斷未重試導致漏發 PWA 隧道網址問題

## [v9.3.8] - 2026-11-17

- 確保移動保本與動態追蹤修改 STP 單時強制 transmit=True 立即傳送至交易所

## [v9.3.7] - 2026-11-17

- 全面修復下單與開倉邏輯：分梯部分成交寬限期與子單過濾(Code 7008)、平倉市價單 outsideRth=True、K棒 UTC 時區對齊(formatDate=2)、雙標的統一保證金與真實匯率校驗、實盤 TP1 移保本與 TP2 動態追蹤止盈

## [v9.3.6] - 2026-11-17

- 修復 IBKR HMDS 歷史數據重複請求逾時導致 Error 162 連續刷屏問題（新增 5 分鐘週期去重、指數退避與 4H 失敗冷卻）

## [v9.3.5] - 2026-11-17

- 修復期貨止蝕單 outsideRth=False 盤外不觸發問題並新增軟體防穿倉緊急平倉保護 (Code 7007)

## [v9.3.4] - 2026-11-17

- 成交明細對應顯示開/平倉時間與跨日配對，並僅於主單顯示盈虧（不於次單重覆）

## [v9.3.3] - 2026-11-17

### 新增與優化 (Added & Changed)
- **一鍵版本發布與訂閱同步工作流 (`release.sh` / `update.sh` / `VERSION`)**
  - 新增 `VERSION` 版本號與 `release.sh`，自動化執行安全檢查、單元測試、乾淨版雲端壓縮包重建、`git tag` 推送與 GitHub Release 發布。
  - 新增 `update.sh` 供其他使用者與雲端 VM 一鍵檢查與同步更新；即使原本是透過壓縮檔部署、缺少 `.git` 目錄的環境，亦可自動轉換為 Git 追蹤並安全保留 `.env`、`data/`、`logs/` 與 `reports/`。
  - 更新 `cloud_deploy/setup.sh`，雲端新裝改以 Git 追蹤與安裝 `update.sh` 為主，避免後續更新遇到 `fatal: not a git repository`。
  - 後端新增 `GET /api/version`（支援 `?check=1` 檢查 GitHub 最新版本），儀表板頂部新增版本號與「檢查更新」按鈕。
- **日曆式每日盈虧記錄與統一 HKD 計價 (`dashboard.html`, `server.py`)**
  - 將「6:4 動態資金調度中樞」改為互動式 **📅 每日盈虧記錄 (HKD)**，支援月份切換、當月勝率/淨盈虧彙總與點擊單日展開 MES/MNQ 分策略及成交流水明細。
  - 雙盤浮動損益、已實現損益與成交流水簿統一以 HKD 計價並以 FIFO 合併開倉/平倉父子階層（淨損益已包含手續費）。
- **異常稽核通知數已讀歸零 (`report_and_incident_manager.py`, `server.py`, `dashboard.html`)**
  - 新增 `POST /api/incidents/ack` 與 `data/incidents_ack.json` 水位線，點開「連線與交易異常稽核日誌」後頂部通知數立即變回 `0`，直到出現新異常時再次累加提示。
- **雙層閉環巡檢非阻塞升級 (`server.py`, `dashboard.html`)**
  - `POST /api/audit/run` 改為背景執行並搭配 `GET /api/audit/status` 輪詢，修正透過 Cloudflare Tunnel / Safari 執行時發生逾時或 `JSON.parse` 模式錯誤。

## [v9.3.2] - 2026-11-16

### 新增與修復 (Added & Fixed)
- 新增每日雙層閉環 AI 自我稽核系統（`daily_auto_audit_fixer.py`）與 `git worktree` 隔離分支安全回歸驗證。
- 強化實盤五層掛單自癒（`7001`–`7006`）與跨 `ClientId` `reqAllOpenOrders()` 同步，修正 `2109`/`202`/`MARGIN` 稽核分類。
- 新增 Telegram 問題＋解決方案＋合併指令推播與 Google Drive (`rclone`) 診斷報告匯出。
