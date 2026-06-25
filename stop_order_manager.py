#!/usr/bin/env python3
"""Module for managing stop-loss (STOP) orders via Futu OpenD API.

Provides functions to check existing active stop orders, cancel them,
and place new ones.
"""
import time
from futu import *

def update_stop_order(trd_ctx, code, qty, stop_price, acc_id):
    """Cancels any existing active sell STOP orders for the given code and places a new one.

    Args:
        trd_ctx (OpenSecTradeContext): Active trade context.
        code (str): Symbol code (e.g., 'US.BE', 'US.XLU').
        qty (int/float): Order quantity.
        stop_price (float): The new trigger/stop price.
        acc_id (int): Trading account ID.

    Returns:
        tuple: (bool, dict/str) Status of the final place_order operation and response data.
    """
    print(f"\n--- Starting stop order update for {code} (target price: {stop_price}) ---")
    
    # 1. Query active orders for the given symbol
    ret, data = trd_ctx.order_list_query(
        code=code,
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
        print(f"Failed to query active orders for {code}: {data}")
        return False, f"Query failed: {data}"
    
    # 2. Filter for sell STOP orders

    if not data.empty:
        # Filter for SELL side and STOP order type
        target_orders = data[
            (data['trd_side'].astype(str) == 'SELL') & 
            (data['order_type'].astype(str) == 'STOP')
        ]
        
        # 3. Cancel each existing STOP order found
        for idx, row in target_orders.iterrows():
            order_id = row['order_id']
            orig_qty = int(row['qty'])
            orig_price = float(row['price'])
            
            print(f"Found existing active STOP order {order_id}. Canceling...")
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
            
            # Small delay to let the gateway process the cancellation
            time.sleep(0.5)

    # 4. Place the new STOP order
    print(f"Placing new STOP order: code={code}, qty={qty}, trigger_price={stop_price}")
    ret_place, data_place = trd_ctx.place_order(
        price=stop_price,
        qty=qty,
        code=code,
        trd_side=TrdSide.SELL,
        order_type=OrderType.STOP,
        aux_price=stop_price,
        trd_env=TrdEnv.REAL,
        acc_id=acc_id
    )
    
    if ret_place == RET_OK:
        new_order_id = data_place['order_id'][0]
        print(f"Successfully placed new STOP order {new_order_id} for {code}.")
        return True, data_place
    else:
        print(f"Failed to place new STOP order for {code}: {data_place}")
        return False, data_place
