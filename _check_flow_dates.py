#!/usr/bin/env python3
"""使い捨て・実測専用スクリプト（実測後に破棄する前提）。

目的:
  実測1: get_capital_flow(DAY) のレスポンス列名と日付列を確認し、
          tail(5) の最終行が「実行日(今日)」か「前日以前」かを目視判定する。
  実測2: get_market_snapshot の turnover が当日累積か前日値かを、
          request_history_kline(K_DAY) の最終行 turnover/日付と並べて目視判定する。

本番の共通モジュール(analysis_common.py 等)には一切影響しない独立スクリプト。
売買判定ロジックは含まない。純粋に API レスポンスを出力するだけ。

使い方:
  python _check_flow_dates.py
  python _check_flow_dates.py US.AAPL JP.7203   # 引数で銘柄を指定(省略時は固定リスト)
"""
import sys, os, time
from datetime import datetime, timedelta

# ── moomoo SDK path (既存スクリプトと同じ流儀) ──────────────────────────────
sys.path.insert(0, '/Users/masaru/.claude/skills/moomooapi/scripts')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from moomoo import OpenQuoteContext, RET_OK, PeriodType

OPEND_HOST = '127.0.0.1'
OPEND_PORT = 11111
CALL_INTERVAL = 1.05   # sec, レート制限(≤30/30s)対策

# 流動性の高い代表銘柄(既存ウォッチリストと同じ US.* / JP.* 形式)
DEFAULT_CODES = ['US.AAPL', 'US.QQQ', 'JP.7203', 'JP.9984']


def check_capital_flow(ctx, code, today_str):
    print(f"\n--- [実測1] capital_flow DAY : {code} ---")
    ret, data = ctx.get_capital_flow(code, period_type=PeriodType.DAY)
    if ret != RET_OK:
        print(f"  取得失敗: {data}")
        return
    if data.empty:
        print("  空のレスポンス")
        return
    print(f"  columns = {list(data.columns)}")
    # 日付列の候補を探す
    date_col = None
    for cand in ('time_key', 'capital_flow_item_time', 'date'):
        if cand in data.columns:
            date_col = cand
            break
    if date_col:
        tail_dates = list(data[date_col].tail(5))
        print(f"  日付列 '{date_col}' の直近5件 = {tail_dates}")
        last_date = str(tail_dates[-1])[:10] if tail_dates else ''
        verdict = "★当日と一致" if last_date == today_str else "前日以前(当日含まれず)"
        print(f"  最終行の日付 = {last_date} / 実行日(今日) = {today_str} -> {verdict}")
    else:
        print("  日付列が見つからない(上記 columns を確認)")
    # ── フロー内訳確認 ──────────────────────────────────────────────────────
    row = data.iloc[-1]
    super_val = float(row.get('super_in_flow', 0) or 0)
    big_val   = float(row.get('big_in_flow', 0) or 0)
    main_val  = float(row.get('main_in_flow', 0) or 0)
    in_flow   = float(row.get('in_flow', 0) or 0)
    print(f"  super_in_flow:       {super_val:,.0f}")
    print(f"  big_in_flow:         {big_val:,.0f}")
    print(f"  super + big:         {super_val + big_val:,.0f}")
    print(f"  main_in_flow:        {main_val:,.0f}")
    print(f"  一致: {abs((super_val + big_val) - main_val) < 1}")
    print(f"  in_flow (全合計):    {in_flow:,.0f}")


def check_snapshot_vs_kline(ctx, code, today_str):
    print(f"\n--- [実測2] snapshot.turnover vs kline : {code} ---")
    # snapshot
    rs, snap = ctx.get_market_snapshot([code])
    snap_tov = None
    if rs == RET_OK and not snap.empty:
        row = snap.iloc[0]
        snap_tov = float(row.get('turnover', 0) or 0)
        snap_ut = row.get('update_time', '(なし)')
        print(f"  snapshot: turnover = {snap_tov:,.0f} / update_time = {snap_ut}")
    else:
        print(f"  snapshot 取得失敗: {snap}")
    time.sleep(CALL_INTERVAL)
    # kline(直近5本)
    start = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
    end = today_str
    rk, kd, _ = ctx.request_history_kline(code, start=start, end=end,
                                          ktype='K_DAY', max_count=5)
    if rk == RET_OK and not kd.empty:
        last = kd.iloc[-1]
        k_date = str(last.get('time_key', ''))[:10]
        k_tov = float(last.get('turnover', 0) or 0)
        print(f"  kline 最終行: time_key = {k_date} / turnover = {k_tov:,.0f}")
        if snap_tov is not None:
            same = abs(snap_tov - k_tov) < 1e-6
            if k_date == today_str:
                note = ("snapshot==kline当日 なら当日値" if same
                        else "snapshot!=kline当日 -> snapshotは当日のリアルタイム累積の可能性")
            else:
                note = ("kline最終は前日。snapshot==前日値 なら前日、"
                        "異なれば snapshot は当日(取引時間外/開始前)の可能性")
            print(f"  目視判定ヒント: {note}")
    else:
        print(f"  kline 取得失敗: {kd}")


def main():
    codes = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_CODES
    today_str = datetime.now().strftime('%Y-%m-%d')
    print(f"実行日(ローカル) = {today_str}")
    print(f"確認対象 = {codes}")
    ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
    try:
        for code in codes:
            check_capital_flow(ctx, code, today_str)
            time.sleep(CALL_INTERVAL)
            check_snapshot_vs_kline(ctx, code, today_str)
            time.sleep(CALL_INTERVAL)
    finally:
        ctx.close()
    print("\n完了。出力の日付/turnover を目視で比較してください。")


if __name__ == '__main__':
    main()
