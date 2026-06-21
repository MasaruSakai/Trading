#!/usr/bin/env python3
"""
Japan Market Capital Flow Analysis
条件: 超大口 & 大口 が売り越していない (買い-売り >= 0, capital_distribution, 当日)
    + 過去5営業日の日次大口フロー(big_in_flow)の中央値 > 0 (capital_flow daily)
出力: ETFのみ・大口中央5d降順
出力: 日本ハイテク / 日本セクター / 日本市場国外 / 日本市場コモディティ (各TOP5)

使い方:
  python japan_analysis.py
  python japan_analysis.py --top 3
  python japan_analysis.py --workers 4
"""
import sys, time, argparse
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, '/Users/masaru/.claude/skills/moomooapi/scripts')
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from moomoo import OpenQuoteContext, RET_OK, PeriodType
from common import create_quote_context
from analysis_common import get_distribution, get_big_median

# ── Config ───────────────────────────────────────────────────────────────────
OPEND_HOST = '127.0.0.1'
OPEND_PORT  = 11111

WATCHLISTS = ['日本ハイテク', '日本セクター', '日本市場国外', '日本市場コモディティ']

CALL_INTERVAL  = 1.05   # sec between calls per worker (≤ 30/30s)
SNAPSHOT_BATCH = 200
TOP_N_DEFAULT  = 6
NUM_WORKERS    = 4


# ── Worker helpers ────────────────────────────────────────────────────────────

# 大口判定の中核(get_distribution / get_big_median)は analysis_common に集約。

def _worker(codes_slice):
    """Opens own context (create_quote_context for JP), processes slice."""
    ctx = create_quote_context()
    results = {}
    try:
        for code in codes_slice:
            dist = get_distribution(ctx, code)
            time.sleep(CALL_INTERVAL)
            flow = None
            if dist['ok']:
                flow = get_big_median(ctx, code)
                time.sleep(CALL_INTERVAL)
            results[code] = {'dist': dist, 'flow': flow}
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    return results


# ── Print helpers ─────────────────────────────────────────────────────────────

def _fmt(v, width=15):
    return f"{v:>{width},.0f}"


def _print_group(label, candidates, top_n, total):
    display = candidates if top_n is None else candidates[:top_n]
    suffix  = "全合格" if top_n is None else f"TOP{top_n}"
    print(f"\n  【{label}】{suffix}  ({len(candidates)}銘柄合格 / {total}銘柄中)")
    if not display:
        print("    条件を満たす銘柄なし")
        return
    hdr = f"    {'Code':<12} {'超大口Net':>15} {'大口Net':>15} {'大口中央5d':>15} {'売買代金':>18}"
    print(hdr)
    print("    " + "-" * 79)
    for r in display:
        tv_str = f"{r['turnover']:>18,.0f}" if r['turnover'] > 0 else f"{'(データなし)':>18}"
        print(f"    {r['code']:<12} {_fmt(r['super_net'])} {_fmt(r['big_net'])}"
              f" {_fmt(r['big_med5'])} {tv_str}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main(top_n=TOP_N_DEFAULT, num_workers=NUM_WORKERS):
    t0 = datetime.now()
    print(f"\n{'='*72}")
    print(f"  Japan Market Capital Flow Analysis  {t0.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*72}\n")

    # ── Step 1: watchlists + snapshot ─────────────────────────────────────────
    print("  [1/3] ウォッチリスト & スナップショット取得中...", end=' ', flush=True)
    q_main = create_quote_context()
    groups = {}
    for g in WATCHLISTS:
        ret, data = q_main.get_user_security(g)
        groups[g] = data['code'].tolist() if ret == RET_OK and not data.empty else []
        time.sleep(0.2)

    all_codes = sorted(set(c for v in groups.values() for c in v))

    # Batch snapshot for turnover
    turnover = {}
    for i in range(0, len(all_codes), SNAPSHOT_BATCH):
        batch = all_codes[i : i + SNAPSHOT_BATCH]
        ret3, snap = q_main.get_market_snapshot(batch)
        if ret3 == RET_OK and not snap.empty:
            for _, row in snap.iterrows():
                c = str(row.get('code', ''))
                t = float(row.get('turnover', 0) or 0)
                if t > 0:
                    turnover[c] = t
        time.sleep(0.3)
    from etf_util import fetch_etf_set
    etf_set = fetch_etf_set(q_main, 'jp')
    q_main.close()

    group_summary = "  /  ".join(f"{g}: {len(codes)}銘柄" for g, codes in groups.items())
    print(f"{len(all_codes)}銘柄ユニーク / ETF判定 {len(etf_set)}件")
    print(f"         {group_summary}")
    print(f"         売買代金取得: {len(turnover)}銘柄")

    # ── Step 2: parallel analysis ──────────────────────────────────────────────
    print(f"  [2/3] {len(all_codes)}銘柄を{num_workers}並列で分析中...", flush=True)

    slices = [[] for _ in range(num_workers)]
    for i, code in enumerate(all_codes):
        slices[i % num_workers].append(code)

    all_results = {}
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {executor.submit(_worker, s): idx for idx, s in enumerate(slices)}
        for future in as_completed(futures):
            all_results.update(future.result())

    passing = [c for c, r in all_results.items()
               if r['dist']['ok'] and r.get('flow') and r['flow']['ok']]
    print(f"         完了: {len(passing)}銘柄が条件クリア")

    # Fallback turnover for passing stocks with 0
    zero_passing = [c for c in passing if turnover.get(c, 0) == 0]
    if zero_passing:
        print(f"  売買代金補完中 ({len(zero_passing)}銘柄)...", end=' ', flush=True)
        end_date   = datetime.now().strftime('%Y-%m-%d')
        start_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        fb_ctx = create_quote_context()
        for code in zero_passing:
            ret_k, kdata, _ = fb_ctx.request_history_kline(
                code, start=start_date, end=end_date, ktype='K_DAY', max_count=5)
            if ret_k == RET_OK and not kdata.empty:
                t = float(kdata.iloc[-1].get('turnover', 0) or 0)
                if t > 0:
                    turnover[code] = t
            time.sleep(0.5)
        fb_ctx.close()
        print("完了")

    # ── Step 3: results ───────────────────────────────────────────────────────
    print("  [3/3] 結果整形中...", flush=True)

    # 日本市場はETFのみ表示(少額のため100株単位の個別株を売買できず、ETFで対応するため)。
    # ただしETF判定の取得に失敗(空集合)した場合は全件表示にフォールバック。
    etf_only = bool(etf_set)

    def build_candidates(codes):
        out = []
        for code in codes:
            if etf_only and code not in etf_set:
                continue
            r = all_results.get(code, {})
            d = r.get('dist', {})
            f = r.get('flow') or {}
            if d.get('ok') and f.get('ok'):
                out.append({
                    'code':      code,
                    'super_net': d['super_net'],
                    'big_net':   d['big_net'],
                    'big_med5':  f['big_med5'],
                    'turnover':  turnover.get(code, 0),
                })
        out.sort(key=lambda x: x['big_med5'], reverse=True)
        return out

    elapsed = (datetime.now() - t0).total_seconds()
    print(f"\n{'='*72}")
    print(f"  分析結果  ({datetime.now().strftime('%H:%M:%S')}  経過: {elapsed:.0f}秒)")
    print(f"{'='*72}")

    if etf_only:
        print("  ※ ETFのみ表示(少額のためETFで対応)")
    group_cands = {g: build_candidates(groups[g]) for g in WATCHLISTS}
    for g in WATCHLISTS:
        total = sum(1 for c in groups[g] if c in etf_set) if etf_only else len(groups[g])
        _print_group(g, group_cands[g], top_n=top_n, total=total)

    # 構造化シグナルをCSVに追記(検証ループ用)
    try:
        from signals_log import append_signals
        path = append_signals('jp', t0, group_cands, variant='base')
        if path:
            print(f"  [signals] CSV追記: {sum(len(v) for v in group_cands.values())}行 → {path}")
    except Exception as e:
        print(f"  [signals] 追記スキップ: {e}")

    print(f"\n  {'='*68}")
    print(f"  合計所要時間: {elapsed:.1f}秒\n")


GDRIVE_LOG_DIR = '/Users/masaru/Library/CloudStorage/GoogleDrive-sbrmsj@gmail.com/マイドライブ/AssetManagement/日本logs'


def _copy_to_gdrive(log_path):
    import os, shutil
    try:
        os.makedirs(GDRIVE_LOG_DIR, exist_ok=True)
        dest = os.path.join(GDRIVE_LOG_DIR, os.path.basename(log_path))
        shutil.copy2(log_path, dest)
        print(f"  [GDrive] コピー完了: {dest}")
    except Exception as e:
        print(f"  [GDrive] コピー失敗: {e}")


def _notify(title, message):
    import subprocess, shlex
    try:
        subprocess.run(['osascript', '-e',
                        f'display notification {shlex.quote(message)} with title {shlex.quote(title)}'],
                       check=False, timeout=5)
    except Exception:
        pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Japan Market Capital Flow Analysis')
    parser.add_argument('--top',     type=int, default=TOP_N_DEFAULT, help='各グループ上位件数 (default: 6)')
    parser.add_argument('--workers', type=int, default=NUM_WORKERS,   help='並列スレッド数 (default: 4)')
    parser.add_argument('--log-dir', default='/Users/masaru/Projects/Trading/logs',
                        help='ログ出力ディレクトリ')
    parser.add_argument('--no-log',    action='store_true', help='ログファイル出力を無効化')
    parser.add_argument('--no-notify', action='store_true', help='macOS通知を無効化')
    args = parser.parse_args()

    import os, io

    if not args.no_log:
        os.makedirs(args.log_dir, exist_ok=True)
        log_path = os.path.join(args.log_dir,
                                f"japan_{datetime.now().strftime('%Y%m%d_%H%M')}.log")

        class Tee:
            def __init__(self, *files): self.files = files
            def write(self, obj):
                for f in self.files: f.write(obj); f.flush()
            def flush(self):
                for f in self.files: f.flush()

        log_file = open(log_path, 'w', encoding='utf-8')
        sys.stdout = Tee(sys.__stdout__, log_file)

    try:
        main(top_n=args.top, num_workers=args.workers)
        if not args.no_log:
            log_file.flush()
            _copy_to_gdrive(log_path)
        if not args.no_notify:
            _notify('📈 日本市場分析 完了', '結果を確認してください')
    except Exception as e:
        print(f'エラー: {e}')
        if not args.no_notify:
            _notify('⚠️ 日本市場分析 失敗', str(e))
        raise
    finally:
        if not args.no_log:
            sys.stdout = sys.__stdout__
            log_file.close()
