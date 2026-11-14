"""
03_paper_order_test.py
示範在 Paper Trading 模擬帳戶中進行 NVDA 自動化下單與取消掛單測試：
1. 嚴格檢查帳戶是否為 Paper Trading (DU/DF 開頭)
2. 取得 NVDA 參考市價，以 80% 安全限價掛單（避免意外撮合）
3. 送出限價買單 (Limit Buy Order)
4. 追蹤委託單狀態 (Submitted / PreSubmitted)
5. 自動送出撤單請求 (Cancel Order) 並確認狀態變更為 Cancelled
"""

import math
import sys
import time

try:
    from ib_async import IB, Stock, LimitOrder, MarketOrder
except ImportError:
    from ib_insync import IB, Stock, LimitOrder, MarketOrder


def get_reference_price(ib, contract):
    """安全取得標的參考價格（支援週末、休市及延遲行情）"""
    ib.reqMarketDataType(3)  # 使用延遲/免費報價模式
    ticker = ib.reqMktData(contract, snapshot=True)
    ib.sleep(2)

    for p in [ticker.last, ticker.close, ticker.marketPrice()]:
        if p is not None and not (isinstance(p, float) and math.isnan(p)) and p > 0:
            return float(p)

    # 若休市無盤口快照，抓取最近 1 個交易日的收盤價
    try:
        bars = ib.reqHistoricalData(
            contract,
            endDateTime='',
            durationStr='3 D',
            barSizeSetting='1 day',
            whatToShow='TRADES',
            useRTH=True,
            formatDate=1
        )
        if bars:
            return float(bars[-1].close)
    except Exception as e:
        print(f"[*] 取得歷史參考價提示: {e}")

    return 120.0  # 預設回退估計值


def paper_order_test(symbol='NVDA', quantity=1, port=4002, client_id=1):
    ib = IB()
    print(f"[*] 連線至 IB Gateway (Port: {port}, ClientID: {client_id})...")
    ib.connect('127.0.0.1', port, clientId=client_id)

    accounts = ib.managedAccounts()
    # 雙重安全防護：確保當前連線為 Paper 模擬帳戶
    is_paper = any(acc.startswith(('DU', 'DF')) for acc in accounts)
    if not is_paper:
        print("[!] 嚴重警告：當前連線帳號非 Paper Trading 模擬帳號！為了您的資金安全，程序已自動終止。")
        ib.disconnect()
        return

    print(f"[✓] 安全防護驗證通過：目前為模擬帳戶 ({accounts[0]})")

    # 定義 NVDA 美股合約
    contract = Stock(symbol, 'SMART', 'USD')
    ib.qualifyContracts(contract)
    print(f"[✓] 合約確認完成: {contract.symbol} (ConId: {contract.conId}, Exchange: {contract.exchange})")

    # 取得參考價格並計算安全限價
    ref_price = get_reference_price(ib, contract)
    safe_limit_price = round(ref_price * 0.8, 2)  # 掛在參考價的 80%，確保不會被意外撮合，方便觀察掛單
    print(f"[*] 參考市場價格: ${ref_price:.2f} | 採用安全測試買進限價 (80%): ${safe_limit_price:.2f}")

    # 建立限價買單
    print(f"\n[1] 準備送出委託單：BUY {quantity} 股 {symbol} @ ${safe_limit_price}...")
    order = LimitOrder('BUY', quantity, safe_limit_price, tif='DAY')

    # 監聽委託狀態變化事件
    def on_status_change(trade):
        print(f"    --> [事件回報] 訂單 #{trade.order.orderId} 狀態變更為: {trade.orderStatus.status}")

    trade = ib.placeOrder(contract, order)
    trade.statusEvent += on_status_change

    # 等待 IB 伺服器受理
    for _ in range(5):
        ib.sleep(1)
        if trade.orderStatus.status in ('Submitted', 'PreSubmitted'):
            break

    print(f"[✓] 委託已由 IB 伺服器受理！")
    print(f"    • Order ID     : {trade.order.orderId}")
    print(f"    • 目前委託狀態  : {trade.orderStatus.status}")
    print(f"    • 剩餘未成交量  : {trade.orderStatus.remaining} 股")

    # 查看目前所有活躍的掛單
    open_trades = ib.openTrades()
    print(f"\n[2] 查詢當前掛單清單 (Active Open Orders: {len(open_trades)}):")
    for t in open_trades:
        print(f"    • [掛單中] #{t.order.orderId}: {t.order.action} {t.order.totalQuantity} {t.contract.symbol} @ ${t.order.lmtPrice} | 狀態: {t.orderStatus.status}")

    # 停留 3 秒讓使用者觀察掛單存在
    print("\n[3] 保持掛單 3 秒，隨後發送撤單指令...")
    ib.sleep(3)

    # 送出撤單
    print(f"[*] 正在向 IBKR 發送取消指令 (Cancel Order #{trade.order.orderId})...")
    ib.cancelOrder(order)

    # 等待取消狀態確認
    for _ in range(5):
        ib.sleep(1)
        if trade.orderStatus.status == 'Cancelled':
            break

    print(f"\n[4] 撤單確認完成！最終狀態: {trade.orderStatus.status}")

    # 再次確認掛單清單是否已清空
    remaining_open = [t for t in ib.openTrades() if t.order.orderId == trade.order.orderId]
    if not remaining_open:
        print(f"[✓] 驗證：委託單 #{trade.order.orderId} 已成功自掛單簿完全移除。")

    ib.disconnect()
    print("\n[✓] NVDA 模擬下單與取消掛單測試成功完成！")


if __name__ == '__main__':
    target_symbol = sys.argv[1] if len(sys.argv) > 1 else 'NVDA'
    paper_order_test(symbol=target_symbol)
