"""
02_fetch_market_data.py
示範如何向 IB Gateway 取得市場行情數據：
1. 查詢歷史 K 棒數據 (Historical Data) 並轉成 Pandas DataFrame
2. 取得即時報價快照 (Market Data Snapshot)
"""

import sys
try:
    from ib_async import IB, Stock, util
except ImportError:
    from ib_insync import IB, Stock, util

def fetch_data(symbol='AAPL', port=4002):
    ib = IB()
    print(f"[*] 連線至 IB Gateway (Port: {port})...")
    ib.connect('127.0.0.1', port, clientId=2)

    # 定義標的合約（以美股 AAPL 為例）
    contract = Stock(symbol, 'SMART', 'USD')
    ib.qualifyContracts(contract)
    print(f"[✓] 合約確認完成: {contract}")

    # 1. 抓取歷史 K 棒（以最近 5 天、1 小時 K 棒為例）
    print(f"\n[*] 正在下載 {symbol} 歷史 K 棒資料...")
    bars = ib.reqHistoricalData(
        contract,
        endDateTime='',
        durationStr='5 D',
        barSizeSetting='1 hour',
        whatToShow='TRADES',
        useRTH=True,
        formatDate=1
    )

    df = util.df(bars)
    if df is not None and not df.empty:
        print(f"[✓] 成功取得 {len(df)} 根 K 棒！最新 5 根數據如下：")
        print(df[['date', 'open', 'high', 'low', 'close', 'volume']].tail())
    else:
        print("[!] 未取得歷史數據，可能因未訂閱市場即時數據或休市期間延遲。")

    # 2. 取得即時快照報價
    print(f"\n[*] 正在取得 {symbol} 即時盤口快照...")
    ib.reqMarketDataType(3)  # 3 = 延遲免費行情（如果 Paper Trading 帳戶沒有付費訂閱即時報價，此設定可避免報錯）
    ticker = ib.reqMktData(contract, snapshot=True)
    ib.sleep(2)  # 等待行情回傳

    print(f"  • {symbol} Bid (買價): {ticker.bid} / Ask (賣價): {ticker.ask} / Last (成交價): {ticker.last}")

    ib.disconnect()
    print("\n[✓] 行情讀取完成。")

if __name__ == '__main__':
    target_symbol = sys.argv[1] if len(sys.argv) > 1 else 'AAPL'
    fetch_data(symbol=target_symbol)
