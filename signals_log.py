#!/usr/bin/env python3
"""
構造化シグナル出力 (CSV追記)
分析スクリプトが「合格した銘柄」を1行1シグナルで logs/signals.csv に追記する。
backtest.py はこの CSV を読んでフォワードリターンを計算する(整形ログのパース不要)。

列:
  run_ts     実行時刻 (YYYY-MM-DD HH:MM:SS, Macローカル)
  market     us / jp
  variant    分析バリアント名 (標準は 'base'。改善版を増やしたら識別に使う)
  group      ウォッチリスト/保有 等のグループ名
  code       銘柄コード
  super_net  超大口ネット
  big_net    大口ネット
  week_big   週次大口
  turnover   売買代金
"""
import os, csv

FIELDS = ['run_ts', 'market', 'variant', 'group', 'code',
          'super_net', 'big_net', 'week_big', 'turnover',
          # 改善版で使う追加指標。vwap_dev は既存CSV互換のため列名を維持。
          'vwap_dev', 'ingest_ratio', 'big_med5', 'surge', 'small_dom', 'is_etf',
          'bear_etf', 'bear_etf_code',
          'ext_dev', 'ext_sess']
CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', 'signals.csv')


def append_signals(market, run_dt, group_candidates, variant='base'):
    """group_candidates: {group_name: [candidate_dict, ...]} を CSV に追記。
    candidate_dict は code/super_net/big_net/week_big/turnover を持つ。"""
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    new_file = not os.path.exists(CSV_PATH) or os.path.getsize(CSV_PATH) == 0
    run_ts = run_dt.strftime('%Y-%m-%d %H:%M:%S')
    try:
        with open(CSV_PATH, 'a', newline='', encoding='utf-8') as fp:
            # extrasaction='ignore': candidate dict に余計なキーがあっても無視
            w = csv.DictWriter(fp, fieldnames=FIELDS, extrasaction='ignore')
            if new_file:
                w.writeheader()
            for group, cands in group_candidates.items():
                for c in cands:
                    row = {'run_ts': run_ts, 'market': market, 'variant': variant,
                           'group': group}
                    # candidate の該当キーをそのまま採用(無いものは空欄)
                    for k in FIELDS:
                        if k in c:
                            row[k] = c[k]
                    w.writerow(row)
        return CSV_PATH
    except Exception as e:
        print(f"  [signals] CSV追記失敗: {e}")
        return None
