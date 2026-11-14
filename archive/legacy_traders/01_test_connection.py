"""
01_test_connection.py
用於測試與 IB Gateway / TWS Paper Trading 帳戶的連線狀態，
並讀取模擬帳戶的基本資產總覽與目前持倉。
"""

import sys
try:
    from ib_async import IB, util
except ImportError:
    from ib_insync import IB, util

def test_paper_connection(host='127.0.0.1', port=4002, client_id=1):
    ib = IB()
    print(f"[*] 嘗試連線至 IB Gateway (Host: {host}, Port: {port}, ClientID: {client_id})...")
    
    try:
        ib.connect(host, port, clientId=client_id, timeout=10)
    except ConnectionRefusedError:
        print("[!] 連線失敗：無法連線至目標 Port。請確認：")
        print("    1. IB Gateway 是否已啟動並完成登入？")
        print("    2. 是否勾選了 Configure -> Settings -> API -> Settings 中的「Enable ActiveX and Socket Clients」？")
        print(f"    3. Port 是否設定正確（IB Gateway Paper 預設為 4002；若是 TWS Paper 則為 7497）？")
        return
    except Exception as e:
        print(f"[!] 連線錯誤: {e}")
        return

    accounts = ib.managedAccounts()
    print(f"[✓] 成功連線！偵測到帳戶: {accounts}")

    # 檢查是否為 Paper Trading 帳戶（安全防護機制）
    is_paper = any(acc.startswith(('DU', 'DF')) for acc in accounts)
    if is_paper:
        print("[✓] 確認為 Paper Trading (模擬交易) 帳戶，安全無虞。")
    else:
        print("[!] 警告：帳號代號未偵測到 DU/DF 前綴，請再次確認是否登入為 Paper Trading 模式！")

    # 讀取帳戶資產總覽
    print("\n--- 帳戶資金總覽 (Account Summary) ---")
    summary = ib.accountSummary()
    important_tags = {'NetLiquidation', 'TotalCashValue', 'BuyingPower', 'GrossPositionValue'}
    for item in summary:
        if item.tag in important_tags:
            print(f"  • {item.tag:20s}: {item.value} {item.currency}")

    # 讀取現有持倉
    print("\n--- 目前持倉 (Positions) ---")
    positions = ib.positions()
    if not positions:
        print("  目前尚無持倉。")
    else:
        for pos in positions:
            print(f"  • {pos.contract.symbol:6s} ({pos.contract.secType}): {pos.position} 股/口, 平均成本: {pos.avgCost:.2f}")

    ib.disconnect()
    print("\n[✓] 測試完畢，連線已正常關閉。")

if __name__ == '__main__':
    # 支援透過指令列調整 Port（如：python 01_test_connection.py 7497）
    target_port = int(sys.argv[1]) if len(sys.argv) > 1 else 4002
    test_paper_connection(port=target_port)
