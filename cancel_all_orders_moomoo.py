#!/usr/bin/env python3
"""Script to cancel all active orders (buy, sell, stop, limit, etc.) for US account on moomoo.
"""
import sys
import time
from futu import *

ACC_ID = 284852706236374484

def main():
    print("==================================================")
    print("Canceling all active moomoo orders...")
    print("==================================================")

    # Initialize connection context
    trd_ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.US,
        host='127.0.0.1',
        port=11111,
        security_firm=SecurityFirm.FUTUJP
    )

    try:
        # Query active orders (buy, sell, stop, etc. everything)
        ret, data = trd_ctx.order_list_query(
            status_filter_list=[
                OrderStatus.WAITING_SUBMIT,
                OrderStatus.SUBMITTING,
                OrderStatus.SUBMITTED
            ],
            trd_env=TrdEnv.REAL,
            acc_id=ACC_ID,
            refresh_cache=True
        )

        if ret != RET_OK:
            print(f"Failed to query active orders: {data}")
            return

        if data.empty:
            print("No active orders found to cancel.")
            return

        print(f"Found {len(data)} active order(s) to cancel.")
        for idx, row in data.iterrows():
            order_id = row['order_id']
            code = row['code']
            qty = int(row['qty'])
            price = float(row['price'])
            trd_side = row['trd_side']
            order_type = row['order_type']

            print(f"Canceling order {order_id} ({trd_side} {order_type}) for {code}...")
            ret_cancel, data_cancel = trd_ctx.modify_order(
                modify_order_op=ModifyOrderOp.CANCEL,
                order_id=order_id,
                qty=qty,
                price=price,
                trd_env=TrdEnv.REAL,
                acc_id=ACC_ID
            )
            if ret_cancel == RET_OK:
                print(f"Successfully canceled order {order_id}.")
            else:
                print(f"Warning: Failed to cancel order {order_id}: {data_cancel}")
            time.sleep(0.5)

    finally:
        print("Closing trade context...")
        trd_ctx.close()
        print("Done.")

if __name__ == "__main__":
    main()
