# IBKR AutoTrade 雙引擎量化交易、自癒守護與雙層閉環稽核系統 (V9.3)

本專案使用 Python 串接 Interactive Brokers (`IB Gateway` / `TWS`) 進行 CME 微型期貨（**MES** / **MNQ**）**V9.3 旗艦雙引擎自動交易**、**實盤五層掛單自癒保護**、**每日雙層閉環 AI 巡檢與隔離分支修復**、**網頁 / 手機 PWA 即時監控**、**Google Drive (`rclone`) 與本機診斷報告匯出**，以及 **Telegram 即時成交與問題解決方案推播**。

> ⚠️ **安全與免責聲明**：本專案預設僅供 **Paper Trading（模擬交易）** 與策略研究使用。所有帳號、密碼、API Key 與個人設定均透過 `.env`（已被 `.gitignore` 排除）隔離管理，請勿將 `.env` 或任何真實憑證提交至公開儲存庫。

---

## 1. 最新版本亮點 (V9.3 Master Hybrid Live)

- **🏛️ V9.3 旗艦雙引擎實盤策略 (`live_ibkr_dual_engine.py`)**
  - **6:4 資金池配置與防飢餓保護**：MES 享有 60% 優先資金池，MNQ 享有 40% 防飢餓池；可用保證金吃緊時自動將 MNQ 降口或暫緩開倉，優先守住 MES 配額。
  - **4H_LOCK 宏觀趨勢濾網 + 0.82x ATR 緊密止損 + 三階梯獲利收割**，支援 100% 整數口分梯獨立 Bracket 下單。
- **🛡️ 實盤五層掛單自癒與安全護欄 (`7001`–`7006`)**
  - **跨 ClientId 掛單全同步**：採用 `reqAllOpenOrders()` 追蹤所有在途掛單（含其他 ClientId 與歷史遺留單），消除殘留單盲區。
  - **五層掛單清理與對齊**：自動偵測並撤銷反向衝突掛單 (`7002`)、空倉殘留出場單 (`7003`)、逾 20 分鐘未成交進場限價單 (`7004`)、超額對沖止損/止盈單 (`7005`)、未綁定 OCA 之冗餘止盈單 (`7006`)，並支援裸倉自動補掛止損/止盈。
  - **消除錯誤碼 `2109` 與精準保證金分類**：期貨單統一設定 `outsideRth=False` 避免無效屬性警告；將 `202` 無明確原因系統撤單降級為 `INFO`，並優先識別真實保證金不足（`MARGIN`）與分級 HMDS 退避重連。
- **🤖 每日雙層閉環自我稽核與安全修復 (`daily_auto_audit_fixer.py`)**
  - **第一層（確定性規則體檢）**：每日 **06:05–06:15 AM**（或於儀表板點擊 `🤖 雙層閉環巡檢`）自動稽核過去 24 小時成交、事件、掛單對齊與異常指標並計算健康評分。
  - **第二層（Gemini AI 根因分析 + 隔離分支安全修復）**：透過 `google.genai` 呼叫 Gemini API（預設 `gemini-4-argon`）診斷根因並產生建議補丁，於獨立的 **`git worktree` 隔離分支 (`autofix/...`)** 內套用修改，必須通過 `py_compile` 與 `tests/test_engine_safety.py` 完整回歸測試後才會保留分支，絕不直接污染 `main` 運行分支。
- **📲 Telegram 問題、解決方案與合併指令推播 (`telegram_notifier.py`)**
  - 除即時成交通知、定時心跳與過濾低風險代碼外，每日雙層閉環巡檢完成後會推播包含：**規則體檢發現的問題、AI 根因判讀、解決方案建議、`autofix/...` 隔離分支回歸測試結果、一鍵合併指令與雲端報告連結**（CLI 模式支援同步發送以防漏發）。
- **☁️ Google Drive (`rclone`) 整合與直接網頁下載 (`gdrive_report_manager.py`)**
  - 支援一鍵將最新成交與異常診斷報告（HTML / CSV）透過 `rclone` 上傳至 Google Drive，亦提供儀表板直接下載（`GET /api/diagnostics/download?format=html|csv`）。

---

## 2. 核心架構與檔案總覽

| 模組 | 檔案 | 說明 |
| :--- | :--- | :--- |
| **旗艦實盤守護主程式** | `live_ibkr_dual_engine.py` | 運行 V9.3 雙引擎策略（MES 60% + MNQ 40%）、跨 ClientId 掛單對齊與五層自癒守護 |
| **雙層閉環 AI 巡檢修復** | `daily_auto_audit_fixer.py` | 24H 確定性體檢、Gemini AI 根因分析與補丁生成、`git worktree` 隔離分支回歸測試 |
| **事件與排程管理** | `report_and_incident_manager.py` | 異常事件分級記錄（`MARGIN` / `2109` / `202` / 自癒代碼 `7001–7006`）與每日 06:05 AM 巡檢排程 |
| **Telegram 通知引擎** | `telegram_notifier.py` | 成交、異常、心跳與「每日稽核問題＋解決方案＋合併指令」Telegram 推播 |
| **雲端報告與診斷匯出** | `gdrive_report_manager.py` | HTML/CSV 成交與異常診斷報告生成、`rclone` 上傳 Google Drive 與本機匯出 |
| **監控伺服器 & 前端** | `server.py`, `dashboard.html`, `mobile.html`, `backtest_lab.html` | 桌面雙欄儀表板、手機 PWA 與回測實驗室；支援 PIN 碼啟停、一鍵平倉、診斷下載與雙層巡檢按鈕 |
| **安全與遠端穿透** | `tunnel_manager.py`, `change_pin.py` | Cloudflare Tunnel 公網加密穿透管理，與 `.env` 6 位數安全 PIN 碼設定工具 |
| **策略核心與回測套件** | `mes_strategy/`, `strategies/`, `backtest_engine.py`, `ibkr_history_downloader.py` | 策略模型、歷史行情下載與回測驗證工具 |
| **自動化回歸測試** | `tests/test_engine_safety.py` | 涵蓋實盤掛單清理、事件分類、雙層閉環體檢、隔離 worktree 補丁驗收與 Telegram 推播單元測試 |
| **啟動與雲端部署** | `start_cloud.sh`, `start_paper_trading.sh`, `start_backtest_lab.sh`, `cloud_deploy/setup.sh` | 雲端/本機一鍵啟動與 GCP Linux Docker + IB Gateway + `rclone` + Tmux 全自動部署腳本 |

---

## 3. 環境需求與 IB Gateway / TWS 設定

- **Python 版本**：Python 3.10 以上
- **選配系統工具**：`rclone`（若需同步上傳診斷報告至 Google Drive）、`git`（隔離分支修復必要）
- **IBKR 交易客戶端**（擇一啟動）：
  - **IB Gateway**（推薦，Paper 預設 Port: `4002`）
  - **Trader Workstation (TWS)**（Paper 預設 Port: `7497`）

### IB Gateway / TWS 必備設定清單 (Checklist)

1. **登入模式**：務必使用 **Paper Trading** 模式（帳號通常為 `DU` 或 `DF` 開頭）。
2. **API 連線配置**（`Configure` -> `Settings` -> `API` -> `Settings`）：
   - [x] **Enable ActiveX and Socket Clients**（必須勾選）
   - **Socket port**：`4002`（IB Gateway Paper）或 `7497`（TWS Paper）
   - [ ] **Read-Only API**：若需自動下單與自癒撤單，請**取消勾選**
   - **Trusted IPs**：新增 `127.0.0.1`

---

## 4. 快速安裝、環境變數與啟動

```bash
# 1. 複製專案並進入目錄
git clone https://github.com/larrywong69-art/ibkr-autotrade-dual-engine.git
cd ibkr-autotrade-dual-engine

# 2. 建立並啟用 Python 虛擬環境
python3 -m venv .venv
source .venv/bin/activate

# 3. 安裝相依套件
pip install -r requirements.txt

# 4. 建立個人環境變數設定檔（請勿將 .env 提交至 Git）
cp .env.example .env
```

編輯 `.env` 設定您的安全與整合參數：
- `WEB_ACCESS_PIN`：公網遠端操作（啟停策略 / 緊急平倉）的 6 位數 PIN 碼
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`：接收成交、異常警報與每日 AI 巡檢問題＋解決方案推播
- `TELEGRAM_IGNORE_CODES`：預設過濾無害代碼（如 `162,326,2103...2219,10167,10168`）
- `GEMINI_API_KEY` / `GEMINI_AUDIT_MODEL`：啟用雙層閉環 Gemini AI 根因分析與修復建議（預設模型 `gemini-4-argon`）
- `RCLONE_GDRIVE_REMOTE` / `GDRIVE_FOLDER_ID`：設定 `rclone` 遠端名稱（預設 `gdrive`）與 Google Drive 目標資料夾 ID
- `IB_ACCOUNT_ID` / `ENABLE_MARKET_SCHEDULE`：自訂帳戶標籤與 CME 自動開休市排程（預設 `true`）

### 本機與雲端啟動 / 更新

```bash
# 本機啟動雙引擎與監控儀表板
chmod +x start_paper_trading.sh
./start_paper_trading.sh

# 雲端 VM 拉取最新 main 分支並重啟服務
cd ~/ibkr_paper_trading && git fetch && git reset --hard origin/main && ./start_cloud.sh
```

啟動後可於瀏覽器開啟：
- 桌面儀表板：`http://127.0.0.1:8080/dashboard.html`
- 手機 PWA 介面：`http://127.0.0.1:8080/mobile.html`
- 回測實驗室：`http://127.0.0.1:8080/backtest_lab.html`

修改安全 PIN 碼：
```bash
python3 change_pin.py 123456
```

---

## 5. 雙層閉環巡檢與單元測試指令

```bash
# 手動執行一次雙層閉環巡檢（含規則體檢 + Gemini AI 分析 + 隔離分支測試 + 同步 Telegram 推播）
.venv/bin/python3 daily_auto_audit_fixer.py

# 執行完整單元測試驗證實盤安全護欄、事件分類、Worktree 修復與 Telegram 通知格式
.venv/bin/python3 -m unittest discover -s tests -p "test_*.py" -v

# 測試 Telegram 推播連通性
.venv/bin/python3 telegram_notifier.py
```

---

## 6. 雲端 Docker 自動部署（Linux / GCP）

`cloud_deploy/setup.sh` 提供 Linux 主機環境一鍵配置（含 Swap、Docker IB Gateway、noVNC、Cloudflare Tunnel 與 Tmux 背景守護）：

```bash
export TWS_USERID="your_paper_username"
export TWS_PASSWORD="your_paper_password"
export VNC_PASSWORD="your_strong_vnc_password"

bash cloud_deploy/setup.sh
```

---

## 7. 常見問題 (FAQ)

- **執行 `git fetch` 出現 `fatal: not a git repository`**：請先確認已 `cd ~/ibkr_paper_trading` 進入專案目錄再執行更新與重啟。
- **連線被拒絕 (`ConnectionRefusedError` / `Timeout`)**：確認 IB Gateway / TWS 已完成登入且 `Enable ActiveX and Socket Clients` 已勾選，並確認 Port (`4002` / `7497`) 一致。
- **Read-Only API 錯誤 (代碼 `321`)**：在 IB Gateway API 設定中取消勾選 `Read-Only API`。
- **程式碼 `162` / `2109` / `202` 疑問**：`162` 與數據農場訊息預設已透過 `TELEGRAM_IGNORE_CODES` 過濾；期貨單已統一 `outsideRth=False` 消除 `2109`；無明確錯誤原因的 `202` 系統撤單已降級為 `INFO`，不再誤報為嚴重異常。
