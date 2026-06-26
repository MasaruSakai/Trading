#!/usr/bin/env python3
"""Main script to automatically update stop-loss (STOP) orders for Japan holdings

Uses KabuClient and Quote context via Futu OpenD (moomoo).
Calculates stop prices based on:
  - Current Price (nominal_price)
  - Cost Price (cost_price)
  - Today's VWAP (vwap)
  - Median True Range (MTR) over the last 14 days
"""
import sys
import os
import time
import math
import argparse
import pandas as pd
from datetime import datetime, timedelta

# Import moomoo quote classes
from moomoo import OpenQuoteContext, RET_OK

# Include current directory in import path to find kabu_client
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from kabu_client import KabuClient, KabuApiError

DEFAULT_PASSWORD_FILE = os.path.join(HERE, "kabu_station_server", "config", "kabu_password.txt")
DEFAULT_PASSWORD_SUFFIX = "prod"

def read_secret(path):
    try:
        with open(path, encoding="utf-8") as fp:
            return fp.read().strip()
    except FileNotFoundError:
        return None

def get_mtr(quote_ctx, code):
    """Calculates Median True Range (MTR) for the last 14 trading days.

    Args:
        quote_ctx (OpenQuoteContext): Active quote context.
        code (str): Stock code (e.g. JP.1475).

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

def round_down_jp_tick(p):
    """Round down stop_price to the nearest valid Japanese ETF/Stock tick size.
    """
    if p <= 0:
        return 0.0
    if p <= 1000:
        return math.floor(p * 10.0) / 10.0
    elif p <= 3000:
        return math.floor(p * 2.0) / 2.0
    elif p <= 10000:
        return float(math.floor(p))
    elif p <= 30000:
        return float(math.floor(p / 5.0) * 5.0)
    elif p <= 50000:
        return float(math.floor(p / 10.0) * 10.0)
    elif p <= 100000:
        return float(math.floor(p / 50.0) * 50.0)
    else:
        return float(math.floor(p / 100.0) * 100.0)

def get_next_business_day():
    """Get the next business day for the Japan stock market, taking into account
    weekends, public holidays (2026/2027), and New Year holidays (12/31 - 1/3).
    """
    current_date = datetime.now()
    
    jp_holidays = {
        # 2026
        "2026-01-01", "2026-01-02", "2026-01-03",
        "2026-01-12", "2026-02-11", "2026-02-23", "2026-03-20", "2026-04-29",
        "2026-05-03", "2026-05-04", "2026-05-05", "2026-05-06",
        "2026-07-20", "2026-08-11", "2026-09-21", "2026-09-22", "2026-09-23",
        "2026-10-12", "2026-11-03", "2026-11-23", "2026-12-31",
        # 2027
        "2027-01-01", "2027-01-02", "2027-01-03",
        "2027-01-11", "2027-02-11", "2027-02-23", "2027-03-21", "2027-03-22",
        "2027-04-29", "2027-05-03", "2027-05-04", "2027-05-05",
        "2027-07-19", "2027-08-11", "2027-09-20", "2027-09-23",
        "2027-10-11", "2027-11-03", "2027-11-23", "2027-12-31",
    }
    
    next_day = current_date + timedelta(days=1)
    while True:
        if next_day.weekday() >= 5:  # Saturday or Sunday
            next_day += timedelta(days=1)
            continue
            
        date_str = next_day.strftime("%Y-%m-%d")
        if date_str in jp_holidays:
            next_day += timedelta(days=1)
            continue
            
        # Check general New Year's holiday (12/31 - 01/03) for any year
        if (next_day.month == 12 and next_day.day == 31) or (next_day.month == 1 and next_day.day in [1, 2, 3]):
            next_day += timedelta(days=1)
            continue
            
        break
        
    return next_day

def main():
    parser = argparse.ArgumentParser(description="Japanese Stock Stop-Loss Update Script")
    parser.add_argument("--base-url", default=os.getenv("KABU_BASE_URL", "http://10.215.1.57:18180"))
    parser.add_argument("--token", default=os.getenv("KABU_API_TOKEN"))
    parser.add_argument("--password", default=os.getenv("KABU_API_PASSWORD"))
    parser.add_argument("--password-file", default=os.getenv("KABU_PASSWORD_FILE", DEFAULT_PASSWORD_FILE))
    parser.add_argument("--password-suffix", default=os.getenv("KABU_PASSWORD_SUFFIX", DEFAULT_PASSWORD_SUFFIX))
    parser.add_argument("--no-token-required", action="store_true", help="Use when calling the Windows proxy")
    parser.add_argument("--dry-run", action="store_true", help="Run without making any order modifications")
    parser.add_argument("--timeout", type=float, default=10.0)
    
    args = parser.parse_args()

    print("==================================================")
    print(f"Starting JP Automatic Stop-Loss update at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if args.dry_run:
        print("!!! DRY RUN MODE ACTIVE !!! (No orders will be placed or canceled)")
    print("==================================================")
    
    # Initialize kabu client
    client = KabuClient(
        base_url=args.base_url,
        token=args.token,
        timeout=args.timeout,
        require_token=not args.no_token_required,
    )
    if not args.no_token_required and not args.token:
        password = args.password or read_secret(args.password_file)
        if not password:
            print("Warning: No token or password provided. Running without auto-authentication.")
        else:
            if args.password_suffix:
                password += args.password_suffix
            try:
                client.token_from_password(password)
                print("[kabu] Successfully authenticated and retrieved token.")
            except Exception as e:
                print(f"[kabu] Authentication failed: {e}")
                sys.exit(1)

    # STEP 0: Cancel all active sell STOP orders for Japan market.
    print("\n--- STEP 0: Clearing all existing active sell STOP orders ---")
    try:
        orders = client.orders(product=1) # spot orders
        if not isinstance(orders, list):
            print(f"No order list returned or error: {orders}")
            orders_list = []
        else:
            orders_list = orders
    except Exception as e:
        print(f"Failed to query active orders: {e}")
        orders_list = []

    active_stop_orders = []
    for order in orders_list:
        state = order.get("State") or order.get("state")
        side = str(order.get("Side") or order.get("side") or "")
        # Filter active sell orders: State != 5 (not ended), Side == '1' (SELL)
        # Note: Since kabu station /orders response might not expose FrontOrderType/ReverseLimitOrder fields directly
        # in some response versions, we clear all active sell orders for safety.
        state_val = int(state) if state is not None else None
        
        if state_val != 5 and side == "1":
            active_stop_orders.append(order)

    if not active_stop_orders:
        print("No active sell STOP orders found to clear.")
    else:
        print(f"Found {len(active_stop_orders)} active sell STOP order(s) to cancel.")
        for order in active_stop_orders:
            order_id = order.get("ID") or order.get("id")
            symbol = order.get("Symbol") or order.get("symbol")
            qty = order.get("Qty") or order.get("qty")
            print(f"Canceling order {order_id} for symbol {symbol} (Qty={qty})...")
            if args.dry_run:
                print(f"[Dry-run] Would cancel order {order_id}.")
            else:
                try:
                    res = client.cancelorder(order_id)
                    print(f"Successfully sent cancellation request. Response: {res}")
                except Exception as e:
                    print(f"Warning: Failed to cancel order {order_id}: {e}")
            time.sleep(0.5)

    # STEP 1: Query spot holdings
    print("\n--- STEP 1: Querying spot holdings ---")
    try:
        positions = client.positions(product=1, addinfo=True)
        if not isinstance(positions, list):
            print(f"No positions list returned or error: {positions}")
            positions_list = []
        else:
            positions_list = positions
    except Exception as e:
        print(f"Failed to query positions: {e}")
        positions_list = []

    active_positions = []
    for pos in positions_list:
        qty = float(pos.get("LeavesQty") or pos.get("leaves_qty") or pos.get("Qty") or pos.get("qty") or 0)
        if qty > 0:
            active_positions.append(pos)

    if not active_positions:
        print("No active JP spot holdings with remaining quantity found.")
        return
    else:
        print(f"Found {len(active_positions)} active JP holding(s) with quantity > 0.")

    # Connect to moomoo quote context
    print("\nConnecting to moomoo OpenD Quote Context at 127.0.0.1:11111...")
    try:
        quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
        print("Connected to moomoo Quote Context.")
    except Exception as e:
        print(f"Failed to connect to moomoo Quote Context: {e}")
        sys.exit(1)

    try:
        # Calculate 1306 (TOPIX ETF) standard MTR ratio
        print("\nCalculating JP.1306 standard MTR ratio...")
        index_code = "JP.1306"
        index_symbol = "1306"
        index_mtr = get_mtr(quote_ctx, index_code)
        
        try:
            index_board = client.board(index_symbol)
            index_last = float(index_board.get("CurrentPrice") or index_board.get("current_price") or 0)
        except Exception as e:
            print(f"Warning: Failed to fetch index current price for {index_symbol}: {e}")
            index_last = 0.0
            
        if index_last <= 0:
            print("Warning: index_last is 0 or negative. Falling back to 2700.0.")
            index_last = 2700.0
            
        index_mtr_ratio = index_mtr / index_last
        print(f"JP.1306 MTR: {index_mtr:.3f}, Last: {index_last:.2f}, Index MTR Ratio: {index_mtr_ratio:.6f}")
        
        # STEP 2: Loop through positions
        print("\n--- STEP 2: Updating stop-loss orders ---")
        for pos in active_positions:
            symbol = str(pos.get("Symbol") or pos.get("symbol") or "")
            if not symbol:
                continue
            
            qty = int(float(pos.get("LeavesQty") or pos.get("leaves_qty") or pos.get("Qty") or pos.get("qty") or 0))
            cost_price = float(pos.get("Price") or pos.get("AvgPrice") or pos.get("avg_price") or 0)
            account_type = pos.get("AccountType") or pos.get("account_type") or 2 # default 2: 特定
            
            print(f"\nProcessing {symbol}: Qty={qty}, Cost={cost_price}, AccountType={account_type}")
            
            # Map symbol to moomoo format e.g. "JP.1475"
            moomoo_code = f"JP.{symbol}"
            
            try:
                # Fetch MTR & VWAP
                mtr = get_mtr(quote_ctx, moomoo_code)
                
                # Get current price and VWAP from board snapshot
                board = client.board(symbol)
                nominal_price = float(board.get("CurrentPrice") or board.get("current_price") or 0)
                vwap = float(board.get("VWAP") or board.get("vwap") or 0)
                
                if nominal_price <= 0 or vwap <= 0:
                    print(f"Warning: Invalid nominal_price ({nominal_price}) or vwap ({vwap}) for {symbol}. Skipping.")
                    continue
                
                # Compute stop_price based on index normalized volatility and min(VWAP, current price)
                mtr_ratio = mtr / nominal_price
                combined_ratio = (mtr_ratio + index_mtr_ratio) / 2.0
                base_price = min(vwap, nominal_price)
                stop_price = base_price - (nominal_price * combined_ratio)
                case_label = "Normalized Index Volatility Stop"
                
                # Round down to nearest JP tick
                rounded_stop_price = round_down_jp_tick(stop_price)
                
                print(f"  MTR: {mtr:.3f}, VWAP: {vwap:.3f}, Last Price: {nominal_price:.3f}, Individual MTR Ratio: {mtr_ratio:.6f}, Combined Ratio: {combined_ratio:.6f}")
                print(f"  Decision: {case_label} | Raw Stop Price: {stop_price:.3f} | Rounded Stop Price: {rounded_stop_price}")
                
                # Send the new stop order
                payload = {
                    "Symbol": symbol,
                    "Exchange": 9,
                    "SecurityType": 1,
                    "Side": "1",
                    "CashMargin": 1,
                    "DelivType": 0,
                    "FundType": "  ",
                    "AccountType": int(account_type),
                    "Qty": qty,
                    "FrontOrderType": 30,
                    "Price": 0,
                    "ExpireDay": int(get_next_business_day().strftime("%Y%m%d")),
                    "ReverseLimitOrder": {
                        "TriggerSec": 1,
                        "TriggerPrice": rounded_stop_price,
                        "UnderOver": 1,
                        "AfterHitOrderType": 1,
                        "AfterHitPrice": 0
                    }
                }
                
                if args.dry_run:
                    print(f"  [Dry-run] Would place STOP order for {symbol}: {payload}")
                else:
                    print(f"  Placing STOP order for {symbol} at {rounded_stop_price}...")
                    res = client.sendorder(payload)
                    print(f"  Successfully placed order. Response: {res}")
                    
            except Exception as e:
                print(f"  Error calculating/updating stop order for {symbol}: {e}")
                continue
    finally:
        print("\nClosing connections...")
        try:
            quote_ctx.close()
        except Exception:
            pass
        print("Done.")

if __name__ == "__main__":
    main()
