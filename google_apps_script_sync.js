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
 * 🎨 V9.3 特色功能更新：
 * 1. 支援 Exit Reason（10大出場原因：TP1, TP2, Runner, Breakeven, Trailing, Stop Loss, Soft Stop, EOD, Manual, Dynamic Exit）維度統計。
 * 2. 嚴格依照用戶範例截圖排版（「🎨 規則進場原因與時段表現」4 欄結構：分析維度 | 成交筆數 | 勝率 | 累計貢獻淨利）。
 * 3. 數據可視化增強：自動建立原生柱狀圖與分佈圖。
 * 4. 全面以港幣 (HKD) 計價與展示（以 7.78 基準匯率轉換損益與佣金）。
 * 
 * 📋 3 分鐘簡易部署指引：
 * 1. 在瀏覽器打開「AutoTrade_交易紀錄與分析總表」或任意 Google 試算表：
 *    https://docs.google.com/spreadsheets/d/139yDV3lOdbF5fK4C00MQYh4MHG-gscBMx_f0RVu853c/edit
 * 2. 點擊頂部選單【擴充功能 (Extensions)】->【Apps Script】。
 * 3. 清空編輯器內所有內容，貼上本檔案全部代碼，點擊上方【儲存 (Ctrl+S / Cmd+S)】。
 * 4. 點擊右上角藍色【部署 (Deploy)】按鈕 -> 選擇【管理部署 (Manage deployments)】或【新部署 (New deployment)】：
 *    - 說明 (Description)：AutoTrade Sheets Sync V9.3 (HKD & Exit Reason)
 *    - 執行身分 (Execute as)：我 (您的 Google 帳號)
 *    - 誰可以存取 (Who has access)：所有人 (Anyone)  <-- 確保雲端主機能正常 POST
 * 5. 點擊【部署】，複製獲得的【網頁應用程式網址 (Web app URL)】：
 *    格式如：https://script.google.com/macros/s/AKfycb.../exec
 * 6. 在雲端主機上的 ~/ibkr_paper_trading/.env 檔案中確認此變數：
 *    GOOGLE_SHEETS_WEBHOOK_URL=https://script.google.com/macros/s/AKfycb.../exec
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
    var fxRate = Number(payload.fx_rate) || 7.78;

    var balanceCount = 0;
    var tradeCount = 0;

    // =========================================================================
    // 1. 更新【AutoTrade_帳戶結餘表】 (14 欄位，全面 HKD 計價)
    // =========================================================================
    if (payload.balances && payload.balances.length > 0) {
      var bApp = SpreadsheetApp.openById(balanceId);
      var bSheet = bApp.getSheetByName("帳戶結餘與每日盈虧") || bApp.getSheets()[0];

      // 清除第 4 行以後的歷史數據以覆蓋更新
      var lastRow = bSheet.getLastRow();
      if (lastRow > 3) {
        bSheet.getRange(4, 1, lastRow - 3, 14).clearContent();
      }

      var bRows = [];
      var runningCumPnl = 0.0;
      var baseEquity = Number(payload.balances[0].start_equity) || 0;

      for (var i = 0; i < payload.balances.length; i++) {
        var b = payload.balances[i];
        var dPnl = Number(b.daily_pnl) || 0;
        runningCumPnl += dPnl;
        var startEq = Number(b.start_equity) || 0;
        var endEq = Number(b.end_equity) || 0;
        var dRet = (startEq > 0) ? (dPnl / startEq) : 0.0;
        var cRet = (baseEquity > 0) ? (runningCumPnl / baseEquity) : 0.0;

        bRows.push([
          b.date || "",
          b.account_id || "DUR340589",
          b.currency || "HKD",
          startEq,
          endEq,
          dPnl,
          dRet,
          runningCumPnl,
          cRet,
          Number(b.mes_pnl) || 0,
          Number(b.mnq_pnl) || 0,
          Number(b.commission) || 0,
          Number(b.drawdown || 0),
          b.status || "🟢 正常運行"
        ]);
      }

      if (bRows.length > 0) {
        var bRange = bSheet.getRange(4, 1, bRows.length, 14);
        bRange.setValues(bRows);
        balanceCount = bRows.length;

        // 格式化數字與 HKD 貨幣格式
        bSheet.getRange(4, 4, bRows.length, 3).setNumberFormat("HK$#,##0.00;[Red](HK$#,##0.00);\"HK$0.00\"");
        bSheet.getRange(4, 7, bRows.length, 1).setNumberFormat("0.00%");
        bSheet.getRange(4, 8, bRows.length, 1).setNumberFormat("HK$#,##0.00;[Red](HK$#,##0.00);\"HK$0.00\"");
        bSheet.getRange(4, 9, bRows.length, 1).setNumberFormat("0.00%");
        bSheet.getRange(4, 10, bRows.length, 3).setNumberFormat("HK$#,##0.00;[Red](HK$#,##0.00);\"HK$0.00\"");
        bSheet.getRange(4, 13, bRows.length, 1).setNumberFormat("0.00%");
      }
    }

    // =========================================================================
    // 2. 更新【AutoTrade_交易紀錄與分析總表】
    // =========================================================================
    if (payload.trades && payload.trades.length > 0) {
      var lApp = SpreadsheetApp.openById(ledgerId);

      // -----------------------------------------------------------------------
      // (1) 更新分頁二【詳細交易紀錄】 (14 欄位，含 Exit Reason 及 HKD 損益)
      // -----------------------------------------------------------------------
      var lSheet = lApp.getSheetByName("詳細交易紀錄") || (lApp.getSheets().length > 1 ? lApp.getSheets()[1] : null);
      if (lSheet) {
        var lastLRow = lSheet.getLastRow();
        if (lastLRow > 3) {
          lSheet.getRange(4, 1, lastLRow - 3, 14).clearContent();
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

          var grossHkd = Number(t.gross_pnl_hkd !== undefined ? t.gross_pnl_hkd : (Number(t.realized_pnl || 0) * fxRate));
          var commHkd = Number(t.commission_hkd !== undefined ? t.commission_hkd : (Number(t.commission || 0) * fxRate));
          var netHkd = Number(t.net_pnl_hkd !== undefined ? t.net_pnl_hkd : (Number(t.net_pnl || 0) * fxRate));

          tRows.push([
            t.timestamp || "",
            t.symbol || "MES",
            t.strategy || "V4.3",
            sideText,
            action || (Number(t.net_pnl || 0) !== 0 ? "平倉" : "開倉"),
            Number(t.shares || t.contracts || 1),
            Number(t.price || t.entry_price || t.exit_price || 0),
            grossHkd,
            commHkd,
            netHkd,
            t.exec_id || t.trade_id || "",
            t.exit_reason || "進場開倉 (Entry)",
            t.session || "🕒 美盤 RTH (09:30-16:00 ET)",
            t.reason || t.order_ref || ""
          ]);
        }

        if (tRows.length > 0) {
          var tRange = lSheet.getRange(4, 1, tRows.length, 14);
          tRange.setValues(tRows);
          tradeCount = tRows.length;

          // 格式化欄位
          lSheet.getRange(4, 6, tRows.length, 1).setNumberFormat("#,##0");
          lSheet.getRange(4, 7, tRows.length, 1).setNumberFormat("$#,##0.00");
          lSheet.getRange(4, 8, tRows.length, 3).setNumberFormat("HK$#,##0.00;[Red](HK$#,##0.00);\"HK$0.00\"");
        }
      }

      // -----------------------------------------------------------------------
      // (2) 更新分頁一【分析報告】 (8 KPI 卡片 + 策略對比 + 範例截圖格式維度表 + 原生圖表)
      // -----------------------------------------------------------------------
      var rSheet = lApp.getSheetByName("分析報告") || lApp.getSheets()[0];
      if (rSheet && payload.stats) {
        var s = payload.stats;
        var nowStr = Utilities.formatDate(new Date(), "Asia/Hong_Kong", "yyyy-MM-dd HH:mm:ss");

        // 副標題 (A2)
        rSheet.getRange("A2").setValue(
          "雙策略分配：V4.3 MES (60%) + V5.0 MNQ (40%) | 基礎匯率：1 USD = " + fxRate.toFixed(2) + " HKD | 統計更新：" + nowStr
        );

        // 8 大 KPI 卡片 (全面 HKD)
        var netHkdVal = Number(s.total_net_pnl_hkd !== undefined ? s.total_net_pnl_hkd : (Number(s.total_net_pnl || 0) * fxRate));
        var avgHkdVal = Number(s.avg_trade_hkd !== undefined ? s.avg_trade_hkd : (Number(s.avg_trade || 0) * fxRate));
        var maxWinHkdVal = Number(s.max_win_hkd !== undefined ? s.max_win_hkd : (Number(s.max_win || 0) * fxRate));
        var maxLossHkdVal = Number(s.max_loss_hkd !== undefined ? s.max_loss_hkd : (Number(s.max_loss || 0) * fxRate));
        var commHkdVal = Number(s.total_comm_hkd !== undefined ? s.total_comm_hkd : (Number(s.total_comm || 0) * fxRate));

        rSheet.getRange("A5").setValue(s.total_trades + " 筆");
        rSheet.getRange("C5").setValue(Number(s.win_rate_pct).toFixed(1) + "%");
        rSheet.getRange("E5").setValue("HK$" + (netHkdVal >= 0 ? "+" : "") + netHkdVal.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}));
        rSheet.getRange("G5").setValue(Number(s.profit_factor).toFixed(2));

        rSheet.getRange("A8").setValue("HK$" + (avgHkdVal >= 0 ? "+" : "") + avgHkdVal.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}));
        rSheet.getRange("C8").setValue("HK$" + (maxWinHkdVal >= 0 ? "+" : "") + maxWinHkdVal.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}));
        rSheet.getRange("E8").setValue("HK$" + (maxLossHkdVal >= 0 ? "+" : "") + maxLossHkdVal.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}));
        rSheet.getRange("G8").setValue("HK$" + commHkdVal.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}));

        // 更新策略對比表 (Row 12 - 13)
        if (s.v43_trades !== undefined) {
          rSheet.getRange("D12").setValue(s.v43_trades);
          rSheet.getRange("E12").setValue((s.v43_win_rate_pct || 0) / 100);
          rSheet.getRange("H12").setValue(Number(s.v43_net_pnl_hkd || 0));
        }
        if (s.v50_trades !== undefined) {
          rSheet.getRange("D13").setValue(s.v50_trades);
          rSheet.getRange("E13").setValue((s.v50_win_rate_pct || 0) / 100);
          rSheet.getRange("H13").setValue(Number(s.v50_net_pnl_hkd || 0));
        }

        // ---------------------------------------------------------------------
        // 核心：【規則進場原因與時段表現】 (精確符合用戶範例截圖排版，含 exit_reason)
        // ---------------------------------------------------------------------
        if (s.multidimensional && s.multidimensional.table_rows && s.multidimensional.table_rows.length > 0) {
          var tRowsData = s.multidimensional.table_rows;

          // 標題行 (Row 15)
          rSheet.getRange("A15:D15").merge();
          var tCell = rSheet.getRange("A15");
          tCell.setValue("🎨 規則進場原因與時段表現 (含 Exit Reason 出場維度)");
          tCell.setFontSize(12).setFontWeight("bold").setFontColor("#1E3A8A");

          // 表頭 (Row 16) - 深藍背景 #1E293B，白字置中粗體
          var headRange = rSheet.getRange("A16:D16");
          headRange.setValues([["分析維度", "成交筆數", "勝率 (Win%)", "累計貢獻淨利"]]);
          headRange.setBackground("#1E293B").setFontColor("#FFFFFF").setFontWeight("bold").setHorizontalAlignment("center");

          // 清除舊資料行 (Row 17 至 Row 45)
          rSheet.getRange(17, 1, 30, 4).clearContent();

          var dimRows = [];
          var fontColors = [];

          for (var k = 0; k < tRowsData.length; k++) {
            var item = tRowsData[k];
            var cntStr = item.count + " 筆";
            var wrVal = (Number(item.win_rate_pct) || 0) / 100.0;
            var pnlHkd = Number(item.net_pnl_hkd) || 0;

            dimRows.push([
              item.dimension || "",
              cntStr,
              wrVal,
              pnlHkd
            ]);

            // 正數綠色 (#16A34A)，負數紅色 (#DC2626)
            var pnlColor = (pnlHkd >= 0) ? "#16A34A" : "#DC2626";
            fontColors.push(["#0F172A", "#0F172A", "#0F172A", pnlColor]);
          }

          if (dimRows.length > 0) {
            var dataRange = rSheet.getRange(17, 1, dimRows.length, 4);
            dataRange.setValues(dimRows);
            dataRange.setFontColors(fontColors);

            // 格式化：勝率與 HKD 貨幣
            rSheet.getRange(17, 2, dimRows.length, 1).setHorizontalAlignment("center");
            rSheet.getRange(17, 3, dimRows.length, 1).setNumberFormat("0.0%").setHorizontalAlignment("right");
            rSheet.getRange(17, 4, dimRows.length, 1).setNumberFormat("+HK$#,##0.00;-HK$#,##0.00;HK$0.00").setFontWeight("bold").setHorizontalAlignment("right");
          }

          // -------------------------------------------------------------------
          // 數據分析可視化：若試算表內尚未存在圖表，自動建立原生 Google Sheets 圖表
          // -------------------------------------------------------------------
          var existingCharts = rSheet.getCharts();
          if (existingCharts.length === 0 && dimRows.length > 0) {
            try {
              // 1. 柱狀圖：策略維度累計貢獻淨利
              var chart1 = rSheet.newChart()
                .setChartType(Charts.ChartType.COLUMN)
                .addRange(rSheet.getRange(16, 1, dimRows.length + 1, 1))
                .addRange(rSheet.getRange(16, 4, dimRows.length + 1, 1))
                .setPosition(16, 6, 0, 0)
                .setOption("title", "🎯 出場原因與維度累計淨利貢獻 (HKD)")
                .setOption("width", 580)
                .setOption("height", 340)
                .setOption("legend", { position: "none" })
                .build();
              rSheet.insertChart(chart1);

              // 2. 圓餅圖：出場維度筆數分佈
              var chart2 = rSheet.newChart()
                .setChartType(Charts.ChartType.PIE)
                .addRange(rSheet.getRange(16, 1, dimRows.length + 1, 1))
                .addRange(rSheet.getRange(16, 2, dimRows.length + 1, 1))
                .setPosition(33, 6, 0, 0)
                .setOption("title", "🥧 出場原因與維度筆數分佈")
                .setOption("width", 580)
                .setOption("height", 320)
                .build();
              rSheet.insertChart(chart2);
            } catch (chartErr) {
              // 若權限或環境限制圖表生成，保持數據正常寫入
            }
          }
        }
      }
    }

    return ContentService.createTextOutput(JSON.stringify({
      status: "ok",
      balances_synced: balanceCount,
      trades_synced: tradeCount,
      fx_rate: fxRate,
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
    message: "AutoTrade Google Sheets Webhook V9.3 is active and running."
  })).setMimeType(ContentService.MimeType.JSON);
}
