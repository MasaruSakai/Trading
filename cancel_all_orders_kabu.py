#!/usr/bin/env python3
"""Script to cancel all active orders (buy, sell, stop, limit, etc.) for Japan market on Kabu Station API.
"""
import sys
import os
import time
import argparse
from datetime import datetime

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

def main():
    parser = argparse.ArgumentParser(description="Japanese Stock Cancel All Orders Script")
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
    print(f"Starting JP Cancel All Orders at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if args.dry_run:
        print("!!! DRY RUN MODE ACTIVE !!! (No orders will be canceled)")
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

    try:
        orders = client.orders(product=1) # spot orders
        if not isinstance(orders, list):
            print(f"No order list returned or error: {orders}")
            return
    except Exception as e:
        print(f"Failed to query active orders: {e}")
        return

    active_orders = []
    for order in orders:
        state = order.get("State") or order.get("state")
        state_val = int(state) if state is not None else None
        
        # State != 5 (not ended)
        if state_val != 5:
            active_orders.append(order)

    if not active_orders:
        print("No active orders found to cancel.")
    else:
        print(f"Found {len(active_orders)} active order(s) to cancel.")
        for order in active_orders:
            order_id = order.get("ID") or order.get("id")
            symbol = order.get("Symbol") or order.get("symbol")
            qty = order.get("Qty") or order.get("qty")
            side = order.get("Side") or order.get("side")
            # Convert Side to human-readable format if possible (1: SELL, 2: BUY)
            side_str = "SELL" if str(side) == "1" else "BUY" if str(side) == "2" else str(side)
            
            print(f"Canceling order {order_id} ({side_str}) for symbol {symbol} (Qty={qty})...")
            if args.dry_run:
                print(f"[Dry-run] Would cancel order {order_id}.")
            else:
                try:
                    res = client.cancelorder(order_id)
                    print(f"Successfully sent cancellation request. Response: {res}")
                except Exception as e:
                    print(f"Warning: Failed to cancel order {order_id}: {e}")
            time.sleep(0.5)

if __name__ == "__main__":
    main()
