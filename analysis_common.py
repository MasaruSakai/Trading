#!/usr/bin/env python3
"""米国・日本、標準・改善版で共通の資金フロー判定ロジック。
ここを変更すれば全スクリプト(morning_analysis / japan_analysis / analysis_enhanced)に
反映される。フィルタ条件の二重管理(市場ごとの直し忘れ)を防ぐための集約モジュール。"""
import statistics
import time as _time
from moomoo import RET_OK, PeriodType

BIG_MED_DAYS = 5   # 過去何営業日で日次大口フローの中央値を取るか


def get_distribution(ctx, code):
    """当日の資金分布。各階層の純額(買い-売り)を返す。
    合格(ok) = 超大口・大口がともに売り越していない (net >= 0)。
    mid_net / small_net は改善版で使用(標準版は無視してよい)。"""
    ret = -1
    for attempt in range(2):
        ret, data = ctx.get_capital_distribution(code)
        if ret == RET_OK and not data.empty:
            r = data.iloc[0]
            def n(a, b):
                return float(r.get(a, 0) or 0) - float(r.get(b, 0) or 0)
            sn = n('capital_in_super', 'capital_out_super')
            bn = n('capital_in_big',   'capital_out_big')
            mn = n('capital_in_mid',   'capital_out_mid')
            ln = n('capital_in_small', 'capital_out_small')
            return {'super_net': sn, 'big_net': bn, 'mid_net': mn, 'small_net': ln,
                    'ok': (sn + bn * 0.5) >= 0}
        if attempt == 0:
            _time.sleep(2.0)
    return {'super_net': 0, 'big_net': 0, 'mid_net': 0, 'small_net': 0, 'ok': False}


def get_big_median(ctx, code, days=BIG_MED_DAYS):
    """過去N営業日の日次(super+big)フローの継続性を判定。1回のAPI呼び出しで両判定を返す。
    ok_strict（標準版）: 5日中4日以上プラス必須。件数不足時は全日プラス必須。
    ok_loose（改善版）: 中央値 > 0 のみ（よりハイリスク・ニュース反応銘柄を拾いやすい）。
    sell_strict: 5日中4日以上が <= 0。件数不足時は全日 <= 0。
    sell_median: 中央値 <= 0。
    big_med5 は表示用の超大口5日中央値。
    standard_sort_med5 は標準版参考出力の補正ソート用
    (超大口5日中央値 + 大口5日中央値*0.5 - 小口5日中央値*0.25)。"""
    ret = -1
    for attempt in range(2):
        ret, data = ctx.get_capital_flow(code, period_type=PeriodType.DAY)
        if ret == RET_OK and not data.empty:
            super_vals = [float(s or 0) for s in data['super_in_flow'].tail(days)]
            big_vals   = [float(b or 0) for b in data['big_in_flow'].tail(days)]
            small_vals = [float(s or 0) for s in data['small_in_flow'].tail(days)] \
                if 'small_in_flow' in data.columns else [0.0 for _ in super_vals]
            vals = [s + b for s, b in zip(super_vals, big_vals)]
            if vals:
                med = statistics.median(super_vals)
                big_med = statistics.median(big_vals)
                small_med = statistics.median(small_vals)
                standard_sort_med = med + big_med * 0.5 - small_med * 0.25
                flow_med = statistics.median(vals)
                pos_days = sum(1 for v in vals if v > 0)
                sell_days = sum(1 for v in vals if v <= 0)
                if len(vals) < days:
                    ok_strict = pos_days >= len(vals)
                    sell_strict = sell_days >= len(vals)
                else:
                    ok_strict = pos_days >= len(vals) - 1
                    sell_strict = sell_days >= len(vals) - 1
                ok_loose = flow_med > 0
                sell_median = flow_med <= 0
                return {'big_med5': med, 'big_component_med5': big_med,
                        'small_med5': small_med, 'standard_sort_med5': standard_sort_med,
                        'ok': ok_loose, 'ok_strict': ok_strict,
                        'sell_strict': sell_strict, 'sell_median': sell_median}
        if attempt == 0:
            _time.sleep(2.0)
    return {'big_med5': 0, 'big_component_med5': 0, 'small_med5': 0,
            'standard_sort_med5': 0, 'ok': False, 'ok_strict': False,
            'sell_strict': False, 'sell_median': False}
