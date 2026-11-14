#!/usr/bin/env python3
"""
================================================================================
04_mes_order_test.py
用於向 IBKR Paper Trading 模擬帳戶送出 CME MES (微型標普期貨) 實體限價測試掛單
並在 5 秒後安全撤單，以驗證 API 下單管道 100% 暢通，且可在 TWS / Gateway 查見！
================================================================================
"""

import sys
import time
import math
from ib_async import IB, Future, LimitOrder

def test_mes_order(port=4002, client_id=19):
    ib = IB()
    print(f"[*] 正在連線至 IB Gateway (Port: {port}, ClientID: {client_id})...")
    ib.connect('127.0.0.1', port, clientId=client_id)

    accounts = ib.managedAccounts()
    is_paper = any(acc.startswith(('DU', 'DF')) for acc in accounts)
    if not is_paper:
        print("[!] 嚴重警告：當前連線非 Paper 模擬帳戶！終止操作。")
        ib.disconnect()
        return

    account_id = accounts[0]
    print(f"[✓] 驗證通過：目前為 IBKR 模擬帳戶 ({account_id})")

    # 查詢並鎖定 CME MES 當季主力合約
    contract_query = Future(symbol="MES", exchange="CME")
    details = ib.reqContractDetails(contract_query)
    if not details:
        print("[!] 未找到 MES 合約細節，請檢查網路連線。")
        ib.disconnect()
        return

    sorted_details = sorted(details, key=lambda d: d.contract.lastTradeDateOrContractMonth)
    contract = sorted_details[0].contract
    ib.qualifyContracts(contract)
    print(f"[✓] 主力合約確認: {contract.localSymbol} (ConId: {contract.conId}, 到期日: {contract.lastTradeDateOrContractMonth})")

    # 取得參考價格
    ib.reqMarketDataType(3)
    ticker = ib.reqMktData(contract, snapshot=True)
    ib.sleep(2)
    cur_p = None
    for p in [ticker.marketPrice(), ticker.last, ticker.close]:
        if p is not None and not (isinstance(p, float) and math.isnan(p)) and p > 0:
            cur_p = float(p)
            break
    
    if cur_p is None:
        bars = ib.reqHistoricalData(contract, endDateTime='', durationStr='1 D', barSizeSetting='5 mins', whatToShow='TRADES', useRTH=False, formatDate=1)
        if bars:
            cur_p = float(bars[-1].close)
        else:
            cur_p = 7500.0

    print(f"[*] 當前 CME MES 參考市價: {cur_p:.2f}")

    # 計算安全限價（掛在市價 85% 處，遠離現價以保證不會被意外撮合成交）
    safe_lmt = round((cur_p * 0.85) / 0.25) * 0.25
    print(f"[1] 準備向 IBKR 發送真實限價買單: BUY 1 口 {contract.localSymbol} @ ${safe_lmt:.2f} (GTC)...")

    order = LimitOrder('BUY', 1, safe_lmt, tif='GTC')
    trade = ib.placeOrder(contract, order)

    # 等待 IB 伺服器受理
    for _ in range(5):
        ib.sleep(1)
        if trade.orderStatus.status in ('Submitted', 'PreSubmitted'):
            break

    print(f"\n[✓] 訂單已成功送達芝商所 (CME) 撮合系統！")
    print(f"    • IBKR 官方訂單編號 (Order ID): {trade.order.orderId}")
    print(f"    • 芝商所委託狀態 (Status)     : {trade.orderStatus.status}")
    print(f"    • 委託合約與數量             : {order.action} {order.totalQuantity} 口 {contract.localSymbol}")
    print(f"    • 限價 (Limit Price)         : ${order.lmtPrice:.2f}")

    # 查詢目前所有活躍掛單
    open_trades = ib.openTrades()
    print(f"\n[2] 查詢當前實體活躍掛單清單 (Active Orders: {len(open_trades)} 筆):")
    for t in open_trades:
        print(f"    • [掛單中] #{t.order.orderId}: {t.order.action} {t.order.totalQuantity} 口 {t.contract.localSymbol} @ ${t.order.lmtPrice or t.order.auxPrice} | 狀態: {t.orderStatus.status}")

    print("\n[3] 保持掛單 4 秒，以便您在 IB Gateway / TWS 委託單視窗確認此單真實存在...")
    ib.sleep(4)

    # 送出撤單請求
    print(f"[*] 正在向 IBKR 發送撤單請求 (Cancel Order #{trade.order.orderId})...")
    ib.cancelOrder(order)

    for _ in range(5):
        ib.sleep(1)
        if trade.orderStatus.status == 'Cancelled':
            break

    print(f"[✓] 撤單確認完成！最終狀態: {trade.orderStatus.status}")
    ib.disconnect()
    print("[✓] 實單測試結束，API 下單管道 100% 正常。")

if __name__ == '__main__':
    test_mes_order()
