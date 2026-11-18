/**
 * ==============================================================================
 * AutoTrade V9.3 Google Sheets 自動同步接收端 (Google Apps Script Web App)
 * ==============================================================================
 * 
 * 🚀 功能說明：
 * 本腳本部署在 Google 試算表中，作為專屬的接收端 Webhook。
 * 讓運行在雲端主機（如 GCP / AWS / VPS）的 AutoTrade 交易機器人，
 * 在無需配置複雜 GCP 服務帳戶與 CLI 工具的情況下，
 * 每日清晨或手動觸發時，透過標準 HTTPS POST 秒級同步「帳戶結餘表」與「詳細成交流水總表」！
 * 
 * 📋 3 分鐘簡易部署指引：
 * 1. 在瀏覽器打開「AutoTrade_交易紀錄與分析總表」或任意 Google 試算表：
 *    https://docs.google.com/spreadsheets/d/139yDV3lOdbF5fK4C00MQYh4MHG-gscBMx_f0RVu853c/edit
 * 2. 點擊頂部選單【擴充功能 (Extensions)】->【Apps Script】。
 * 3. 清空編輯器內所有內容，貼上本檔案全部代碼，點擊上方【儲存 (Ctrl+S / Cmd+S)】。
 * 4. 點擊右上角藍色【部署 (Deploy)】按鈕 -> 選擇【新部署 (New deployment)】。
 * 5. 點擊左側齒輪圖示，選擇【網頁應用程式 (Web app)】：
 *    - 說明 (Description)：AutoTrade Sheets Sync
 *    - 執行身分 (Execute as)：我 (您的 Google 帳號)
 *    - 誰可以存取 (Who has access)：所有人 (Anyone)  <-- 確保雲端主機能正常 POST
 * 6. 點擊【部署】，系統會彈出權限審查，點擊【審查授權 (Review Permissions)】：
 *    - 選擇您的 Google 帳戶
 *    - 若出現「Google 尚未驗證這個應用程式」，點擊左下角【進階 (Advanced)】->【前往 AutoTrade Sheets Sync (不安全)】
 *    - 點擊【允許 (Allow)】
 * 7. 部署完成後，複製獲得的【網頁應用程式網址 (Web app URL)】：
 *    格式如：https://script.google.com/macros/s/AKfycby.../exec
 * 8. 在雲端主機上的 ~/ibkr_paper_trading/.env 檔案中加入此變數：
 *    GOOGLE_SHEETS_WEBHOOK_URL=https://script.google.com/macros/s/AKfycby.../exec
 * 
 * ✨ 完成！之後每日 06:00 雲端主機即會自動將最新成交流水與結餘推送至 Google Sheets！
 */

function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return ContentService.createTextOutput(JSON.stringify({
        status: "error",
        message: "未收到任何 POST 資料 (No post data received)"
      })).setMimeType(ContentService.MimeType.JSON);
    }

    var payload = JSON.parse(e.postData.contents);
    var balanceId = payload.balance_sheet_id || "1aVh_vFGwxrcXckr4LNh-AWxwGzzav1KuY7uXCLqpFXI";
    var ledgerId = payload.trade_ledger_sheet_id || "139yDV3lOdbF5fK4C00MQYh4MHG-gscBMx_f0RVu853c";

    var balanceCount = 0;
    var tradeCount = 0;

    // =========================================================================
    // 1. 更新【AutoTrade_帳戶結餘表】
    // =========================================================================
    if (payload.balances && payload.balances.length > 0) {
      var bApp = SpreadsheetApp.openById(balanceId);
      var bSheet = bApp.getSheetByName("帳戶結餘與每日盈虧") || bApp.getSheets()[0];

      // 清除第 4 行以後的歷史數據以覆蓋更新
      var lastRow = bSheet.getLastRow();
      if (lastRow > 3) {
        bSheet.getRange(4, 1, lastRow - 3, 13).clearContent();
      }

      var bRows = [];
      for (var i = 0; i < payload.balances.length; i++) {
        var b = payload.balances[i];
        bRows.push([
          b.date || "",
          Number(b.start_equity) || 0,
          Number(b.realized_pnl) || 0,
          Number(b.net_pnl) || 0,
          Number(b.cumulative_net_pnl) || 0,
          Number(b.end_equity) || 0,
          (Number(b.daily_return_pct) || 0) / 100,
          (Number(b.cum_return_pct) || 0) / 100,
          Number(b.mes_trades) || 0,
          Number(b.mnq_trades) || 0,
          Number(b.commission) || 0,
          (Number(b.max_drawdown_pct) || 0) / 100,
          b.status || "NORMAL"
        ]);
      }

      if (bRows.length > 0) {
        var bRange = bSheet.getRange(4, 1, bRows.length, 13);
        bRange.setValues(bRows);
        balanceCount = bRows.length;

        // 格式化數字與貨幣格式
        bSheet.getRange(4, 2, bRows.length, 5).setNumberFormat("$#,##0.00;[Red]($#,##0.00);\"$0.00\"");
        bSheet.getRange(4, 7, bRows.length, 2).setNumberFormat("0.00%");
        bSheet.getRange(4, 9, bRows.length, 2).setNumberFormat("#,##0");
        bSheet.getRange(4, 11, bRows.length, 1).setNumberFormat("$#,##0.00");
        bSheet.getRange(4, 12, bRows.length, 1).setNumberFormat("0.00%");
      }
    }

    // =========================================================================
    // 2. 更新【AutoTrade_交易紀錄與分析總表】
    // =========================================================================
    if (payload.trades && payload.trades.length > 0) {
      var lApp = SpreadsheetApp.openById(ledgerId);

      // (1) 更新分頁二【詳細交易紀錄】
      var lSheet = lApp.getSheetByName("詳細交易紀錄") || (lApp.getSheets().length > 1 ? lApp.getSheets()[1] : null);
      if (lSheet) {
        var lastLRow = lSheet.getLastRow();
        if (lastLRow > 3) {
          lSheet.getRange(4, 1, lastLRow - 3, 12).clearContent();
        }

        var tRows = [];
        for (var j = 0; j < payload.trades.length; j++) {
          var t = payload.trades[j];
          var side = t.side || "";
          var action = t.action || "";
          if (!side && action) {
            side = (t.direction && t.direction.indexOf("LONG") !== -1) ? "BOT" : "SLD";
          }
          var sideText = (side === "BOT") ? "買入 (BOT)" : (side === "SLD" ? "賣出 (SLD)" : side);

          tRows.push([
            t.timestamp || "",
            t.symbol || "MES",
            t.strategy || "V4.3",
            sideText,
            action || (Number(t.net_pnl || 0) !== 0 ? "平倉" : "開倉"),
            Number(t.shares || t.contracts || 1),
            Number(t.price || t.entry_price || t.exit_price || 0),
            Number(t.realized_pnl || 0),
            Number(t.commission || 0),
            Number(t.net_pnl || 0),
            t.exec_id || t.trade_id || "",
            t.reason || t.order_ref || ""
          ]);
        }

        if (tRows.length > 0) {
          var tRange = lSheet.getRange(4, 1, tRows.length, 12);
          tRange.setValues(tRows);
          tradeCount = tRows.length;

          // 格式化數字欄位
          lSheet.getRange(4, 6, tRows.length, 1).setNumberFormat("#,##0");
          lSheet.getRange(4, 7, tRows.length, 1).setNumberFormat("$#,##0.00");
          lSheet.getRange(4, 8, tRows.length, 1).setNumberFormat("$#,##0.00;[Red]($#,##0.00);\"$0.00\"");
          lSheet.getRange(4, 9, tRows.length, 1).setNumberFormat("$#,##0.00");
          lSheet.getRange(4, 10, tRows.length, 1).setNumberFormat("$#,##0.00;[Red]($#,##0.00);\"$0.00\"");
        }
      }

      // (2) 更新分頁一【分析報告】KPI 卡片
      var rSheet = lApp.getSheetByName("分析報告") || lApp.getSheets()[0];
      if (rSheet && payload.stats) {
        var s = payload.stats;
        rSheet.getRange("A5").setValue(s.total_trades + " 筆");
        rSheet.getRange("C5").setValue(Number(s.win_rate_pct).toFixed(1) + "%");
        rSheet.getRange("E5").setValue("$" + (Number(s.total_net_pnl) >= 0 ? "+" : "") + Number(s.total_net_pnl).toFixed(2));
        rSheet.getRange("G5").setValue(Number(s.profit_factor).toFixed(2));

        rSheet.getRange("A8").setValue("$" + (Number(s.avg_trade) >= 0 ? "+" : "") + Number(s.avg_trade).toFixed(2));
        rSheet.getRange("C8").setValue("$" + (Number(s.max_win) >= 0 ? "+" : "") + Number(s.max_win).toFixed(2));
        rSheet.getRange("E8").setValue("$" + (Number(s.max_loss) >= 0 ? "+" : "") + Number(s.max_loss).toFixed(2));
        rSheet.getRange("G8").setValue("$" + Number(s.total_comm).toFixed(2));

        // 更新更新時間副標題 (A2)
        rSheet.getRange("A2").setValue("雙策略分配：V4.3 MES (60%) + V5.0 MNQ (40%) | 統計更新：" + Utilities.formatDate(new Date(), "Asia/Hong_Kong", "yyyy-MM-dd HH:mm:ss"));
      }
    }

    return ContentService.createTextOutput(JSON.stringify({
      status: "ok",
      balances_synced: balanceCount,
      trades_synced: tradeCount,
      synced_at: Utilities.formatDate(new Date(), "Asia/Hong_Kong", "yyyy-MM-dd HH:mm:ss")
    })).setMimeType(ContentService.MimeType.JSON);

  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({
      status: "error",
      error: err.toString()
    })).setMimeType(ContentService.MimeType.JSON);
  }
}

function doGet(e) {
  return ContentService.createTextOutput(JSON.stringify({
    status: "ok",
    message: "AutoTrade Google Sheets Webhook is active and running."
  })).setMimeType(ContentService.MimeType.JSON);
}
