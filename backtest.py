#!/usr/bin/env python3
"""
シグナル検証ループ (フォワードリターン計測)
================================================
目的: 「大口net買い越し」シグナルが翌寄り/翌引けを実際に当てているかを数値化し、
      売買の再現性を測る。

やること:
  1. logs/ 内の分析ログ(analysis_*.log = 米国, japan_*.log = 日本)をパースし、
     各実行で合格した銘柄(シグナル)を抽出する。
  2. 各シグナルについて日足を取得し、
       - シグナル日の終値バー → 翌バーの寄り (= 翌寄りギャップ。狙いの主指標)
       - シグナル日の終値 → 翌引け (= 1日保有)
     のフォワードリターンを計算する。
  3. グループ別・全体の的中率(翌寄りが上)と平均リターンを集計し、
     ベンチマーク(米国: QQQ)と比較する。

シグナル日の特定は、タイムゾーン推定ではなく日足バーから行う:
  「実行時刻(その市場ローカル)の日付 D 以前の最後の日足バー」をシグナル終値バーとし、
  その次のバーをフォワードとする。米国の引け前実行(ET 15:00)も、日本の寄り前実行も
  これで自動的に正しい基準日に揃う。

使い方:
  python3 backtest.py
  python3 backtest.py --market us          # 米国のみ
  python3 backtest.py --exclude-etf        # ETF(QQQ等)を除外して集計
  python3 backtest.py --benchmark US.QQQ   # 米国ベンチマーク変更
"""
import sys, os, re, csv, time, argparse
from datetime import datetime, timedelta
from collections import defaultdict
from zoneinfo import ZoneInfo

sys.path.insert(0, '/Users/masaru/.claude/skills/moomooapi/scripts')
from moomoo import OpenQuoteContext, RET_OK

OPEND_HOST, OPEND_PORT = '127.0.0.1', 11111
LOG_DIR = '/Users/masaru/Projects/Trading/logs'
RESULTS_CSV = os.path.join(LOG_DIR, 'backtest_results.csv')
CALL_INTERVAL = 1.05

# 市場ごとのローカルTZ(実行時刻→基準日の解釈に使用)
MARKET_TZ = {'us': ZoneInfo('America/New_York'), 'jp': ZoneInfo('Asia/Tokyo')}
# よくあるETF/インデックス(--exclude-etf 用の簡易判定)
ETF_HINT = {'QQQ', 'QQQM', 'DIA', 'SPY', 'XLF', 'XLB', 'XLE', 'XLK', 'XLV', 'XLI',
            'XLP', 'XLU', 'XLRE', 'XLY', 'IWM', 'VOO', 'IVV', 'JP.1321', 'JP.1306',
            'JP.1671', 'JP.1326', 'JP.2039', 'JP.1545', 'JP.2638'}

HEADER_RE = re.compile(r'(Morning Capital Flow|Japan Market Capital Flow).*?'
                       r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
GROUP_RE  = re.compile(r'【(.+?)】')
CODE_RE   = re.compile(r'^\s+((?:US|JP|HK)\.\w+)\s')


# ── ログ解析 ──────────────────────────────────────────────────────────────────

def parse_log(path):
    """1ファイルから (market, run_dt, [(group, code), ...]) を返す。失敗時 None。"""
    market = 'us' if os.path.basename(path).startswith('analysis_') else \
             'jp' if os.path.basename(path).startswith('japan_') else None
    if market is None:
        return None
    try:
        text = open(path, encoding='utf-8').read()
    except Exception:
        return None
    mh = HEADER_RE.search(text)
    if not mh:
        return None
    run_dt = datetime.strptime(mh.group(2), '%Y-%m-%d %H:%M:%S')

    pairs, group = [], None
    for line in text.splitlines():
        mg = GROUP_RE.search(line)
        if mg:
            group = mg.group(1)
            continue
        mc = CODE_RE.match(line)
        if mc and group:
            pairs.append((group, mc.group(1)))
    return market, run_dt, pairs


SIGNALS_CSV = os.path.join(LOG_DIR, 'signals.csv')


def _run_date(market, run_dt):
    """実行時刻(東京naive)を市場ローカル日付に変換。"""
    return run_dt.replace(tzinfo=ZoneInfo('Asia/Tokyo')) \
                 .astimezone(MARKET_TZ[market]).date().isoformat()


def collect_signals():
    """シグナルを集約。 (market, signal_date_str, group, code) のユニーク行を返す。
    優先順位: 構造化CSV(signals.csv) があればそれ + 整形ログ も併合(重複は自動排除)。"""
    rows = {}  # key=(market, run_date_local, group, code) → True

    # 1) 構造化CSV (堅牢・本命)
    if os.path.exists(SIGNALS_CSV):
        try:
            with open(SIGNALS_CSV, encoding='utf-8') as fp:
                for r in csv.DictReader(fp):
                    market = r.get('market')
                    if market not in MARKET_TZ:
                        continue
                    run_dt = datetime.strptime(r['run_ts'], '%Y-%m-%d %H:%M:%S')
                    rows[(market, _run_date(market, run_dt),
                          r.get('group', ''), r.get('code', ''))] = True
        except Exception as e:
            print(f"  [warn] signals.csv 読込失敗: {e}")

    # 2) 整形ログ (CSV以前の履歴のブートストラップ用)
    for f in sorted(x for x in os.listdir(LOG_DIR) if x.endswith('.log')):
        parsed = parse_log(os.path.join(LOG_DIR, f))
        if not parsed:
            continue
        market, run_dt, pairs = parsed
        for group, code in pairs:
            rows[(market, _run_date(market, run_dt), group, code)] = True

    return sorted(rows.keys())


# ── 日足取得 & フォワードリターン ─────────────────────────────────────────────

def fetch_klines(ctx, code, start, end):
    ret, data, _ = ctx.request_history_kline(
        code, start=start, end=end, ktype='K_DAY', max_count=400)
    if ret != RET_OK or data.empty:
        return []
    out = []
    for _, r in data.iterrows():
        out.append({
            'date': str(r['time_key'])[:10],
            'open': float(r['open']), 'close': float(r['close']),
        })
    return out


def forward_returns(bars, ref_date):
    """ref_date 以前の最後のバー(シグナル終値)と、その次のバー(翌寄り/翌引け)から
    リターンを計算。評価不能(翌バー未到来等)は None。"""
    idx = None
    for i, b in enumerate(bars):
        if b['date'] <= ref_date:
            idx = i
        else:
            break
    if idx is None or idx + 1 >= len(bars):
        return None
    sig, nxt = bars[idx], bars[idx + 1]
    c0 = sig['close']
    if c0 <= 0:
        return None
    return {
        'sig_date': sig['date'], 'fwd_date': nxt['date'],
        'ret_gap': nxt['open'] / c0 - 1.0,       # 終値→翌寄り(主指標)
        'ret_cc':  nxt['close'] / c0 - 1.0,      # 終値→翌引け(1日保有)
        'ret_oc':  nxt['close'] / nxt['open'] - 1.0,  # 翌寄り→翌引け(寄りで入る場合)
    }


# ── 集計 ──────────────────────────────────────────────────────────────────────

def summarize(name, results):
    if not results:
        print(f"  {name:<16} 評価可能シグナルなし")
        return None
    n = len(results)
    gaps = [r['ret_gap'] for r in results]
    ccs  = [r['ret_cc']  for r in results]
    hit  = sum(1 for g in gaps if g > 0) / n
    mean_gap = sum(gaps) / n
    mean_cc  = sum(ccs) / n
    print(f"  {name:<16} n={n:<4} 翌寄り的中率={hit*100:5.1f}%  "
          f"平均翌寄り={mean_gap*100:+6.2f}%  平均翌引け={mean_cc*100:+6.2f}%")
    return {'n': n, 'hit': hit, 'mean_gap': mean_gap, 'mean_cc': mean_cc}


def main():
    ap = argparse.ArgumentParser(description='シグナル フォワードリターン検証')
    ap.add_argument('--market', choices=['us', 'jp'], help='市場を限定')
    ap.add_argument('--exclude-etf', action='store_true', help='ETF/インデックスを除外')
    ap.add_argument('--benchmark', default='US.QQQ', help='米国ベンチマーク(default US.QQQ)')
    args = ap.parse_args()

    signals = collect_signals()
    if args.market:
        signals = [s for s in signals if s[0] == args.market]
    if args.exclude_etf:
        signals = [s for s in signals
                   if s[3] not in ETF_HINT and s[3].split('.')[-1] not in ETF_HINT]

    if not signals:
        print("シグナルが見つかりません(logs/ に分析ログがあるか確認)")
        return

    codes = sorted(set(s[3] for s in signals))
    min_date = min(s[1] for s in signals)
    start = (datetime.fromisoformat(min_date) - timedelta(days=10)).strftime('%Y-%m-%d')
    end = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')

    print(f"\n{'='*72}")
    print(f"  シグナル検証 (フォワードリターン)")
    print(f"  シグナル件数: {len(signals)}  ユニーク銘柄: {len(codes)}  "
          f"期間: {min_date}〜")
    print(f"{'='*72}\n")
    print(f"  日足取得中 ({len(codes)}銘柄 + ベンチマーク)...", flush=True)

    ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
    klines = {}
    try:
        for code in codes + [args.benchmark]:
            if code in klines:
                continue
            klines[code] = fetch_klines(ctx, code, start, end)
            time.sleep(CALL_INTERVAL)
    finally:
        ctx.close()

    # 各シグナル評価
    per_signal, by_group, by_market, pending = [], defaultdict(list), defaultdict(list), 0
    overall_dates_us = set()
    for market, sig_date, group, code in signals:
        fr = forward_returns(klines.get(code, []), sig_date)
        if fr is None:
            pending += 1
            continue
        rec = {'market': market, 'group': group, 'code': code, **fr}
        per_signal.append(rec)
        by_group[(market, group)].append(rec)
        by_market[market].append(rec)
        if market == 'us':
            overall_dates_us.add(fr['sig_date'])

    # 結果保存
    if per_signal:
        with open(RESULTS_CSV, 'w', newline='', encoding='utf-8') as fp:
            w = csv.DictWriter(fp, fieldnames=['market', 'group', 'code', 'sig_date',
                              'fwd_date', 'ret_gap', 'ret_cc', 'ret_oc'])
            w.writeheader()
            w.writerows(per_signal)

    # 出力
    print(f"\n  評価可能: {len(per_signal)}件 / 翌バー未到来(保留): {pending}件\n")
    print(f"{'─'*72}")
    print("  ◆ 市場別")
    for m in ('us', 'jp'):
        if by_market[m]:
            summarize(f'{m.upper()} 全体', by_market[m])

    print(f"\n{'─'*72}")
    print("  ◆ グループ別")
    for (m, g), recs in sorted(by_group.items()):
        summarize(f'[{m}] {g}', recs)

    # ベンチマーク比較(米国: シグナルが出た営業日のQQQ翌寄り/翌引け)
    if by_market['us'] and overall_dates_us:
        bbars = klines.get(args.benchmark, [])
        bench = [forward_returns(bbars, d) for d in sorted(overall_dates_us)]
        bench = [b for b in bench if b]
        print(f"\n{'─'*72}")
        print(f"  ◆ ベンチマーク {args.benchmark} (同一シグナル営業日)")
        summarize(args.benchmark, [{'ret_gap': b['ret_gap'], 'ret_cc': b['ret_cc']}
                                   for b in bench])
        us = by_market['us']
        edge_gap = sum(r['ret_gap'] for r in us)/len(us) - \
                   (sum(b['ret_gap'] for b in bench)/len(bench) if bench else 0)
        print(f"\n  → シグナルの翌寄り超過リターン(対{args.benchmark}): {edge_gap*100:+.2f}%/件")

    if per_signal:
        print(f"\n  明細CSV: {RESULTS_CSV}")
    print()


if __name__ == '__main__':
    main()
