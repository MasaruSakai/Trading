#!/usr/bin/env python3
"""特定銘柄の各フィルタ値を個別に表示するデバッグスクリプト。
使い方: python _debug_stock.py US.PANW
"""
import sys
from moomoo import OpenQuoteContext, RET_OK, PeriodType
from analysis_common import get_distribution, get_big_median, BIG_MED_DAYS
import statistics

OPEND_HOST = '127.0.0.1'
OPEND_PORT  = 11111

def fmt(v):
    return f"{v:>+15,.0f}"

def main():
    code = sys.argv[1] if len(sys.argv) > 1 else 'US.PANW'
    print(f"\n=== {code} フィルタ診断 ===\n")

    ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
    try:
        # ── フィルタ①: 当日資金分布 ──────────────────────────────────
        d = get_distribution(ctx, code)
        print("【フィルタ①: 当日資金分布】")
        print(f"  super_net : {fmt(d['super_net'])}")
        print(f"  big_net   : {fmt(d['big_net'])}")
        print(f"  mid_net   : {fmt(d['mid_net'])}")
        print(f"  small_net : {fmt(d['small_net'])}")
        print(f"  → ok(super>=0 AND big>=0): {d['ok']}")
        print()

        # ── フィルタ② ────────────────────────────────────────────────
        f = get_big_median(ctx, code)
        print("【フィルタ②: 標準版 (4/5日プラス)】")
        print(f"  big_med5(super中央値): {fmt(f['big_med5'])}")
        print(f"  → ok: {f['ok_strict']}")
        print()

        print("【フィルタ②: 改善版 (median>0)】")
        print(f"  big_med5(super中央値): {fmt(f['big_med5'])}")
        print(f"  → ok: {f['ok']}")
        print()

        # ── 生データ確認 ──────────────────────────────────────────────
        ret, data = ctx.get_capital_flow(code, period_type=PeriodType.DAY)
        if ret == RET_OK and not data.empty:
            tail = data.tail(BIG_MED_DAYS)
            print(f"【過去{BIG_MED_DAYS}日の日次フロー】")
            print(f"  {'日付':<12} {'super_in':>15} {'big_in':>15} {'super+big':>15} {'正負'}")
            for _, row in tail.iterrows():
                s = float(row.get('super_in_flow', 0) or 0)
                b = float(row.get('big_in_flow', 0) or 0)
                sb = s + b
                sign = '+' if sb > 0 else '-' if sb < 0 else '0'
                print(f"  {str(row.get('capital_flow_item_time','')):<12} {s:>+15,.0f} {b:>+15,.0f} {sb:>+15,.0f}  [{sign}]")
            vals = [float(r.get('super_in_flow', 0) or 0) + float(r.get('big_in_flow', 0) or 0)
                    for _, r in tail.iterrows()]
            super_vals = [float(r.get('super_in_flow', 0) or 0) for _, r in tail.iterrows()]
            print(f"\n  median(super+big) : {fmt(statistics.median(vals))}")
            print(f"  median(super)     : {fmt(statistics.median(super_vals))}")
            pos = sum(1 for v in vals if v > 0)
            print(f"  プラス日数        : {pos}/{len(vals)}")
        print()

        # ── 売買代金(スナップショット) ─────────────────────────────────
        ret2, snap = ctx.get_market_snapshot([code])
        if ret2 == RET_OK and not snap.empty:
            tov = float(snap.iloc[0].get('turnover', 0) or 0)
            print(f"【当日売買代金】")
            print(f"  turnover : {tov:>15,.0f}")
            if tov > 0:
                ratio_current = (d['super_net'] + d['big_net'] * 0.5) / tov
                print(f"  改善版ソートスコア (super + big*0.5) / tov : {ratio_current:.4f}")
            print()

        # ── 総合判定 ──────────────────────────────────────────────────
        print("【総合判定】")
        print(f"  標準版: フィルタ① {d['ok']} × フィルタ② {f['ok_strict']} → {'通過 ✓' if d['ok'] and f['ok_strict'] else '除外 ✗'}")
        print(f"  改善版: フィルタ① {d['ok']} × フィルタ② {f['ok']} → {'通過 ✓' if d['ok'] and f['ok'] else '除外 ✗'}")

    finally:
        ctx.close()

if __name__ == '__main__':
    main()
