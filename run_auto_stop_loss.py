#!/usr/bin/env python3
"""Main script to automatically update stop-loss (STOP) orders for US holdings

Uses both Trade and Quote contexts via Futu OpenD.
Calculates stop prices based on:
  - Unrealized PL (Holding Profit/Loss)
  - Current Price (nominal_price)
  - Cost Price (cost_price)
  - Today's VWAP (avg_price)
  - Median True Range (MTR) over the last 14 days
"""
import sys
import time
import pandas as pd
from datetime import datetime, timedelta
from futu import *

# Include current directory in import path to find stop_order_manager
sys.path.append("/Users/masaru/Projects/Trading")
from stop_order_manager import update_stop_order

# Constants
ACC_ID = 284852706236374484  # Account ID

def get_mtr(quote_ctx, code):
    """Calculates Median True Range (MTR) for the last 14 trading days.

    Args:
        quote_ctx (OpenQuoteContext): Active quote context.
        code (str): Stock code.

    Returns:
        float: Calculated MTR.
    """
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=25)).strftime("%Y-%m-%d")
    
    ret, df, _ = quote_ctx.request_history_kline(
        code, 
        start=start_date, 
        end=end_date, 
        ktype='K_DAY', 
        autype='qfq'
    )
    # Small delay to respect rate limits
    time.sleep(0.2)
    
    if ret != RET_OK or df.empty:
        raise ValueError(f"Failed to fetch historical daily candles for {code}: {df}")
    
    # Calculate True Range (TR)
    # df columns: high, low, last_close (prior close)
    tr1 = df['high'] - df['low']
    tr2 = (df['high'] - df['last_close']).abs()
    tr3 = (df['low'] - df['last_close']).abs()
    
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    # Get the median of the last 14 trading days
    mtr = tr.tail(14).median()
    return float(mtr)

def get_today_vwap(quote_ctx, code):
    """Fetches today's volume weighted average price (VWAP) from snapshot if inside RTH.
    If outside US Regular Trading Hours (RTH), fetches the last completed trading day's VWAP from historical daily klines.
    """
    from datetime import datetime, time as dt_time, timedelta
    now = datetime.now()
    current_time = now.time()
    
    # Define US Regular Trading Hours JST window (safe estimate: 22:30 to 06:00 JST)
    is_rth = (current_time >= dt_time(22, 30)) or (current_time <= dt_time(6, 0))
    
    if is_rth:
        ret, snap = quote_ctx.get_market_snapshot([code])
        time.sleep(0.2)
        
        if ret != RET_OK or snap.empty:
            raise ValueError(f"Failed to fetch market snapshot for {code}: {snap}")
            
        # 'avg_price' in the snapshot returns the daily average price (VWAP)
        vwap = snap['avg_price'].iloc[0]
        return float(vwap)
    else:
        # Fetch recent historical daily data to compute the last completed trading day's VWAP
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        
        ret, df, _ = quote_ctx.request_history_kline(
            code, 
            start=start_date, 
            end=end_date, 
            ktype='K_DAY', 
            autype='qfq'
        )
        time.sleep(0.2)
        
        if ret != RET_OK or df.empty:
            raise ValueError(f"Failed to fetch historical daily candles for {code}: {df}")
            
        last_row = df.iloc[-1]
        volume = float(last_row['volume'])
        turnover = float(last_row['turnover'])
        
        if volume > 0:
            vwap = turnover / volume
        else:
            vwap = float(last_row['close'])
            
        return float(vwap)

def cancel_all_active_stop_orders(trd_ctx, acc_id):
    """Cancels all active sell STOP orders for the account, regardless of holdings.
    """
    print("\n--- Clearing all existing active sell STOP orders ---")
    ret, data = trd_ctx.order_list_query(
        status_filter_list=[
            OrderStatus.WAITING_SUBMIT,
            OrderStatus.SUBMITTING,
            OrderStatus.SUBMITTED
        ],
        trd_env=TrdEnv.REAL,
        acc_id=acc_id,
        refresh_cache=True
    )
    if ret != RET_OK:
        print(f"Failed to query active orders: {data}")
        return
        
    if data.empty:
        print("No active orders found to clear.")
        return
        
    # Filter for SELL side and STOP/STOP_LIMIT order type
    target_orders = data[
        (data['trd_side'].astype(str) == 'SELL') & 
        (data['order_type'].astype(str).isin(['STOP', 'STOP_LIMIT']))
    ]
    
    if target_orders.empty:
        print("No active sell STOP orders found to clear.")
        return
        
    print(f"Found {len(target_orders)} active sell STOP order(s) to cancel.")
    for idx, row in target_orders.iterrows():
        order_id = row['order_id']
        code = row['code']
        orig_qty = int(row['qty'])
        orig_price = float(row['price'])
        
        print(f"Canceling order {order_id} for {code}...")
        ret_cancel, data_cancel = trd_ctx.modify_order(
            modify_order_op=ModifyOrderOp.CANCEL,
            order_id=order_id,
            qty=orig_qty,
            price=orig_price,
            trd_env=TrdEnv.REAL,
            acc_id=acc_id
        )
        if ret_cancel == RET_OK:
            print(f"Successfully canceled order {order_id}.")
        else:
            print(f"Warning: Failed to cancel order {order_id}: {data_cancel}")
        time.sleep(0.5)

def main():
    print("==================================================")
    print(f"Starting automatic Stop-Loss update at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("==================================================")
    
    # Initialize connection context
    print("Connecting to Futu OpenD...")
    trd_ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.US,
        host='127.0.0.1',
        port=11111,
        security_firm=SecurityFirm.FUTUJP
    )
    quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    
    try:
        # 0. Cancel all active stop orders first
        cancel_all_active_stop_orders(trd_ctx, ACC_ID)
        
        # Calculate SPY standard MTR ratio
        print("\nCalculating SPY standard MTR ratio...")
        spy_code = 'US.SPY'
        spy_mtr = get_mtr(quote_ctx, spy_code)
        ret_spy, spy_snap = quote_ctx.get_market_snapshot([spy_code])
        time.sleep(0.2)
        if ret_spy != RET_OK or spy_snap.empty:
            raise ValueError(f"Failed to fetch market snapshot for {spy_code}: {spy_snap}")
        spy_last = float(spy_snap['last_price'].iloc[0])
        spy_mtr_ratio = spy_mtr / spy_last
        print(f"SPY MTR: {spy_mtr:.3f}, SPY Last: {spy_last:.2f}, SPY MTR Ratio: {spy_mtr_ratio:.6f}")
        
        # 1. Fetch current US positions
        print("\nQuerying current US positions...")
        ret, positions = trd_ctx.position_list_query(
            trd_env=TrdEnv.REAL, 
            acc_id=ACC_ID, 
            currency='USD'
        )
        
        if ret != RET_OK:
            print(f"Failed to retrieve positions: {positions}")
            return
            
        if positions.empty:
            print("No active US positions found.")
            return
            
        # Keep only long positions with quantity > 0
        active_positions = positions[
            (positions['qty'] > 0) & 
            (positions['position_side'].astype(str) == 'LONG')
        ]
        
        if active_positions.empty:
            print("No active LONG US positions with quantity > 0 found.")
            return
            
        print(f"Found {len(active_positions)} active position(s). Processing...")
        
        # 2. Iterate through each position and calculate stop price
        for idx, row in active_positions.iterrows():
            code = row['code']
            qty = int(row['qty'])
            cost_price = float(row['cost_price'])
            nominal_price = float(row['nominal_price'])  # Current Price
            
            print(f"\nProcessing {code}: Qty={qty}, Cost={cost_price}, Last={nominal_price}")
            
            try:
                # Fetch MTR & VWAP
                mtr = get_mtr(quote_ctx, code)
                vwap = get_today_vwap(quote_ctx, code)
                
                # Apply MTR-based stop-loss logic using SPY standardized volatility
                mtr_ratio = mtr / nominal_price
                combined_ratio = (mtr_ratio + spy_mtr_ratio) / 2.0
                base_price = min(vwap, nominal_price)
                stop_price = base_price - (nominal_price * combined_ratio)
                case_label = "Normalized Index Volatility Stop"
                
                # Round stop price according to US market tick size specifications:
                # $1.00 and above: 2 decimal places (cents)
                # Below $1.00: 4 decimal places
                if stop_price >= 1.0:
                    stop_price = round(stop_price, 2)
                else:
                    stop_price = round(stop_price, 4)

                print(f"MTR: {mtr:.3f}, VWAP: {vwap:.3f}, Individual MTR Ratio: {mtr_ratio:.6f}, Combined Ratio: {combined_ratio:.6f}")
                print(f"Decision: {case_label} | Target Stop Price: {stop_price}")
                
                # Trigger cancellation and re-placement of stop order
                success, res = update_stop_order(
                    trd_ctx=trd_ctx,
                    code=code,
                    qty=qty,
                    stop_price=stop_price,
                    acc_id=ACC_ID
                )
                
                # Add a sleep to respect the API rate limit (Get Order list: Max 10 times per 30 seconds)
                time.sleep(2.5)
                
            except Exception as e:
                print(f"Error calculating/updating stop order for {code}: {e}")
                time.sleep(1.0)
                continue
                
    finally:
        print("\nClosing connections...")
        quote_ctx.close()
        trd_ctx.close()
        print("Done.")

if __name__ == "__main__":
    main()
