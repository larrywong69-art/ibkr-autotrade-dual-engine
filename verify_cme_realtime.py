"""
verify_cme_realtime.py
驗證 CME 微型期貨 (MES & MNQ) 實時報價訂閱狀態
檢查市場行情數據類型 (MarketDataType):
1 = Real-Time (實時行情)
2 = Frozen (盤後凍結)
3 = Delayed (延遲行情，約延後 10-15 分鐘)
4 = Delayed-Frozen (延遲凍結)
"""

import sys
import asyncio
from ib_async import IB, Future, util

async def test_cme_realtime(port=4002, client_id=98):
    ib = IB()
    print(f"[*] 正在連線至 IB Gateway / TWS (Port: {port}, ClientId: {client_id})...")
    
    warnings_received = []
    
    def on_error(reqId, errorCode, errorString, contract):
        if errorCode in [10167, 10168, 2104, 2106, 2119, 354]:
            warnings_received.append((errorCode, errorString))
            print(f"  [IB 訊息/代碼 {errorCode}]: {errorString}")

    ib.errorEvent += on_error

    try:
        await ib.connectAsync('127.0.0.1', port, clientId=client_id, timeout=10)
    except Exception as e:
        print(f"[!] 連線失敗: {e}")
        return

    print(f"[✓] 成功連線！帳戶: {ib.managedAccounts()}")

    # 1. 要求實時報價 (Type 1 = Real-Time)
    print("\n[*] 設定市場數據模式為: 1 (Real-Time 實時串流)")
    ib.reqMarketDataType(1)

    # 2. 定義 MES 與 MNQ 2026年12月 (或當季合約)
    # 根據先前日誌 conId: MESZ6=815824257, MNQZ6=815824267
    mes_contract = Future(conId=815824257, symbol='MES', exchange='CME', currency='USD')
    mnq_contract = Future(conId=815824267, symbol='MNQ', exchange='CME', currency='USD')
    
    await ib.qualifyContractsAsync(mes_contract, mnq_contract)
    print(f"[✓] 合約確認: {mes_contract.localSymbol} (ConId: {mes_contract.conId}), {mnq_contract.localSymbol} (ConId: {mnq_contract.conId})")

    # 3. 訂閱市場行情
    print("[*] 正在訂閱行情串流，等待 5 秒接收行情回傳...")
    mes_ticker = ib.reqMktData(mes_contract, '', False, False)
    mnq_ticker = ib.reqMktData(mnq_contract, '', False, False)

    for i in range(5):
        await asyncio.sleep(1)
        print(f"  ... 等待行情回傳 ({i+1}/5s): MES Last={mes_ticker.last}, MNQ Last={mnq_ticker.last}, Type={mes_ticker.marketDataType}")

    print("\n" + "="*50)
    print("【CME 期貨報價狀態診斷結果】")
    print("="*50)

    type_desc = {
        1: "1 - 實時報價 (Real-Time Streaming) ✅",
        2: "2 - 盤後凍結報價 (Frozen)",
        3: "3 - 延遲報價 (Delayed ~15分鐘) ⚠️",
        4: "4 - 延遲凍結 (Delayed-Frozen) ⚠️"
    }

    mes_type_str = type_desc.get(mes_ticker.marketDataType, f"未知 ({mes_ticker.marketDataType})")
    mnq_type_str = type_desc.get(mnq_ticker.marketDataType, f"未知 ({mnq_ticker.marketDataType})")

    print(f"MES 合約 ({mes_contract.localSymbol}):")
    print(f"  • 行情數據模式: {mes_type_str}")
    print(f"  • 買價 (Bid): {mes_ticker.bid} x {mes_ticker.bidSize}")
    print(f"  • 賣價 (Ask): {mes_ticker.ask} x {mes_ticker.askSize}")
    print(f"  • 最新成交價 (Last): {mes_ticker.last}")
    print(f"  • 買賣價差 (Spread): {round(mes_ticker.ask - mes_ticker.bid, 2) if mes_ticker.bid and mes_ticker.ask else 'N/A'}")

    print(f"\nMNQ 合約 ({mnq_contract.localSymbol}):")
    print(f"  • 行情數據模式: {mnq_type_str}")
    print(f"  • 買價 (Bid): {mnq_ticker.bid} x {mnq_ticker.bidSize}")
    print(f"  • 賣價 (Ask): {mnq_ticker.ask} x {mnq_ticker.askSize}")
    print(f"  • 最新成交價 (Last): {mnq_ticker.last}")
    print(f"  • 買賣價差 (Spread): {round(mnq_ticker.ask - mnq_ticker.bid, 2) if mnq_ticker.bid and mnq_ticker.ask else 'N/A'}")

    has_unsubscribed_warning = any(code in [354, 10167, 10168] for code, _ in warnings_received)
    print("\n" + "-"*50)
    if not has_unsubscribed_warning and mes_ticker.marketDataType == 1 and mnq_ticker.marketDataType == 1 and mes_ticker.last > 0:
        print("🎉 [驗證成功] CME 實時報價已正式生效！無任何延遲警告，雙合約均處於 Type 1 實時串流狀態！")
    elif has_unsubscribed_warning or mes_ticker.marketDataType in [3, 4] or (mes_ticker.last is None or str(mes_ticker.last) == 'nan'):
        print("⚠️ [需要重新登入 IB Gateway / TWS 刷新權限]")
        print("原因說明:")
        print("  IBKR 帳戶管理後台 (Client Portal) 訂閱完成後，伺服器權限已更新，")
        print("  但目前已開啟的 IB Gateway 或 TWS 連線工作階段 (Session) 仍保留登入時的舊快取權限。")
        print("解決方法:")
        print("  請將您的 IB Gateway 或 TWS 軟體進行一次【登出並重新登入 (Restart / Re-login)】，")
        print("  登入時 Gateway 便會自動從 IBKR 主機拉取最新的 CME 實時權限！")
    else:
        print(f"ℹ️ 目前狀態碼: MES={mes_ticker.marketDataType}, MNQ={mnq_ticker.marketDataType}")
    print("="*50)

    ib.cancelMktData(mes_contract)
    ib.cancelMktData(mnq_contract)
    ib.disconnect()

if __name__ == '__main__':
    asyncio.run(test_cme_realtime())
