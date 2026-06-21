#!/usr/bin/env python3
"""
Morning Capital Flow Analysis  /  毎朝実行スクリプト
条件: 超大口 & 大口 が売り越していない (買い-売り >= 0, capital_distribution, 当日)
    + 過去5営業日の日次大口フロー(big_in_flow)の中央値 > 0 (capital_flow daily)
出力: 保有銘柄(全件) + 米ハイテク/米国銘柄/米国セクター(上位6件 / 大口中央5d降順)

使い方:
  python morning_analysis.py
  python morning_analysis.py --top 3         # 各グループ上位N件変更
  python morning_analysis.py --workers 6     # 並列数変更(デフォルト4)
"""
import sys, time, argparse, os, shutil
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo

# ── moomoo SDK path ──────────────────────────────────────────────────────────
sys.path.insert(0, '/Users/masaru/.claude/skills/moomooapi/scripts')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from moomoo import OpenQuoteContext, RET_OK, PeriodType, TrdEnv
from common import create_trade_context, parse_security_firm
from analysis_common import get_distribution, get_big_median

# ── Config ───────────────────────────────────────────────────────────────────
OPEND_HOST = '127.0.0.1'
OPEND_PORT  = 11111

HOLDINGS_ACC_ID   = 284852706236374484
HOLDINGS_FIRM_STR = 'FUTUJP'

WATCHLISTS = ['米ハイテク', '米国銘柄', '米国セクター']

# 元々ETFで構成されるグループはETF分離せず、そのグループ内でそのまま表示する
ETF_NATIVE_WATCHLISTS = {'米国セクター'}

CALL_INTERVAL = 1.05      # sec between calls per worker thread (≤ 30/30s)
SNAPSHOT_BATCH = 200      # max codes per get_market_snapshot call


# ── Worker helpers (each thread owns one OpenQuoteContext) ────────────────────
# 大口判定の中核(get_distribution / get_big_median)は analysis_common に集約。

def _worker(codes_slice):
    """Opens own context, processes assigned slice, returns dict of results."""
    ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
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

def _fmt(v, width=16):
    return f"{v:>{width},.0f}"


def _print_group(label, candidates, top_n, total):
    display = candidates if top_n is None else candidates[:top_n]
    suffix  = "全合格" if top_n is None else f"TOP{top_n}"
    print(f"\n  【{label}】{suffix}  ({len(candidates)}銘柄合格 / {total}銘柄中)")
    if not display:
        print("    条件を満たす銘柄なし")
        return
    hdr = f"    {'Code':<10} {'超大口Net':>15} {'大口Net':>15} {'大口中央5d':>15} {'売買代金':>18}"
    print(hdr)
    print("    " + "-" * 77)
    for r in display:
        tv_str = f"{r['turnover']:>18,.0f}" if r['turnover'] > 0 else f"{'(データなし)':>18}"
        print(f"    {r['code']:<10} {_fmt(r['super_net'],15)} {_fmt(r['big_net'],15)}"
              f" {_fmt(r['big_med5'],15)} {tv_str}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main(top_n=5, num_workers=4):
    t0 = datetime.now()
    print(f"\n{'='*72}")
    print(f"  Morning Capital Flow Analysis  {t0.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*72}\n")

    # ── Step 1: holdings ──────────────────────────────────────────────────────
    print("  [1/4] 保有銘柄取得中...", end=' ', flush=True)
    trd = create_trade_context(None, security_firm=parse_security_firm(HOLDINGS_FIRM_STR))
    ret, pos = trd.position_list_query(
        trd_env=TrdEnv.REAL, acc_id=HOLDINGS_ACC_ID,
        refresh_cache=True)
    trd.close()
    holdings = []
    if ret == RET_OK and not pos.empty:
        for _, row in pos.iterrows():
            if float(row.get('qty', 0) or 0) > 0:
                holdings.append(str(row.get('code', '')))
    print(f"{len(holdings)}銘柄")

    # ── Step 2: watchlists + snapshot ─────────────────────────────────────────
    print("  [2/4] ウォッチリスト & スナップショット取得中...", end=' ', flush=True)
    q_main = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
    groups = {'保有銘柄': holdings}
    for g in WATCHLISTS:
        ret2, data = q_main.get_user_security(g)
        groups[g] = data['code'].tolist() if ret2 == RET_OK and not data.empty else []
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
    etf_set = fetch_etf_set(q_main, 'us')
    q_main.close()
    print(f"{len(all_codes)}銘柄ユニーク / 売買代金取得: {len(turnover)}銘柄 / ETF判定 {len(etf_set)}件")

    # ── Step 3: parallel analysis ──────────────────────────────────────────────
    print(f"  [3/4] {len(all_codes)}銘柄を{num_workers}並列で分析中...", flush=True)

    # Round-robin split across workers
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

    # Fallback turnover fetch for passing stocks with 0 turnover
    zero_passing = [c for c in passing if turnover.get(c, 0) == 0]
    if zero_passing:
        print(f"  売買代金補完中 ({len(zero_passing)}銘柄)...", end=' ', flush=True)
        from datetime import timedelta
        end_date   = datetime.now().strftime('%Y-%m-%d')
        start_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        fb_ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
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

    # ── Step 4: results ───────────────────────────────────────────────────────
    print("  [4/4] 結果整形中...", flush=True)

    def build_candidates(codes):
        out = []
        for code in codes:
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

    # ETF分離: 各グループは個別株のみ表示、ETFは別セクションに集約(除外しない)
    def split_etf(cands):
        ind = [c for c in cands if c['code'] not in etf_set]
        etf = [c for c in cands if c['code'] in etf_set]
        return ind, etf

    def ind_total(codes):
        return len([c for c in codes if c not in etf_set])

    group_cands = {}
    etf_all = []
    h_all = build_candidates(groups['保有銘柄'])
    h_ind, h_etf = split_etf(h_all)
    group_cands['保有銘柄'] = h_all
    etf_all += h_etf
    _print_group('保有銘柄', h_all, top_n=None, total=len(groups['保有銘柄']))
    for g in WATCHLISTS:
        cands = build_candidates(groups[g])
        if g in ETF_NATIVE_WATCHLISTS:        # 元々ETF構成: 分離せずそのまま表示
            group_cands[g] = cands
            _print_group(g, cands, top_n=top_n, total=len(groups[g]))
        else:
            ind, etf = split_etf(cands)
            group_cands[g] = ind
            etf_all += etf
            _print_group(g, ind, top_n=top_n, total=ind_total(groups[g]))

    # ETFセクション(全グループから集約・重複排除・売買代金降順)
    seen, etf_uniq = set(), []
    for c in etf_all:
        if c['code'] not in seen:
            seen.add(c['code'])
            etf_uniq.append(c)
    etf_uniq.sort(key=lambda x: x['big_med5'], reverse=True)
    group_cands['ETF(参考)'] = etf_uniq
    _print_group('ETF(参考・分散用)', etf_uniq, top_n=None,
                 total=sum(1 for c in all_codes if c in etf_set))

    # 構造化シグナルをCSVに追記(検証ループ用)
    try:
        from signals_log import append_signals
        path = append_signals('us', t0, group_cands, variant='base')
        if path:
            print(f"  [signals] CSV追記: {sum(len(v) for v in group_cands.values())}行 → {path}")
    except Exception as e:
        print(f"  [signals] 追記スキップ: {e}")

    print(f"\n  {'='*68}")
    print(f"  合計所要時間: {elapsed:.1f}秒\n")


GDRIVE_LOG_DIR = '/Users/masaru/Library/CloudStorage/GoogleDrive-sbrmsj@gmail.com/マイドライブ/AssetManagement/米国logs'


def _copy_to_gdrive(log_path):
    try:
        os.makedirs(GDRIVE_LOG_DIR, exist_ok=True)
        dest = os.path.join(GDRIVE_LOG_DIR, os.path.basename(log_path))
        shutil.copy2(log_path, dest)
        print(f"  [GDrive] コピー完了: {dest}")
    except Exception as e:
        print(f"  [GDrive] コピー失敗: {e}")


def _notify(title, message):
    """macOS desktop notification via osascript."""
    import subprocess, shlex
    script = f'display notification {shlex.quote(message)} with title {shlex.quote(title)}'
    try:
        subprocess.run(['osascript', '-e', script], check=False, timeout=5)
    except Exception:
        pass


def _check_market_window():
    """Return True if NY time is 15:00–15:59 (3 PM ET) on a weekday.
    JST 4:00 AM → EDT 15:00 ✓ (サマータイム)
    JST 5:00 AM → EST 15:00 ✓ (冬時間)
    JST 4:00 AM → EST 14:00 × (冬時間の誤発火をスキップ)
    JST 5:00 AM → EDT 16:00 × (サマータイムの誤発火をスキップ)
    """
    et = datetime.now(ZoneInfo('America/New_York'))
    season = 'EDT' if et.utcoffset().total_seconds() == -4 * 3600 else 'EST'
    if et.weekday() >= 5:
        print(f"[SKIP] 週末 (ET: {et.strftime('%a %H:%M')} {season})")
        return False
    if et.hour != 15:   # 3:00 PM ET のみ実行
        print(f"[SKIP] ET {et.strftime('%H:%M')} {season} — 実行ウィンドウ外 (対象: 15:00–15:59 ET)")
        return False
    print(f"[OK] ET {et.strftime('%H:%M')} {season} — 引け約{60 - et.minute}分前")
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Morning Capital Flow Analysis')
    parser.add_argument('--top',     type=int, default=5, help='各ウォッチリストの上位件数 (default: 5)')
    parser.add_argument('--workers', type=int, default=4, help='並列スレッド数 (default: 4)')
    parser.add_argument('--log-dir', default='/Users/masaru/Projects/Trading/logs',
                        help='ログ出力ディレクトリ')
    parser.add_argument('--no-notify', action='store_true', help='macOS通知を無効化')
    parser.add_argument('--force', action='store_true', help='NY市場時間チェックをスキップして強制実行')
    args = parser.parse_args()

    # NY market window guard (skip if outside 1-3 hrs before close)
    if not args.force and not _check_market_window():
        sys.exit(0)

    log_dir = args.log_dir
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"analysis_{datetime.now().strftime('%Y%m%d_%H%M')}.log")

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
        log_file.flush()
        _copy_to_gdrive(log_path)
        if not args.no_notify:
            _notify('📈 朝の資金分析 完了',
                    f'ログ: {os.path.basename(log_path)} — {log_path}')
    except Exception as e:
        msg = f'エラー: {e}'
        print(msg)
        if not args.no_notify:
            _notify('⚠️ 朝の資金分析 失敗', msg)
        raise
    finally:
        sys.stdout = sys.__stdout__
        log_file.close()
