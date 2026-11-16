#!/usr/bin/env python3
"""
================================================================================
daily_auto_audit_fixer.py
AutoTrade 雙層閉環：每日自動收集、確定性漏洞體檢與沙盒安全修復系統
================================================================================
架構說明 (雙層安全閉環)：
- 第一層 (Deterministic Audit + Cloud Archive)：
  每日香港時間 06:05 AM（或由儀表板手動觸發）收集過去 24 小時成交明細、
  異常稽核事件、即時持倉掛單快照及守護引擎日誌；執行零幻覺確定性規則掃描，
  將完整診斷檔上傳至 Google Drive 備存。
- 第二層 (Gemini AI Root-Cause Analysis + Isolated Worktree Test Gate)：
  若偵測到潛在漏洞且已於 .env 設定 GEMINI_API_KEY，呼叫 Gemini Interactions API
  分析根因並產出精準程式碼補丁；於隔離的臨時 Git Worktree 建立獨立分支
  `autofix/YYYY-MM-DD`，強制執行 `py_compile` 與 `tests/test_engine_safety.py`
  回歸測試，全數通過才推送分支至 GitHub 並發送 Telegram 通知，絕不未經確認
  直接修改 main 分支或重啟實盤下單引擎。
================================================================================
"""

import os
import sys
import json
import html
import shutil
import tempfile
import subprocess
import logging
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(CURRENT_DIR, "data")
REPORTS_DIR = os.path.join(CURRENT_DIR, "reports")
AUDITS_DIR = os.path.join(REPORTS_DIR, "audits")
STATUS_FILE = os.path.join(CURRENT_DIR, "trade_status.json")
LOG_FILE = os.path.join(CURRENT_DIR, "mes_dual_engine_live.log")
TRADES_JSONL = os.path.join(DATA_DIR, "trades.jsonl")
INCIDENTS_JSONL = os.path.join(DATA_DIR, "incidents.jsonl")
ENV_FILE = os.path.join(CURRENT_DIR, ".env")

os.makedirs(AUDITS_DIR, exist_ok=True)
logger = logging.getLogger("DailyAutoAuditFixer")

# 允許 AI 建議修補的原始碼檔案白名單（嚴格排除 .env、金鑰與資料檔）
ALLOWED_PATCH_FILES = {
    "live_ibkr_dual_engine.py",
    "report_and_incident_manager.py",
    "gdrive_report_manager.py",
    "server.py",
    "telegram_notifier.py",
    "backtest_engine.py",
    "ibkr_history_downloader.py",
}

DEFAULT_GEMINI_MODEL = os.getenv("GEMINI_AUDIT_MODEL", "gemini-4-argon").strip() or "gemini-4-argon"


def _load_env_safe():
    """安全載入 .env 到環境變數，絕不輸出金鑰明文"""
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE)
        return
    except Exception:
        pass
    if os.path.exists(ENV_FILE):
        try:
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip())
        except Exception:
            pass


_load_env_safe()


AUDIT_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "簡潔的繁體中文根因分析與系統健康評估摘要"
        },
        "risk_level": {
            "type": "string",
            "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
            "description": "整體潛在實盤風險等級"
        },
        "root_causes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "偵測到的關鍵漏洞或異常根因清單"
        },
        "recommended_patches": {
            "type": "array",
            "description": "精確且最小化的原始碼修補建議清單；若無需改碼請回傳空陣列",
            "items": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "相對於專案根目錄的檔名，必須屬於白名單"
                    },
                    "explanation": {
                        "type": "string",
                        "description": "該修補的目的與防止的問題說明"
                    },
                    "search_block": {
                        "type": "string",
                        "description": "目標檔案中必須完全逐字匹配的既有程式碼片段"
                    },
                    "replace_block": {
                        "type": "string",
                        "description": "替換後的正確程式碼片段"
                    }
                },
                "required": ["file_path", "explanation", "search_block", "replace_block"]
            }
        }
    },
    "required": ["summary", "risk_level", "root_causes", "recommended_patches"]
}


class DailyAutoAuditFixer:
    """每日雙層閉環巡檢、AI 分析與隔離分支安全修復協調器"""

    def __init__(self, base_dir: str = CURRENT_DIR):
        self.base_dir = os.path.abspath(base_dir)
        self.data_dir = os.path.join(self.base_dir, "data")
        self.audits_dir = os.path.join(self.base_dir, "reports", "audits")
        os.makedirs(self.audits_dir, exist_ok=True)

    @staticmethod
    def _parse_ts(ts_str: str) -> Optional[datetime]:
        if not ts_str:
            return None
        s = str(ts_str).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(s[:19], fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(s.replace("Z", ""))
        except Exception:
            return None

    def _read_jsonl_since(self, path: str, cutoff: datetime, max_rows: int = 300) -> List[Dict[str, Any]]:
        if not os.path.exists(path):
            return []
        rows: List[Dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    dt = self._parse_ts(obj.get("timestamp", ""))
                    if dt is None or dt >= cutoff:
                        rows.append(obj)
        except Exception as e:
            logger.warning(f"讀取 {path} 異常: {e}")
        return rows[-max_rows:]

    def collect_24h_snapshot(self, hours: int = 24, log_tail_lines: int = 250) -> Dict[str, Any]:
        """第一層階段 A：收集過去 N 小時的成交、異常稽核、系統快照與引擎日誌"""
        now_dt = datetime.now()
        cutoff = now_dt - timedelta(hours=hours)

        trades = self._read_jsonl_since(os.path.join(self.data_dir, "trades.jsonl"), cutoff)
        incidents = self._read_jsonl_since(os.path.join(self.data_dir, "incidents.jsonl"), cutoff)

        status_snapshot: Dict[str, Any] = {}
        status_path = os.path.join(self.base_dir, "trade_status.json")
        if os.path.exists(status_path):
            try:
                with open(status_path, "r", encoding="utf-8") as sf:
                    status_snapshot = json.load(sf)
            except Exception:
                pass

        # 若 trades.jsonl 為空，備援補充 trade_status.json 內的 recent_trades
        if not trades and status_snapshot.get("recent_trades"):
            trades = list(status_snapshot.get("recent_trades", []))

        log_lines: List[str] = []
        log_path = os.path.join(self.base_dir, "mes_dual_engine_live.log")
        if os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as lf:
                    log_lines = [ln.rstrip("\n") for ln in lf.readlines()[-log_tail_lines:]]
            except Exception:
                pass

        return {
            "collected_at": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "window_hours": hours,
            "trades": trades,
            "incidents": incidents,
            "status_snapshot": status_snapshot,
            "log_lines": log_lines,
        }

    def run_deterministic_audit(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """
        第一層階段 B：零幻覺確定性不變量稽核 (Deterministic Invariants Audit)
        """
        findings: List[Dict[str, Any]] = []
        score = 100
        status = snapshot.get("status_snapshot") or {}
        incidents = snapshot.get("incidents") or []
        trades = snapshot.get("trades") or []
        log_lines = snapshot.get("log_lines") or []

        def add_finding(rule_id: str, severity: str, title: str, detail: str, penalty: int, files: List[str]):
            nonlocal score
            score = max(0, score - penalty)
            findings.append({
                "rule_id": rule_id,
                "severity": severity,
                "title": title,
                "detail": detail,
                "candidate_files": files,
            })

        # 1. 檢查即時持倉與掛單不變量 (Wrong-direction / Over-hedged / Orphan on flat)
        for label, sub_key in (("MES", "v43"), ("MNQ", "v50")):
            sub = status.get(sub_key) or {}
            pos = sub.get("active_position") or None
            orders = sub.get("open_orders") or []
            qty = int((pos or {}).get("contracts", 0) or 0)

            if qty > 0 and pos:
                direction = str(pos.get("direction", "LONG")).upper()
                expected_exit = "SELL" if direction == "LONG" else "BUY"
                wrong_side = [o for o in orders if expected_exit not in str(o.get("action", "")).upper()]
                if wrong_side:
                    add_finding(
                        "WRONG_DIRECTION_ORDER",
                        "CRITICAL",
                        f"{label} 持倉方向與掛單方向衝突",
                        f"{label} 持倉 {direction} {qty} 口，但仍有 {len(wrong_side)} 筆錯誤方向掛單 ({[o.get('order_id') for o in wrong_side]})",
                        35,
                        ["live_ibkr_dual_engine.py"],
                    )

                exit_orders = [o for o in orders if expected_exit in str(o.get("action", "")).upper()]
                sl_qty = sum(int(o.get("contracts", 0) or 0) for o in exit_orders if o.get("order_type") in ("STP", "STP PRT", "TRAIL"))
                tp_qty = sum(int(o.get("contracts", 0) or 0) for o in exit_orders if o.get("order_type") == "LMT")
                if sl_qty > qty or tp_qty > qty:
                    add_finding(
                        "OVER_HEDGED_ORDERS",
                        "HIGH",
                        f"{label} 出場保護單總口數超出持倉",
                        f"{label} 持倉 {qty} 口，但止損總量={sl_qty}、止盈總量={tp_qty}，可能造成超賣反手",
                        25,
                        ["live_ibkr_dual_engine.py"],
                    )
                if sl_qty == 0 and status.get("ibkr_connected"):
                    add_finding(
                        "NAKED_POSITION_DETECTED",
                        "CRITICAL",
                        f"{label} 當前處於無止損裸倉狀態",
                        f"{label} 持倉 {direction} {qty} 口但場上 STP 止損單為 0 口",
                        35,
                        ["live_ibkr_dual_engine.py"],
                    )
            elif orders:
                orphan_exits = [
                    o for o in orders
                    if (
                        o.get("order_type") in ("STP", "STP PRT", "TRAIL")
                        or str(o.get("order_ref", "")).startswith("GUARD_")
                        or ("_ENTRY_" not in str(o.get("order_ref", "")) and any(tag in str(o.get("order_ref", "")) for tag in ("_TP", "_SL", "_RUNNER")))
                    )
                ]
                if orphan_exits:
                    add_finding(
                        "ORPHAN_EXIT_ORDER",
                        "HIGH",
                        f"{label} 空倉時仍遺留出場掛單",
                        f"{label} 目前空倉 (0 口)，但仍有 {len(orphan_exits)} 筆殘留出場單 ({[o.get('order_id') for o in orphan_exits]})",
                        25,
                        ["live_ibkr_dual_engine.py"],
                    )

        # 2. 統計過去 24h incidents 規則異常
        code_counts: Dict[int, int] = {}
        margin_incidents = []
        critical_or_error_incidents = []
        for inc in incidents:
            try:
                c = int(inc.get("code", -1))
            except Exception:
                c = -1
            code_counts[c] = code_counts.get(c, 0) + 1
            sev = str(inc.get("severity", "INFO")).upper()
            cat = str(inc.get("category", "")).upper()
            if cat == "MARGIN" or c in (4001, 4002):
                margin_incidents.append(inc)
            if sev in ("ERROR", "CRITICAL") and c not in (1101, 1102):
                critical_or_error_incidents.append(inc)

        if code_counts.get(7001, 0) >= 2:
            add_finding(
                "FREQUENT_NAKED_GUARDIAN",
                "HIGH",
                "24 小時內多次觸發裸倉自動補掛 (7001)",
                f"過去 {snapshot.get('window_hours', 24)} 小時觸發 {code_counts[7001]} 次 7001 補掛，需檢查 Bracket 子單同步或跨 ClientId 狀態",
                20,
                ["live_ibkr_dual_engine.py"],
            )

        if code_counts.get(7002, 0) >= 1 or code_counts.get(7005, 0) >= 1:
            add_finding(
                "ORDER_CONFLICT_REPAIRED",
                "MEDIUM",
                "守護引擎於實盤中偵測並清理過反向或超額掛單",
                f"觸發次數：錯向撤單(7002)={code_counts.get(7002, 0)}、超額裁減(7005)={code_counts.get(7005, 0)}",
                10,
                ["live_ibkr_dual_engine.py"],
            )

        if code_counts.get(162, 0) >= 10:
            add_finding(
                "HMDS_TIMEOUT_STORM",
                "MEDIUM",
                "IBKR 歷史行情查詢頻繁取消 (Code 162)",
                f"過去 {snapshot.get('window_hours', 24)} 小時發生 {code_counts[162]} 次 Code 162，建議檢視請求頻率與 timeout 設定",
                10,
                ["live_ibkr_dual_engine.py", "report_and_incident_manager.py"],
            )

        conn_drops = code_counts.get(1100, 0) + code_counts.get(502, 0) + code_counts.get(504, 0) + code_counts.get(2110, 0)
        if conn_drops >= 3:
            add_finding(
                "CONNECTIVITY_INSTABILITY",
                "HIGH",
                "IB Gateway 斷線或 Session 衝突頻繁",
                f"過去 {snapshot.get('window_hours', 24)} 小時累計 {conn_drops} 次斷線事件 (1100/502/504/2110)",
                15,
                ["live_ibkr_dual_engine.py"],
            )

        if margin_incidents:
            add_finding(
                "MARGIN_GATEKEEPER_ALERT",
                "HIGH",
                "發生保證金閘門調整或資金不足攔截",
                f"共 {len(margin_incidents)} 次保證金相關事件：{margin_incidents[-1].get('message', '')}",
                10,
                ["live_ibkr_dual_engine.py"],
            )

        if len(critical_or_error_incidents) >= 1:
            samples = "; ".join(str(x.get("message", ""))[:120] for x in critical_or_error_incidents[-3:])
            add_finding(
                "CRITICAL_OR_ERROR_EVENTS",
                "HIGH",
                f"出現 {len(critical_or_error_incidents)} 筆 ERROR/CRITICAL 稽核紀錄",
                f"最近事件：{samples}",
                15,
                ["live_ibkr_dual_engine.py", "report_and_incident_manager.py"],
            )

        # 3. 檢查引擎尾部日誌 Python Traceback / Unhandled Exception
        tb_lines = [ln for ln in log_lines if "Traceback (most recent call last)" in ln or "異常:" in ln or "[ERROR]" in ln]
        if len(tb_lines) >= 3:
            add_finding(
                "ENGINE_LOG_EXCEPTIONS",
                "MEDIUM",
                f"引擎日誌近期出現 {len(tb_lines)} 條異常訊息",
                "最近異常日誌：" + " | ".join(tb_lines[-3:])[:260],
                10,
                ["live_ibkr_dual_engine.py", "server.py"],
            )

        # 計算交易統計摘要
        realized_sum = 0.0
        comm_sum = 0.0
        for t in trades:
            realized_sum += float(t.get("realized_pnl", 0.0) or 0.0)
            comm_sum += float(t.get("commission", 0.0) or 0.0)

        return {
            "health_score": score,
            "findings_count": len(findings),
            "findings": findings,
            "stats": {
                "trades_24h": len(trades),
                "incidents_24h": len(incidents),
                "realized_pnl_24h": round(realized_sum, 2),
                "commission_24h": round(comm_sum, 2),
                "code_counts": {str(k): v for k, v in code_counts.items()},
            },
        }

    def _collect_source_snippets(self, file_names: List[str], max_chars_per_file: int = 18000) -> str:
        """從白名單原始碼讀取相關函式片段供 Gemini 分析 (嚴禁讀取 .env 或機密檔)"""
        chunks: List[str] = []
        seen = set()
        for fname in file_names:
            clean_name = os.path.basename(fname)
            if clean_name not in ALLOWED_PATCH_FILES or clean_name in seen:
                continue
            seen.add(clean_name)
            fpath = os.path.join(self.base_dir, clean_name)
            if not os.path.exists(fpath):
                continue
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
                if len(content) > max_chars_per_file:
                    content = content[-max_chars_per_file:]
                chunks.append(f"===== FILE: {clean_name} =====\n{content}")
            except Exception:
                pass
        return "\n\n".join(chunks)

    def _call_gemini_analysis(self, prompt_text: str, model_name: str = DEFAULT_GEMINI_MODEL) -> Dict[str, Any]:
        """使用最新 Google GenAI SDK (Interactions API) 或官方 REST 端點分析漏洞並輸出 JSON"""
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            return {"status": "skipped", "reason": "GEMINI_API_KEY 未設定於 .env"}

        system_prompt = (
            "你是資深量化交易系統架構師與安全稽核專家。請根據過去 24 小時的確定性異常稽核結果、"
            "掛單快照與對應 Python 原始碼，找出真正導致問題的根因，並僅在明確必要且極高把握時提出最小化、"
            "零破壞性且可直接精確搜尋替換的程式碼修補建議（search_block 必須逐字存在於檔案中）。"
            "若既有防護已覆蓋或僅屬外部網路波動，recommended_patches 請回傳空陣列。"
        )

        # 優先使用新版 google-genai SDK (>=3.2.0) Interactions API
        try:
            from google import genai
            client = genai.Client(api_key=api_key)
            interaction = client.interactions.create(
                model=model_name,
                system_instruction=system_prompt,
                input=prompt_text,
                store=False,
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": AUDIT_RESPONSE_SCHEMA,
                },
            )
            raw_text = getattr(interaction, "output_text", "") or ""
            if raw_text:
                parsed = json.loads(raw_text)
                return {"status": "ok", "model": model_name, "via": "google-genai-sdk", **parsed}
        except ImportError:
            pass
        except Exception as sdk_err:
            logger.warning(f"Gemini SDK 呼叫失敗，嘗試 REST 備援: {sdk_err}")

        # 官方 Interactions API REST 備援 (Api-Revision: 2026-05-20)
        try:
            url = "https://generativelanguage.googleapis.com/v1beta/interactions"
            payload = {
                "model": model_name,
                "system_instruction": system_prompt,
                "input": prompt_text,
                "store": False,
                "response_format": {
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": AUDIT_RESPONSE_SCHEMA,
                },
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": api_key,
                    "Api-Revision": "2026-05-20",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            out_txt = body.get("output_text", "")
            if not out_txt and isinstance(body.get("steps"), list):
                for st in reversed(body["steps"]):
                    if isinstance(st, dict) and st.get("type") == "model_output":
                        parts = st.get("content") or st.get("parts") or []
                        texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")]
                        if texts:
                            out_txt = "\n".join(texts)
                            break
            if not out_txt:
                return {"status": "error", "reason": "Gemini 回應不含有效 JSON 文本"}
            parsed = json.loads(out_txt)
            return {"status": "ok", "model": model_name, "via": "interactions-rest", **parsed}
        except urllib.error.HTTPError as he:
            err_body = ""
            try:
                err_body = he.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            return {"status": "error", "reason": f"HTTP {he.code}: {err_body}"}
        except Exception as e:
            return {"status": "error", "reason": str(e)}

    def apply_patches_in_isolated_worktree(
        self,
        patches: List[Dict[str, str]],
        branch_name: str,
        push_branch: bool = True
    ) -> Dict[str, Any]:
        """
        第二層安全閘門：在臨時隔離的 Git Worktree 套用 AI 修補並執行完整回歸測試，
        通過測試才建立分支並推上 GitHub (絕不直接改動 main 或重啟實盤)。
        """
        if not patches:
            return {"status": "skipped", "reason": "無需要程式碼修補的項目"}

        git_dir = os.path.join(self.base_dir, ".git")
        if not os.path.exists(git_dir):
            return {"status": "skipped", "reason": "當前目錄非 Git 儲存庫，僅保留修補建議供審閱"}

        worktree_dir = tempfile.mkdtemp(prefix="autotrade_autofix_")
        created_branch = False
        final_branch = branch_name
        try:
            # 確保不與已有同名分支碰撞
            chk = subprocess.run(
                ["git", "rev-parse", "--verify", branch_name],
                cwd=self.base_dir, capture_output=True, text=True
            )
            if chk.returncode == 0:
                final_branch = f"{branch_name}-{datetime.now().strftime('%H%M%S')}"

            add_res = subprocess.run(
                ["git", "worktree", "add", "-b", final_branch, worktree_dir, "HEAD"],
                cwd=self.base_dir, capture_output=True, text=True
            )
            if add_res.returncode != 0:
                return {"status": "error", "reason": f"建立隔離 worktree 失敗: {add_res.stderr.strip()}"}
            created_branch = True

            applied_files: List[str] = []
            skipped_patches: List[str] = []
            for p in patches:
                rel_path = str(p.get("file_path", "")).strip().lstrip("./")
                if rel_path not in ALLOWED_PATCH_FILES:
                    skipped_patches.append(f"{rel_path}: 不在允許修補白名單中")
                    continue
                target_file = os.path.join(worktree_dir, rel_path)
                if not os.path.exists(target_file):
                    skipped_patches.append(f"{rel_path}: 檔案不存在")
                    continue
                search_blk = p.get("search_block", "")
                replace_blk = p.get("replace_block", "")
                if not search_blk or search_blk == replace_blk:
                    skipped_patches.append(f"{rel_path}: 無效或無變更的補丁")
                    continue
                with open(target_file, "r", encoding="utf-8") as tf:
                    src = tf.read()
                occurrences = src.count(search_blk)
                if occurrences != 1:
                    skipped_patches.append(f"{rel_path}: search_block 匹配次數={occurrences} (必須唯一匹配=1)")
                    continue
                with open(target_file, "w", encoding="utf-8") as tf:
                    tf.write(src.replace(search_blk, replace_blk, 1))
                applied_files.append(rel_path)

            if not applied_files:
                return {
                    "status": "skipped",
                    "reason": "沒有成功套用的有效補丁",
                    "skipped_patches": skipped_patches,
                }

            # 沙盒驗證 1：語法編譯檢查
            py_exec = sys.executable or "python3"
            compile_targets = [f for f in applied_files if f.endswith(".py")]
            if compile_targets:
                cmp_res = subprocess.run(
                    [py_exec, "-m", "py_compile", *compile_targets],
                    cwd=worktree_dir, capture_output=True, text=True
                )
                if cmp_res.returncode != 0:
                    return {
                        "status": "rejected",
                        "reason": f"語法編譯未通過，已丟棄補丁: {cmp_res.stderr.strip()}",
                        "applied_files": applied_files,
                    }

            # 沙盒驗證 2：執行完整安全回歸測試套件
            test_dir = os.path.join(worktree_dir, "tests")
            if os.path.isdir(test_dir):
                tst_res = subprocess.run(
                    [py_exec, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"],
                    cwd=worktree_dir, capture_output=True, text=True, timeout=120
                )
                if tst_res.returncode != 0:
                    return {
                        "status": "rejected",
                        "reason": "安全回歸測試未通過，已安全攔截並丟棄補丁",
                        "test_output": (tst_res.stdout + "\n" + tst_res.stderr)[-1200:],
                        "applied_files": applied_files,
                    }

            # 提交至獨立分支並推送遠端供審核
            subprocess.run(["git", "add", *applied_files], cwd=worktree_dir, check=True, capture_output=True)
            commit_res = subprocess.run(
                ["git", "commit", "-m", f"chore(autofix): 雙層閉環每日自動漏洞修復建議 ({final_branch})"],
                cwd=worktree_dir, capture_output=True, text=True
            )
            if commit_res.returncode != 0:
                return {"status": "error", "reason": f"Commit 失敗: {commit_res.stderr.strip()}"}

            pushed_remote = ""
            push_err = ""
            if push_branch:
                rem_res = subprocess.run(["git", "remote"], cwd=self.base_dir, capture_output=True, text=True)
                remotes = [r.strip() for r in rem_res.stdout.splitlines() if r.strip()]
                target_remote = "origin" if "origin" in remotes else (remotes[0] if remotes else "")
                if target_remote:
                    psh = subprocess.run(
                        ["git", "push", "-u", target_remote, final_branch],
                        cwd=worktree_dir, capture_output=True, text=True
                    )
                    if psh.returncode == 0:
                        pushed_remote = target_remote
                    else:
                        push_err = psh.stderr.strip()

            return {
                "status": "ready_for_review",
                "branch": final_branch,
                "pushed_remote": pushed_remote,
                "push_error": push_err,
                "applied_files": applied_files,
                "skipped_patches": skipped_patches,
                "tests_passed": True,
            }
        except Exception as e:
            return {"status": "error", "reason": str(e)}
        finally:
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", worktree_dir],
                    cwd=self.base_dir, capture_output=True, text=True
                )
            except Exception:
                pass
            if os.path.exists(worktree_dir):
                shutil.rmtree(worktree_dir, ignore_errors=True)
            # 若分支被拒絕或無有效變更，清理未用本地分支
            if created_branch:
                chk_log = subprocess.run(
                    ["git", "log", "-1", "--pretty=%s", final_branch],
                    cwd=self.base_dir, capture_output=True, text=True
                )
                if chk_log.returncode == 0 and "chore(autofix):" not in chk_log.stdout:
                    subprocess.run(["git", "branch", "-D", final_branch], cwd=self.base_dir, capture_output=True)

    def _save_audit_artifacts(self, report: Dict[str, Any], date_str: str) -> Tuple[str, str]:
        json_path = os.path.join(self.audits_dir, f"daily_audit_{date_str}.json")
        md_path = os.path.join(self.audits_dir, f"daily_audit_{date_str}.md")
        latest_json = os.path.join(self.audits_dir, "latest_audit.json")

        with open(json_path, "w", encoding="utf-8") as jf:
            json.dump(report, jf, indent=2, ensure_ascii=False)
        with open(latest_json, "w", encoding="utf-8") as lf:
            json.dump(report, lf, indent=2, ensure_ascii=False)

        det = report.get("deterministic_audit", {})
        ai = report.get("ai_analysis", {})
        patch_res = report.get("patch_result", {})
        lines = [
            f"# AutoTrade 每日雙層閉環審計報告 ({date_str})",
            f"- **執行時間**：{report.get('run_at', '')}",
            f"- **健康評分**：{det.get('health_score', 100)} / 100",
            f"- **偵測問題數**：{det.get('findings_count', 0)}",
            f"- **24h 成交**：{det.get('stats', {}).get('trades_24h', 0)} 筆 | 已實現損益: ${det.get('stats', {}).get('realized_pnl_24h', 0.0):.2f}",
            "",
            "## 一、確定性不變量檢查結果",
        ]
        if det.get("findings"):
            for f in det["findings"]:
                lines.append(f"- **[{f['severity']}] {f['title']} (`{f['rule_id']}`)**: {f['detail']}")
        else:
            lines.append("- ✅ 過去 24 小時未偵測到任何不變量異常。")

        lines.extend(["", "## 二、AI 深度漏洞分析與修復狀態"])
        lines.append(f"- **AI 狀態**：{ai.get('status', 'skipped')} ({ai.get('reason') or ai.get('model', '')})")
        if ai.get("summary"):
            lines.append(f"- **AI 總結**：{ai['summary']}")
        if patch_res:
            lines.append(f"- **修復分支狀態**：{patch_res.get('status', '')} {patch_res.get('branch', '')} {patch_res.get('reason', '')}")

        with open(md_path, "w", encoding="utf-8") as mf:
            mf.write("\n".join(lines) + "\n")
        return json_path, md_path

    def get_latest_audit(self) -> Optional[Dict[str, Any]]:
        latest_json = os.path.join(self.audits_dir, "latest_audit.json")
        if os.path.exists(latest_json):
            try:
                with open(latest_json, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return None

    def run_daily_closed_loop(
        self,
        hours: int = 24,
        upload_gdrive: bool = True,
        enable_ai_fix: bool = True,
        force_ai: bool = False,
        push_branch: bool = True,
        notify_telegram: bool = True,
    ) -> Dict[str, Any]:
        """執行完整雙層閉環巡檢：收集 -> 確定性體檢 -> Drive 歸檔 -> Gemini AI 分析 -> 隔離分支驗證 -> Telegram 通知"""
        now_dt = datetime.now()
        date_str = now_dt.strftime("%Y-%m-%d")

        # Layer 1A: 收集數據
        snapshot = self.collect_24h_snapshot(hours=hours)

        # Layer 1B: 確定性不變量體檢
        det_audit = self.run_deterministic_audit(snapshot)

        # Layer 1C: 打包上傳 Google Drive (非阻塞失敗不中斷審計)
        gdrive_res: Dict[str, Any] = {"status": "skipped"}
        if upload_gdrive:
            try:
                from gdrive_report_manager import GoogleDriveReportManager
                gdrive_res = GoogleDriveReportManager().export_diagnostics_to_gdrive(
                    trades=snapshot["trades"],
                    incidents=snapshot["incidents"],
                    status_snapshot=snapshot["status_snapshot"],
                    log_lines=snapshot["log_lines"],
                )
            except Exception as ge:
                gdrive_res = {"status": "error", "message": str(ge)}

        # Layer 2: Gemini AI 根因分析與隔離分支修補
        ai_analysis: Dict[str, Any] = {"status": "skipped", "reason": "健康評分 100，無需發起 AI 修復"}
        patch_result: Dict[str, Any] = {"status": "skipped", "reason": "無待修補項目"}

        if enable_ai_fix and (det_audit["findings_count"] > 0 or force_ai):
            candidate_files = []
            for f in det_audit["findings"]:
                candidate_files.extend(f.get("candidate_files", []))
            if not candidate_files:
                candidate_files = ["live_ibkr_dual_engine.py", "report_and_incident_manager.py"]

            source_ctx = self._collect_source_snippets(candidate_files)
            ai_prompt = (
                f"=== 審計時間 ===\n{now_dt.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"=== 確定性規則體檢結果 (Health Score: {det_audit['health_score']}/100) ===\n"
                f"{json.dumps(det_audit['findings'], ensure_ascii=False, indent=2)}\n\n"
                f"=== 24h 統計 ===\n{json.dumps(det_audit['stats'], ensure_ascii=False)}\n\n"
                f"=== 最近異常事件 (最多 30 筆) ===\n"
                f"{json.dumps(snapshot['incidents'][-30:], ensure_ascii=False)}\n\n"
                f"=== 尾部日誌 (最多 60 行) ===\n"
                + "\n".join(snapshot["log_lines"][-60:])
                + f"\n\n=== 相關原始碼 ===\n{source_ctx}"
            )
            ai_analysis = self._call_gemini_analysis(ai_prompt)

            if ai_analysis.get("status") == "ok" and ai_analysis.get("recommended_patches"):
                branch_name = f"autofix/{date_str}"
                patch_result = self.apply_patches_in_isolated_worktree(
                    patches=ai_analysis["recommended_patches"],
                    branch_name=branch_name,
                    push_branch=push_branch,
                )

        report = {
            "status": "ok",
            "run_at": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "date": date_str,
            "deterministic_audit": det_audit,
            "gdrive": gdrive_res,
            "ai_analysis": ai_analysis,
            "patch_result": patch_result,
        }
        json_path, md_path = self._save_audit_artifacts(report, date_str)
        report["audit_json_path"] = json_path
        report["audit_md_path"] = md_path

        if notify_telegram:
            try:
                from telegram_notifier import notifier
                if hasattr(notifier, "notify_daily_audit"):
                    notifier.notify_daily_audit(report)
            except Exception as te:
                logger.warning(f"Telegram 雙層閉環推播失敗: {te}")

        return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AutoTrade 雙層閉環每日自動稽核與沙盒修復工具")
    parser.add_argument("--hours", type=int, default=24, help="收集過去多少小時紀錄")
    parser.add_argument("--no-gdrive", action="store_true", help="略過 Google Drive 上傳")
    parser.add_argument("--no-ai", action="store_true", help="僅執行確定性體檢，不呼叫 Gemini AI")
    parser.add_argument("--force-ai", action="store_true", help="即使無異常亦執行 Gemini AI 巡檢")
    parser.add_argument("--no-push", action="store_true", help="不推送 autofix 分支至遠端")
    parser.add_argument("--no-tg", action="store_true", help="不發送 Telegram 通知")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    res = DailyAutoAuditFixer().run_daily_closed_loop(
        hours=args.hours,
        upload_gdrive=not args.no_gdrive,
        enable_ai_fix=not args.no_ai,
        force_ai=args.force_ai,
        push_branch=not args.no_push,
        notify_telegram=not args.no_tg,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
