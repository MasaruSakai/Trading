#!/usr/bin/env python3
"""ETF判定の共通ヘルパ。
moomoo から市場の全ETFコード集合を取得する(固定リストは漏れるため動的取得)。"""
from moomoo import RET_OK, SecurityType, Market

MOOMOO_MARKET = {'us': Market.US, 'jp': Market.JP}


def fetch_etf_set(ctx, market):
    """市場の全ETFコード集合を返す。取得失敗時は空集合。"""
    try:
        r, d = ctx.get_stock_basicinfo(MOOMOO_MARKET[market], SecurityType.ETF)
        if r == RET_OK and not d.empty:
            return set(d['code'].tolist())
    except Exception as e:
        print(f"  [etf] 動的取得失敗: {e}")
    return set()
