# IBKR AutoTrade 雙引擎量化交易與監控系統

本專案使用 Python 串接 Interactive Brokers (`IB Gateway` / `TWS`) 進行 CME 微型期貨（**MES** / **MNQ**）**雙策略自動交易**、**多週期連續回測**、**網頁與手機 PWA 即時監控**、**Telegram 警報推播**與**雲端 Docker 自動部署**。

> ⚠️ **安全與免責聲明**：本專案預設僅供 **Paper Trading（模擬交易）** 與策略研究使用。所有帳號、密碼、API Token 與個人設定均透過 `.env`（已被 `.gitignore` 排除）隔離管理，請勿將 `.env` 或任何真實憑證提交至公開儲存庫。

---

## 1. 核心架構與檔案總覽

| 模組 | 檔案 | 說明 |
| :--- | :--- | :--- |
| **雙引擎實盤/模擬主程式** | `live_ibkr_dual_engine.py` | 同時運行 **V4.3 (MES 60%)** 與 **V5.0 (MNQ 40%)** 雙策略引擎，支援自動重連與三梯次平倉 |
| **單策略實盤引擎** | `live_ibkr_trader_v4_3.py`, `live_ibkr_trader_v5.py`, `live_paper_trader_v3b.py` | V4.3 / V5.0 / V3B 獨立執行引擎 |
| **監控伺服器 & 儀表板** | `server.py`, `dashboard.html`, `mobile.html`, `manifest.json` | 桌面雙欄儀表板與手機 PWA 監控介面，支援 PIN 碼遠端控制開關與緊急平倉 |
| **安全與遠端穿透** | `tunnel_manager.py`, `change_pin.py` | Cloudflare Tunnel 公網加密穿透管理，與 `.env` 6 位數安全 PIN 碼修改工具 |
| **Telegram 通知與稽核** | `telegram_notifier.py`, `report_and_incident_manager.py`, `gdrive_report_manager.py` | 成交推播、定期心跳、低風險錯誤過濾（如代碼 `162`/`2219`）、每日結算報告與雲端匯出 |
| **策略核心與模型** | `mes_strategy/`, `strategies/`, `strategy_v5_champion.py` | 指標計算、策略引擎、V4.1 / V4.3 / V5.0 冠軍參數與模型定義 |
| **回測與圖表套件** | `backtest_v4_suite.py`, `run_continuous_5yr_backtest.py`, `run_v5_optimization_suite.py`, `run_v5_dual_champion_backtest.py`, `generate_clean_chart.py` | 5 年連續回測、V5 九合一優化矩陣與淨值比較圖表生成 |
| **基礎連線測試** | `01_test_connection.py`, `02_fetch_market_data.py`, `03_paper_order_test.py`, `04_mes_order_test.py`, `verify_cme_realtime.py` | 連線驗證、行情抓取與模擬下單生命週期測試 |
| **啟動與雲端部署** | `start_paper_trading.sh`, `start_v4_3_only.sh`, `cloud_deploy/setup.sh` | 本機一鍵啟動與 GCP/Linux Docker + IB Gateway + Tmux 全自動部署腳本 |

---

## 2. 環境需求與 IB Gateway / TWS 設定

- **Python 版本**：Python 3.10 以上
- **IBKR 交易客戶端**（擇一啟動）：
  - **IB Gateway**（推薦，輕量無龐大 GUI 負擔，Paper 預設 Port: `4002`）
  - **Trader Workstation (TWS)**（桌面版完整圖形介面，Paper 預設 Port: `7497`）

### IB Gateway / TWS 必備設定清單 (Checklist)

1. **登入模式**：務必使用 **Paper Trading** 模式（帳號通常為 `DU` 或 `DF` 開頭）。
2. **API 連線配置**（`Configure` -> `Settings` -> `API` -> `Settings`）：
   - [x] **Enable ActiveX and Socket Clients**（必須勾選）
   - **Socket port**：`4002`（IB Gateway Paper）或 `7497`（TWS Paper）
   - [ ] **Read-Only API**：若需自動下單與平倉測試，請**取消勾選**
   - **Trusted IPs**：新增 `127.0.0.1`

---

## 3. 快速安裝與啟動

```bash
# 1. 複製專案
git clone <YOUR_REPOSITORY_URL>
cd ibkr_paper_trading

# 2. 建立並啟用 Python 虛擬環境
python3 -m venv .venv
source .venv/bin/activate
# Windows: .venv\Scripts\activate

# 3. 安裝相依套件
pip install -r requirements.txt

# 4. 建立個人環境變數設定檔（請勿將 .env 提交至 Git）
cp .env.example .env
```

編輯 `.env` 設定您的安全參數：
- `WEB_ACCESS_PIN`：公網遠端操作（啟停策略 / 緊急平倉）的 6 位數 PIN 碼
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`：選填，用於接收成交通知與系統心跳
- `TELEGRAM_IGNORE_CODES`：預設過濾無害代碼（`162,326,2103...2219,10167,10168`）
- `IB_ACCOUNT_ID` / `GDRIVE_FOLDER_ID`：選填，自訂帳戶顯示標籤與雲端報告資料夾 ID

### 一鍵啟動雙引擎與監控儀表板

```bash
chmod +x start_paper_trading.sh
./start_paper_trading.sh
```

啟動後可於瀏覽器開啟：
- 桌面儀表板：`http://127.0.0.1:8080/dashboard.html`
- 手機 PWA 介面：`http://127.0.0.1:8080/mobile.html`

修改安全 PIN 碼：
```bash
python3 change_pin.py 123456
```

---

## 4. 基礎連線測試腳本

```bash
# 測試連線 IB Gateway (4002) 或 TWS (7497)
python3 01_test_connection.py
python3 01_test_connection.py 7497

# 抓取歷史 K 棒與即時盤口 (預設 AAPL，可帶入股票代碼)
python3 02_fetch_market_data.py NVDA

# 模擬下單與撤單測試 (股票 / MES 期貨)
python3 03_paper_order_test.py
python3 04_mes_order_test.py
```

---

## 5. 雲端 Docker 自動部署（Linux / GCP）

`cloud_deploy/setup.sh` 提供 Linux 主機環境一鍵配置（含 4GB Swap、Docker IB Gateway、noVNC、Cloudflare Tunnel 與 Tmux 背景守護）：

```bash
# 建議搭配環境變數設定 IB Gateway 登入憑證，勿寫死在腳本中
export TWS_USERID="your_paper_username"
export TWS_PASSWORD="your_paper_password"
export VNC_PASSWORD="your_strong_vnc_password"

bash cloud_deploy/setup.sh
```

---

## 6. 常見問題 (FAQ)

- **連線被拒絕 (`ConnectionRefusedError` / `Timeout`)**：確認 IB Gateway / TWS 已完成登入且 `Enable ActiveX and Socket Clients` 已勾選，並確認 Port (`4002` / `7497`) 一致。
- **Read-Only API 錯誤 (代碼 `321`)**：在 IB Gateway API 設定中取消勾選 `Read-Only API`。
- **程式碼 `162` / `2219` 警報**：屬無害的 HMDS 查詢取消或數據農場重連通知，預設已透過 `TELEGRAM_IGNORE_CODES` 過濾推播。
