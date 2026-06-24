#!/usr/bin/env python3
"""
改善版 資金分析  (variant = 'enhanced')
==================================================
引け前30〜60分の候補抽出用。改善版をメイン表示し、下部に標準版参考を併記する。

共通の当日フィルタ:
  当日加重Net = 超大口Net + 大口Net * 0.5
  通過条件   = 当日加重Net >= 0

改善版メイン:
  過去5日条件 = median(超大口 + 大口) > 0
  ソート     = (超大口Net + 大口Net*0.5 - 小口Net*0.25) / 売買代金
  狙い       = 当日の売買代金に対して大口側が強く食い込んだ銘柄を上位表示する。

標準版参考:
  過去5日条件 = 5日中4日以上、超大口 + 大口 > 0（件数不足時は全日プラス）
  ソート     = 超大口5日中央値 + 大口5日中央値*0.5 - 小口5日中央値*0.25
  狙い       = 継続的に大口側が入っている銘柄を上位表示する。

補助表示:
  - ベアETF: 米国個別株のみ、対応しそうなベアETFコードを参考表示
  - 平均乖離%: last_price / avg_price - 1（表示のみ）
  - 時間外%: 米国のみ、時間外価格と通常終値の乖離（表示のみ）
  - 小口過熱: 当日の小口Netが超大口Net + 大口Netを上回る場合に警告
  - 保有銘柄・売却注意: 当日補正Net <= 0 かつ過去フロー悪化時に別枠表示

保有銘柄:
  - 米国市場: moomooの保有銘柄APIから取得
  - 日本市場: お気に入り「Eスマート証券」を保有銘柄相当として使用

シグナルは logs/signals.csv に variant='enhanced' で追記。検証は backtest.py。

使い方:
  python3 analysis_enhanced.py --market us
  python3 analysis_enhanced.py --market jp
  python3 analysis_enhanced.py --market us --top 6 --workers 4
"""
import sys, time, argparse, os, re, math
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, '/Users/masaru/.claude/skills/moomooapi/scripts')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from moomoo import (OpenQuoteContext, RET_OK, PeriodType, TrdEnv,
                    SecurityType, Market)
from common import create_trade_context, create_quote_context, parse_security_firm
from signals_log import append_signals
from analysis_common import get_distribution, get_big_median

OPEND_HOST, OPEND_PORT = '127.0.0.1', 11111
CALL_INTERVAL = 1.05
SNAPSHOT_BATCH = 200
OVERHEAT_THRESHOLD_PCT = 1.0
OVERHEAT_FACTOR = 2.5
OVERHEAT_CAP = 15.0

GDRIVE_LOG_DIR = {
    'us': '/Users/masaru/Library/CloudStorage/GoogleDrive-sbrmsj@gmail.com/マイドライブ/AssetManagement/米国logs',
    'jp': '/Users/masaru/Library/CloudStorage/GoogleDrive-sbrmsj@gmail.com/マイドライブ/AssetManagement/日本logs',
}

# 元々ETFで構成されるグループはETF分離せず、そのグループ内でそのまま表示する
ETF_NATIVE_WATCHLISTS = {'米国セクター'}

MARKET_CFG = {
    'us': {
        'label': '米国市場',
        'watchlists': ['米ハイテク', '米国銘柄', '米国セクター'],
        'holdings': {'acc_id': 284852706236374484, 'firm': 'FUTUJP'},
    },
    'jp': {
        'label': '日本市場',
        'watchlists': ['日本ハイテク', '日本セクター', '日本市場国外', '日本市場コモディティ'],
        'holdings': None,
        'holdings_watchlist': 'Eスマート証券',
    },
}

# ETF判定は起動時に moomoo から市場の全ETFを取得して動的に行う(固定リストは漏れるため)。
# 取得失敗時のフォールバック用の最小セット。
_ETF_FALLBACK = {
    'US.QQQ', 'US.QQQM', 'US.SPY', 'US.VOO', 'US.IVV', 'US.DIA', 'US.IWM',
    'US.XLF', 'US.XLB', 'US.XLE', 'US.XLK', 'US.XLV', 'US.XLI', 'US.XLP',
    'US.XLU', 'US.XLRE', 'US.XLY', 'US.SMH', 'US.SOXX', 'US.SOXL', 'US.SOXS',
    'JP.1321', 'JP.1306', 'JP.1326', 'JP.1545', 'JP.1671', 'JP.2039',
}
_ETF_SET = set(_ETF_FALLBACK)   # main() で市場のETF全件に置き換える
_BEAR_ETF_NAMES = []
_BEAR_ETF_TOKEN_SET = set()
_BEAR_ETF_TOKEN_TO_CODE = {}
_BEAR_ETF_NAME_TO_CODE = []

MOOMOO_MARKET = {'us': Market.US, 'jp': Market.JP}


BEAR_ETF_KEYWORDS = {'SHORT', 'BEAR', 'INVERSE'}
GENERIC_NAME_TOKENS = {
    'INC', 'CORP', 'CORPORATION', 'COMPANY', 'CO', 'LTD', 'PLC', 'ADR',
    'CLASS', 'GROUP', 'HOLDINGS', 'HOLDING', 'TECHNOLOGY', 'TECHNOLOGIES',
    'ENERGY', 'AI', 'SEMICONDUCTOR', 'SEMICONDUCTORS', 'PHARMACEUTICALS',
}


def _norm_text(value):
    return re.sub(r'[^A-Z0-9]+', ' ', str(value or '').upper()).strip()


def _tokens(value):
    return [t for t in _norm_text(value).split() if t]


def load_etf_set(ctx, market):
    """市場の全ETFコード集合を取得して _ETF_SET を更新。失敗時はフォールバック維持。"""
    global _ETF_SET, _BEAR_ETF_NAMES, _BEAR_ETF_TOKEN_SET
    global _BEAR_ETF_TOKEN_TO_CODE, _BEAR_ETF_NAME_TO_CODE
    try:
        r, d = ctx.get_stock_basicinfo(MOOMOO_MARKET[market], SecurityType.ETF)
        if r == RET_OK and not d.empty:
            _ETF_SET = set(d['code'].tolist())
            if market == 'us' and 'name' in d.columns:
                bear_names = []
                bear_tokens = set()
                bear_token_to_code = {}
                bear_name_to_code = []
                for _, row in d.iterrows():
                    code = str(row.get('code', '') or '')
                    name = row.get('name', '')
                    toks = set(_tokens(name))
                    if toks & BEAR_ETF_KEYWORDS:
                        norm_name = _norm_text(name)
                        bear_names.append(norm_name)
                        bear_tokens.update(toks)
                        bear_name_to_code.append((norm_name, code))
                        for tok in toks:
                            bear_token_to_code.setdefault(tok, code)
                _BEAR_ETF_NAMES = bear_names
                _BEAR_ETF_TOKEN_SET = bear_tokens
                _BEAR_ETF_TOKEN_TO_CODE = bear_token_to_code
                _BEAR_ETF_NAME_TO_CODE = bear_name_to_code
            return len(_ETF_SET)
    except Exception as e:
        print(f"  [etf] 動的取得失敗(フォールバック使用): {e}")
    return None


def is_etf(code):
    return code in _ETF_SET


def find_bear_etf_code(market, code, name, is_target_etf=False):
    """米国個別銘柄に対応するベアETFの参考コードをETF名一覧から推定する。"""
    if market != 'us' or is_target_etf or not _BEAR_ETF_NAMES:
        return ''
    ticker = code.split('.')[-1].upper()
    if ticker in _BEAR_ETF_TOKEN_TO_CODE:
        return _BEAR_ETF_TOKEN_TO_CODE[ticker]

    norm_name = _norm_text(name)
    if norm_name and len(norm_name) >= 4:
        for etf_name, etf_code in _BEAR_ETF_NAME_TO_CODE:
            if norm_name in etf_name:
                return etf_code

    for token in _tokens(name):
        if len(token) < 4 or token in GENERIC_NAME_TOKENS:
            continue
        if token in _BEAR_ETF_TOKEN_TO_CODE:
            return _BEAR_ETF_TOKEN_TO_CODE[token]
    return ''


def ext_confirm(info, market):
    """時間外価格による確認。
    実行時刻ET(米国)に応じて生きているセッションを選び、通常終値との乖離を返す。
      戻り値: (ext_dev, ext_sess)  例: (0.031, 'after')。該当なしは (None, '')。
    日本市場(jp)は時間外区分を扱わないため常に (None, '')。"""
    if market != 'us':
        return None, ''
    last = info.get('last', 0)            # 通常終値
    if last <= 0:
        return None, ''
    et_hour = datetime.now(ZoneInfo('America/New_York')).hour
    # ET時間帯 → 優先セッション
    if 16 <= et_hour < 20:
        order = ['after', 'overnight', 'pre']
    elif et_hour >= 20 or et_hour < 4:
        order = ['overnight', 'after', 'pre']
    elif 4 <= et_hour < 9:
        order = ['pre', 'overnight', 'after']
    else:                                 # 9:30-16:00 通常時間中は時間外確認なし
        return None, ''
    for sess in order:
        p = info.get(sess, 0)
        if p and p > 0:
            return round(p / last - 1.0, 5), sess
    return None, ''


def _row_float(row, key):
    try:
        return float(row.get(key, 0) or 0)
    except (ValueError, TypeError):
        return 0.0


def _snapshot_today_change_pct(row):
    """Return today's change in percentage points, e.g. 1.5 for +1.5%."""
    last = _row_float(row, 'last_price')
    for key in ('prev_close_price', 'prev_close', 'yesterday_close_price'):
        prev_close = _row_float(row, key)
        if last > 0 and prev_close > 0:
            return (last / prev_close - 1.0) * 100.0

    for key in ('change_rate', 'change_rate_percentage', 'change_pct',
                'change_percentage', 'change_ratio'):
        if key not in row:
            continue
        raw = _row_float(row, key)
        if not raw:
            return 0.0
        return raw * 100.0 if 'ratio' in key else raw
    return 0.0


def _get_us_market_session():
    """Return 'PRE_MARKET', 'REGULAR', 'AFTER_HOURS', 'OVERNIGHT', or 'CLOSED'."""
    from zoneinfo import ZoneInfo
    et = datetime.now(ZoneInfo('America/New_York'))
    if et.weekday() >= 5:
        return 'CLOSED'
    time_str = et.strftime('%H:%M')
    if '04:00' <= time_str < '09:30':
        return 'PRE_MARKET'
    elif '09:30' <= time_str < '16:00':
        return 'REGULAR'
    elif '16:00' <= time_str < '20:00':
        return 'AFTER_HOURS'
    else:
        return 'OVERNIGHT'


def _overheat_penalty(today_change_pct):
    over = max(abs(today_change_pct) - OVERHEAT_THRESHOLD_PCT, 0.0)
    return min(OVERHEAT_FACTOR * math.sqrt(over), OVERHEAT_CAP)


def _copy_to_gdrive(log_path, market):
    try:
        dest_dir = GDRIVE_LOG_DIR[market]
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, os.path.basename(log_path))
        import shutil
        shutil.copy2(log_path, dest)
        print(f"  [GDrive] コピー完了: {dest}")
    except Exception as e:
        print(f"  [GDrive] コピー失敗: {e}")


def _check_us_market_window():
    """Return True if NY time is 15:00-15:59 on a weekday."""
    et = datetime.now(ZoneInfo('America/New_York'))
    season = 'EDT' if et.utcoffset().total_seconds() == -4 * 3600 else 'EST'
    if et.weekday() >= 5:
        print(f"[SKIP] 週末 (ET: {et.strftime('%a %H:%M')} {season})")
        return False
    if et.hour != 15:
        print(f"[SKIP] ET {et.strftime('%H:%M')} {season} - 実行ウィンドウ外 (対象: 15:00-15:59 ET)")
        return False
    print(f"[OK] ET {et.strftime('%H:%M')} {season} - 引け約{60 - et.minute}分前")
    return True


# ── Worker: 分布(当日) + 大口5日中央値 + 中口/小口net ───────────────────────────
# 大口判定の中核(get_distribution / get_big_median)は analysis_common に集約。

def _worker(codes_slice):
    ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
    out = {}
    try:
        for code in codes_slice:
            dist = get_distribution(ctx, code)
            time.sleep(CALL_INTERVAL)
            flow = None
            if dist['ok']:
                flow = get_big_median(ctx, code)
                time.sleep(CALL_INTERVAL)
            out[code] = {'dist': dist, 'flow': flow}
    finally:
        try: ctx.close()
        except Exception: pass
    return out


# ── Print ─────────────────────────────────────────────────────────────────────

def _print_group(label, cands, top_n, total,
                 score_key='sort_ingest_ratio', score_header='補正食込%',
                 score_percent=True, show_bear_etf=True, show_change_pct=False):
    display = cands if top_n is None else cands[:top_n]
    suffix = '全件' if top_n is None else f'TOP{top_n}'
    print(f"\n  【{label}】{suffix}  ({len(cands)}銘柄合格 / {total}銘柄中)")
    if not display:
        print("    条件を満たす銘柄なし")
        return
    is_holdings = (label == '保有銘柄')
    bear_header = f"{'ベアETF':>8} " if show_bear_etf else ''
    change_header = f" {'前日比%':>9}" if show_change_pct else ''
    pl_header = f" {'含み益%':>9}" if is_holdings else ''

    if score_percent:
        header_str = f"{'Code':<10} {bear_header}{score_header:>11}{change_header}{pl_header} {'小口過熱':>7}"
    else:
        header_str = f"{'Code':<10} {bear_header}{'小口過熱':>7} {score_header:>11}{change_header}{pl_header}"
    print("    " + header_str)
    print("    " + "-" * len(header_str))

    for r in display:
        hot = '⚠' if r.get('small_dom') else ''
        bear_s = f"{(r.get('bear_etf_code') or '--'):>8} " if show_bear_etf else ''
        score = r.get(score_key, 0.0)
        score_s = f"{score*100:>11.3f}" if score_percent else f"{score:>11,.0f}"
        change_s = f" {r.get('today_change_pct', 0.0):>9.3f}" if show_change_pct else ''

        pl_val = r.get('pl_ratio', 0.0)
        pl_val_str = f"{pl_val:+.2f}%"
        pl_s = f" {pl_val_str:>9}" if is_holdings else ''

        if score_percent:
            print(f"    {r['code']:<10} {bear_s}{score_s}{change_s}{pl_s} {hot:>7}")
        else:
            print(f"    {r['code']:<10} {bear_s}{hot:>7} {score_s}{change_s}{pl_s}")


def _print_group_enhanced2(label, cands, top_n, total, show_bear_etf=True):
    display = cands if top_n is None else cands[:top_n]
    suffix = '全件' if top_n is None else f'TOP{top_n}'
    print(f"\n  【{label}】{suffix}  ({len(cands)}銘柄合格 / {total}銘柄中)")
    if not display:
        print("    条件を満たす銘柄なし")
        return
    is_holdings = (label == '保有銘柄')
    bear_header = f"{'ベアETF':>8} " if show_bear_etf else ''
    pl_header = f" {'含み益%':>9}" if is_holdings else ''

    header_str = f"{'Code':<10} {bear_header}{'改善2':>11} {'当日変化率%':>11} {'過熱減点':>9}{pl_header} {'小口過熱':>7}"
    print("    " + header_str)
    print("    " + "-" * len(header_str))

    for r in display:
        hot = '⚠' if r.get('small_dom') else ''
        bear_s = f"{(r.get('bear_etf_code') or '--'):>8} " if show_bear_etf else ''

        pl_val = r.get('pl_ratio', 0.0)
        pl_val_str = f"{pl_val:+.2f}%"
        pl_s = f" {pl_val_str:>9}" if is_holdings else ''

        print(f"    {r['code']:<10} {bear_s}"
              f"{r.get('enhanced2_score', 0.0):>11.3f} "
              f"{r.get('today_change_pct', 0.0):>11.3f} "
              f"{r.get('overheat_penalty', 0.0):>9.3f}{pl_s} {hot:>7}")


def _print_sell_watch(cands, total, show_bear_etf=True):
    print(f"\n  【保有銘柄・売却注意】({len(cands)}銘柄該当 / {total}銘柄中)")
    if not cands:
        print("    売却注意に該当する保有銘柄なし")
        return
    bear_header = f"{'ベアETF':>8} " if show_bear_etf else ''
    print(f"    {'Code':<10} {'理由':>8} {bear_header}{'当日補正Net':>14}")
    print("    " + "-" * 48)
    for r in cands:
        bear_s = f"{(r.get('bear_etf_code') or '--'):>8} " if show_bear_etf else ''
        print(f"    {r['code']:<10} {r['sell_reason']:>8} {bear_s}"
              f"{r['weighted_net']:>14,.0f}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main(market, top_n=5, num_workers=4, show_standard_reference=True,
         holdings_only=False):
    cfg = MARKET_CFG[market]
    t0 = datetime.now()
    mode_label = '保有のみ' if holdings_only else '通常'
    print(f"\n{'='*78}")
    print(f"  改善版 資金分析  {cfg['label']}  {mode_label}  {t0.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*78}\n")

    # Step 1: 保有 + ウォッチリスト
    groups = {}
    holding_pls = {}
    json_pls = {}

    if market == 'jp':
        # Fetch JP holdings PL from kabu station proxy API
        kabu_base_url = os.environ.get("KABU_BASE_URL", "http://10.215.1.57:18180").rstrip("/")
        url = f"{kabu_base_url}/kabusapi/positions?product=0"
        try:
            import urllib.request
            import urllib.error
            import json
            print(f"  [holdings_pl] JP保有銘柄P/L取得中 ({url})...", end=' ', flush=True)
            req = urllib.request.Request(url, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                api_count = 0
                for pos in res_data if isinstance(res_data, list) else []:
                    symbol = pos.get("Symbol") if pos.get("Symbol") is not None else pos.get("symbol")
                    pl_rate = pos.get("ProfitLossRate") if pos.get("ProfitLossRate") is not None else pos.get("ProfitLossRatio")
                    if pl_rate is None:
                        pl_rate = pos.get("pl_rate")
                    if symbol is not None and pl_rate is not None:
                        code = f"JP.{symbol}"
                        try:
                            holding_pls[code] = float(pl_rate)
                            api_count += 1
                        except (ValueError, TypeError):
                            holding_pls[code] = 0.0
                print(f"{api_count}件取得")
        except Exception as e:
            print(f"\n  [holdings_pl] Warning: kabu station positions API 取得失敗: {e}. Fallback to 0.0%.")
    else:
        # Load manual override/JP holdings PL ratios from config/holdings_pl.json
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config', 'holdings_pl.json')
        if os.path.exists(config_path):
            try:
                import json
                with open(config_path, 'r', encoding='utf-8') as f:
                    json_pls = json.load(f)
                json_pls = {str(k): float(v) for k, v in json_pls.items()}
            except Exception as e:
                print(f"  [holdings_pl] config/holdings_pl.json 読み込み失敗: {e}")

    if cfg.get('holdings'):
        print("  [1/3] 保有銘柄取得中...", end=' ', flush=True)
        h = cfg['holdings']
        trd = create_trade_context(None, security_firm=parse_security_firm(h['firm']))
        ret, pos = trd.position_list_query(trd_env=TrdEnv.REAL, acc_id=h['acc_id'],
                                           refresh_cache=True)
        trd.close()
        hold = []
        if ret == RET_OK and not pos.empty:
            for _, r in pos.iterrows():
                qty = float(r.get('qty', 0) or 0)
                if qty > 0:
                    code = str(r.get('code', ''))
                    hold.append(code)
                    pl_ratio = 0.0
                    if 'pl_ratio' in r:
                        try:
                            pl_ratio = float(r['pl_ratio'] or 0.0)
                        except (ValueError, TypeError):
                            pl_ratio = 0.0
                    holding_pls[code] = pl_ratio
        groups['保有銘柄'] = hold
        print(f"{len(hold)}銘柄")

    print("  [2/3] ウォッチリスト & スナップショット取得中...", end=' ', flush=True)
    q = create_quote_context() if market == 'jp' else \
        OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
    n_etf = load_etf_set(q, market)
    if n_etf:
        print(f"(ETF {n_etf}件読込) ", end='', flush=True)
    if cfg.get('holdings_watchlist'):
        fav = cfg['holdings_watchlist']
        r, data = q.get_user_security(fav)
        watchlist_codes = data['code'].tolist() if r == RET_OK and not data.empty else []
        # Merge with API holdings
        api_codes = [c for c in holding_pls.keys() if c.startswith('JP.')]
        merged_holdings = list(watchlist_codes)
        for c in api_codes:
            if c not in merged_holdings:
                merged_holdings.append(c)
        groups['保有銘柄'] = merged_holdings
        print(f"(保有相当: {fav} {len(watchlist_codes)}銘柄 + API {len(api_codes)}銘柄) ", end='', flush=True)
        time.sleep(0.2)
    if not holdings_only:
        for g in cfg['watchlists']:
            r, data = q.get_user_security(g)
            groups[g] = data['code'].tolist() if r == RET_OK and not data.empty else []
            time.sleep(0.2)

    all_codes = sorted(set(c for v in groups.values() for c in v))
    group_order = (['保有銘柄'] if '保有銘柄' in groups else []) + \
                  ([] if holdings_only else cfg['watchlists'])

    # Merge manual overrides and default to 0.0% for any remaining holding codes
    for code in groups.get('保有銘柄', []):
        if code in json_pls:
            holding_pls[code] = json_pls[code]
        elif code not in holding_pls:
            holding_pls[code] = 0.0

    # スナップショット: turnover, last_price, avg_price
    # 注: OTC等の不良コードが1つでも混ざるとバッチ全体がエラーになるため、
    #     失敗時は分割リトライして不良銘柄だけ捨てる。
    snap_info = {}

    def _snap(codes):
        if not codes:
            return
        r, s = q.get_market_snapshot(codes)
        time.sleep(0.3)
        if r == RET_OK and not s.empty:
            session = _get_us_market_session() if market == 'us' else 'REGULAR'
            for _, row in s.iterrows():
                c = str(row.get('code', ''))
                tov = _row_float(row, 'turnover')
                last = _row_float(row, 'last_price')
                change = _snapshot_today_change_pct(row)
                
                if market == 'us':
                    if session == 'PRE_MARKET':
                        last = _row_float(row, 'pre_price') or last
                        change = _row_float(row, 'pre_change_rate') if 'pre_change_rate' in row else change
                    elif session == 'AFTER_HOURS':
                        last = _row_float(row, 'after_price') or last
                        change = _row_float(row, 'after_change_rate') if 'after_change_rate' in row else change
                    elif session == 'OVERNIGHT':
                        last = _row_float(row, 'overnight_price') or last
                        change = _row_float(row, 'overnight_change_rate') if 'overnight_change_rate' in row else change

                snap_info[c] = {
                    'name': str(row.get('name', '') or ''),
                    'turnover': tov, 'last': last,
                    'avg_price': _row_float(row, 'avg_price'), 'after': _row_float(row, 'after_price'),
                    'overnight': _row_float(row, 'overnight_price'), 'pre': _row_float(row, 'pre_price'),
                    'today_change_pct': change,
                }
        elif len(codes) == 1:
            print(f"\n    [snapshot] スキップ: {codes[0]} ({s})", end='')
        else:                                   # 分割して不良銘柄を隔離
            mid = len(codes) // 2
            _snap(codes[:mid])
            _snap(codes[mid:])

    for i in range(0, len(all_codes), SNAPSHOT_BATCH):
        _snap(all_codes[i:i + SNAPSHOT_BATCH])
    q.close()
    print(f"{len(all_codes)}銘柄ユニーク / snapshot {len(snap_info)}銘柄")

    # Step 3: 並列で 分布(当日) + 大口5日中央値
    print(f"  [3/3] {len(all_codes)}銘柄を分析中（保有銘柄優先処理＋{num_workers}並列）...", flush=True)

    # 保有銘柄をシングル接続で先に処理
    holding_codes = set(groups.get('保有銘柄', []))
    results = {}
    if holding_codes:
        h_ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
        try:
            for code in sorted(holding_codes):
                dist = get_distribution(h_ctx, code)
                time.sleep(CALL_INTERVAL)
                flow = get_big_median(h_ctx, code)
                time.sleep(CALL_INTERVAL)
                results[code] = {'dist': dist, 'flow': flow}
        finally:
            try: h_ctx.close()
            except Exception: pass

    # 残りのコードを並列処理
    remaining = [c for c in all_codes if c not in holding_codes]
    slices = [[] for _ in range(num_workers)]
    for i, c in enumerate(remaining):
        slices[i % num_workers].append(c)
    with ThreadPoolExecutor(max_workers=num_workers) as ex:
        for fut in as_completed({ex.submit(_worker, s): i for i, s in enumerate(slices)}):
            results.update(fut.result())

    # ハードフィルタ: 超大口&大口が売り越していない(当日 net≧0) + 大口5日中央値>0。
    # 平均価格乖離はフィルタには使わず、参考の表示列として保持する。
    passers = []
    passers_strict = []   # 標準版フィルタ② (4/5日プラス) を通過した銘柄
    for c, r in results.items():
        d, f = r['dist'], r.get('flow') or {}
        is_holding = c in holding_codes
        if not is_holding:
            if not f.get('ok'):
                continue
        info = snap_info.get(c, {})
        last, avg_price, tov = info.get('last', 0), info.get('avg_price', 0), info.get('turnover', 0)
        avg_price_dev = (last / avg_price - 1.0) if avg_price > 0 else None   # 表示のみ
        passers.append((c, d, f, tov, avg_price_dev))
        if is_holding or f.get('ok_strict'):
            passers_strict.append((c, d, f, tov, avg_price_dev))
    print(f"         改善版通過: {len(passers)}銘柄  /  標準版通過: {len(passers_strict)}銘柄")

    # candidate 構築
    def make(c, d, f, tov, avg_price_dev):
        big = d['big_net']
        weighted_net = d['super_net'] + big * 0.5
        sort_weighted_net = weighted_net - d['small_net'] * 0.25
        info = snap_info.get(c, {})
        is_target_etf = is_etf(c)
        bear_etf_code = find_bear_etf_code(market, c, info.get('name', ''), is_target_etf)
        ext_dev, ext_sess = ext_confirm(info, market)
        enhanced1_score_pct = ((sort_weighted_net / tov) * 100.0) if tov > 0 else 0.0
        today_change_pct = info.get('today_change_pct', 0.0)
        overheat_penalty = _overheat_penalty(today_change_pct)

        pl_ratio = holding_pls.get(c, 0.0)
        bonus = 0.0
        if c in holding_codes:
            bonus = max(pl_ratio * 0.2, 0.0)

        sort_ingest_ratio = (sort_weighted_net / tov) if tov > 0 else 0.0
        enhanced2_score = enhanced1_score_pct - overheat_penalty

        sort_ingest_ratio += bonus / 100.0
        enhanced2_score += bonus

        return {
            'code': c, 'super_net': d['super_net'], 'big_net': big,
            'mid_net': d['mid_net'], 'small_net': d['small_net'],
            'turnover': tov,
            'avg_price_dev': round(avg_price_dev, 5) if avg_price_dev is not None else None,
            'vwap_dev': round(avg_price_dev, 5) if avg_price_dev is not None else None,
            'ingest_ratio': (weighted_net / tov) if tov > 0 else 0.0,
            'sort_ingest_ratio': sort_ingest_ratio,
            'today_change_pct': today_change_pct,
            'overheat_penalty': overheat_penalty,
            'enhanced2_score': enhanced2_score,
            'big_med5': f.get('big_med5', 0.0),
            'big_component_med5': f.get('big_component_med5', 0.0),
            'small_med5': f.get('small_med5', 0.0),
            'standard_sort_med5': f.get('standard_sort_med5', 0.0),
            'small_dom': 1 if d['small_net'] > (d['super_net'] + d['big_net']) else 0,
            'is_etf': 1 if is_target_etf else 0,
            'bear_etf': 1 if bear_etf_code else 0,
            'bear_etf_code': bear_etf_code,
            'ext_dev': ext_dev, 'ext_sess': ext_sess,
            'pl_ratio': pl_ratio,
        }

    pass_map = {c: make(c, d, f, tov, vd) for c, d, f, tov, vd in passers}

    def sell_reason(flow):
        if flow.get('sell_strict'):
            return '4/5売り'
        if flow.get('sell_median'):
            return '中央値売り'
        return ''

    sell_watch = []
    for c in sorted(holding_codes):
        r = results.get(c, {})
        d, f = r.get('dist', {}), r.get('flow') or {}
        weighted_net = (d.get('super_net', 0.0) + d.get('big_net', 0.0) * 0.5
                        - d.get('small_net', 0.0) * 0.25)
        reason = sell_reason(f)
        if weighted_net <= 0 and reason:
            info = snap_info.get(c, {})
            is_target_etf = is_etf(c)
            sell_watch.append({
                'code': c,
                'sell_reason': reason,
                'bear_etf_code': find_bear_etf_code(market, c, info.get('name', ''), is_target_etf),
                'weighted_net': weighted_net,
                'super_net': d.get('super_net', 0.0),
                'big_net': d.get('big_net', 0.0),
                'big_med5': f.get('big_med5', 0.0),
                'turnover': info.get('turnover', 0.0),
            })
    sell_watch.sort(key=lambda x: (x['sell_reason'] != '4/5売り', x['weighted_net']))

    def build(codes, etf):
        out = [pass_map[c] for c in codes if c in pass_map and bool(pass_map[c]['is_etf']) == etf]
        out.sort(key=lambda x: x['sort_ingest_ratio'], reverse=True)  # 小口を0.25逆方向に効かせた食い込み率
        return out

    def build_enhanced2(codes, etf):
        out = [pass_map[c] for c in codes if c in pass_map and bool(pass_map[c]['is_etf']) == etf]
        out.sort(key=lambda x: x['enhanced2_score'], reverse=True)
        return out

    elapsed = (datetime.now() - t0).total_seconds()
    print(f"\n{'='*78}")
    print(f"  分析結果  {cfg['label']}  ({datetime.now().strftime('%H:%M:%S')}  経過: {elapsed:.0f}秒)")
    print(f"{'='*78}")

    group_cands = {}
    order = group_order

    def _separator(title):
        print(f"\n{'='*78}")
        print(f"  {title}")
        print(f"{'='*78}")

    def _build_enhanced1_group(g):
        if market == 'jp':
            etf_only = bool(_ETF_SET)
            if g == '保有銘柄':
                c = build(groups[g], etf=True) + build(groups[g], etf=False)
                c.sort(key=lambda x: x['sort_ingest_ratio'], reverse=True)
                return c, len(groups[g])
            c = build(groups[g], etf=True) if etf_only else \
                sorted([pass_map[x] for x in groups[g] if x in pass_map],
                       key=lambda x: x['sort_ingest_ratio'], reverse=True)
            total = sum(1 for x in groups[g] if is_etf(x)) if etf_only else len(groups[g])
            return c, total

        if g in ETF_NATIVE_WATCHLISTS:
            c = build(groups[g], etf=True) + build(groups[g], etf=False)
            c.sort(key=lambda x: x['sort_ingest_ratio'], reverse=True)
            return c, len(groups[g])
        if g == '保有銘柄':
            c = build(groups[g], etf=True) + build(groups[g], etf=False)
            c.sort(key=lambda x: x['sort_ingest_ratio'], reverse=True)
            return c, len(groups[g])
        c = build(groups[g], etf=False)
        return c, len([x for x in groups[g] if not is_etf(x)])

    # signals は従来の改善版1候補を維持する。表示対象とは分離する。
    native_codes = set()
    for g in order:
        c, _ = _build_enhanced1_group(g)
        group_cands[g] = c
        if market == 'us' and g in ETF_NATIVE_WATCHLISTS:
            native_codes.update(x['code'] for x in c)
    if market == 'us' and not holdings_only:
        group_cands['ETF(参考)'] = sorted(
            [v for v in pass_map.values()
             if v['is_etf'] and v['code'] not in native_codes],
            key=lambda x: x['sort_ingest_ratio'], reverse=True)

    if '保有銘柄' in groups:
        _separator('保有銘柄（改善版: 保有継続/売却判断）')
        c = group_cands.get('保有銘柄', [])
        _print_group('保有銘柄', c, top_n=None,
                     total=len(groups['保有銘柄']),
                     show_bear_etf=(market == 'us'),
                     show_change_pct=holdings_only)
        _print_sell_watch(sell_watch, total=len(groups['保有銘柄']),
                          show_bear_etf=(market == 'us'))

    if not holdings_only:
        _separator('新規候補（改善版2: 過熱補正あり）')
        if market == 'jp':
            etf_only = bool(_ETF_SET)
            if etf_only:
                print("  ※ ETFのみ表示(少額のためETFで対応)")
            for g in [x for x in order if x != '保有銘柄']:
                c = build_enhanced2(groups[g], etf=True) if etf_only else \
                    sorted([pass_map[x] for x in groups[g] if x in pass_map],
                           key=lambda x: x['enhanced2_score'], reverse=True)
                c = [r for r in c if r['code'] not in holding_codes]
                total = sum(1 for x in groups[g] if is_etf(x) and x not in holding_codes) \
                    if etf_only else len([x for x in groups[g] if x not in holding_codes])
                _print_group_enhanced2(g, c, top_n=top_n, total=total, show_bear_etf=False)
        else:
            native_codes_e2 = set()
            for g in [x for x in order if x != '保有銘柄']:
                if g in ETF_NATIVE_WATCHLISTS:
                    c = build_enhanced2(groups[g], etf=True) + build_enhanced2(groups[g], etf=False)
                    c = [r for r in c if r['code'] not in holding_codes]
                    c.sort(key=lambda x: x['enhanced2_score'], reverse=True)
                    native_codes_e2.update(x['code'] for x in c)
                    _print_group_enhanced2(g, c, top_n=top_n,
                                           total=len([x for x in groups[g] if x not in holding_codes]))
                else:
                    c = build_enhanced2(groups[g], etf=False)
                    c = [r for r in c if r['code'] not in holding_codes]
                    _print_group_enhanced2(g, c, top_n=top_n,
                                           total=len([x for x in groups[g]
                                                      if not is_etf(x) and x not in holding_codes]))
            etf_pass_e2 = sorted([v for v in pass_map.values()
                                  if v['is_etf']
                                  and v['code'] not in native_codes_e2
                                  and v['code'] not in holding_codes],
                                 key=lambda x: x['enhanced2_score'], reverse=True)
            _print_group_enhanced2('ETF(参考・分散用)', etf_pass_e2, top_n=None,
                                   total=sum(1 for c in all_codes
                                             if is_etf(c)
                                             and c not in native_codes_e2
                                             and c not in holding_codes))

    if show_standard_reference:
        # ── 標準版結果(参考) ─────────────────────────────────────────────────────
        strict_pass_map = {c: make(c, d, f, tov, vd) for c, d, f, tov, vd in passers_strict}

        def build_strict(codes, etf):
            out = [strict_pass_map[c] for c in codes if c in strict_pass_map and bool(strict_pass_map[c]['is_etf']) == etf]
            out.sort(key=lambda x: x['standard_sort_med5'], reverse=True)
            return out

        _separator('参考: 標準版（継続性確認）')
        print("  フィルタ②: 4/5日プラス / ソート: 超大口5日中央値 + 大口5日中央値*0.5 - 小口5日中央値*0.25")
        if market == 'jp':
            for g in group_order:
                if g == '保有銘柄':
                    c = build_strict(groups[g], etf=True) + build_strict(groups[g], etf=False)
                    c.sort(key=lambda x: x['standard_sort_med5'], reverse=True)
                    total = len(groups[g])
                else:
                    c = build_strict(groups[g], etf=True) if bool(_ETF_SET) else \
                        sorted([strict_pass_map[x] for x in groups[g] if x in strict_pass_map],
                               key=lambda x: x['standard_sort_med5'], reverse=True)
                    total = len(groups[g])
                _print_group(g, c, top_n=None if g == '保有銘柄' else top_n, total=total,
                             score_key='standard_sort_med5', score_header='標準補正5d',
                             score_percent=False, show_bear_etf=False,
                             show_change_pct=holdings_only)
        else:
            native_codes_s = set()
            for g in order:
                if g in ETF_NATIVE_WATCHLISTS:
                    c = build_strict(groups[g], etf=True) + build_strict(groups[g], etf=False)
                    c.sort(key=lambda x: x['standard_sort_med5'], reverse=True)
                    native_codes_s.update(x['code'] for x in c)
                    _print_group(g, c, top_n=top_n, total=len(groups[g]),
                                 score_key='standard_sort_med5', score_header='標準補正5d',
                                 score_percent=False, show_change_pct=holdings_only)
                else:
                    if g == '保有銘柄':
                        c = build_strict(groups[g], etf=True) + build_strict(groups[g], etf=False)
                        c.sort(key=lambda x: x['standard_sort_med5'], reverse=True)
                        _print_group(g, c, top_n=None, total=len(groups[g]),
                                     score_key='standard_sort_med5', score_header='標準補正5d',
                                     score_percent=False, show_change_pct=holdings_only)
                    else:
                        c = build_strict(groups[g], etf=False)
                        _print_group(g, c, top_n=top_n, total=len([x for x in groups[g] if not is_etf(x)]),
                                     score_key='standard_sort_med5', score_header='標準補正5d',
                                     score_percent=False, show_change_pct=holdings_only)
            if not holdings_only:
                etf_strict = sorted([v for v in strict_pass_map.values()
                                     if v['is_etf'] and v['code'] not in native_codes_s],
                                    key=lambda x: x['standard_sort_med5'], reverse=True)
                _print_group('ETF(参考・分散用)', etf_strict, top_n=None,
                             total=sum(1 for c in all_codes if is_etf(c) and c not in native_codes_s),
                             score_key='standard_sort_med5', score_header='標準補正5d', score_percent=False)

    if holdings_only:
        print("\n  [signals] 保有のみ分析のためCSV追記をスキップ")
    else:
        # signals.csv 追記(variant='enhanced')
        try:
            path = append_signals(market, t0, group_cands, variant='enhanced')
            if path:
                print(f"\n  [signals] CSV追記: {sum(len(v) for v in group_cands.values())}行 → {path}")
        except Exception as e:
            print(f"  [signals] 追記スキップ: {e}")

    print(f"\n  合計所要時間: {elapsed:.1f}秒\n")


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='改善版 資金分析')
    ap.add_argument('--market', choices=['us', 'jp'], required=True)
    ap.add_argument('--top', type=int, default=5)
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--log-dir', default='/Users/masaru/Projects/Trading/logs')
    ap.add_argument('--market-window', action='store_true',
                    help='米国市場はNY 15:00-15:59の時だけ実行する(定期実行用)')
    ap.add_argument('--hide-standard-reference', action='store_true',
                    help='参考の標準版条件結果を下部に表示しない')
    ap.add_argument('--holdings-only', action='store_true',
                    help='保有銘柄のみを分析し、signals.csv には追記しない')
    args = ap.parse_args()

    if args.market == 'us' and args.market_window and not _check_us_market_window():
        sys.exit(0)

    os.makedirs(args.log_dir, exist_ok=True)
    mode_suffix = '_holdings' if args.holdings_only else ''
    log_path = os.path.join(args.log_dir,
                            f"enhanced_{args.market}{mode_suffix}_{datetime.now().strftime('%Y%m%d_%H%M')}.log")

    class Tee:
        def __init__(self, *fs): self.files = fs
        def write(self, o):
            for f in self.files: f.write(o); f.flush()
        def flush(self):
            for f in self.files: f.flush()

    lf = open(log_path, 'w', encoding='utf-8')
    sys.stdout = Tee(sys.__stdout__, lf)
    try:
        main(args.market, top_n=args.top, num_workers=args.workers,
             show_standard_reference=not args.hide_standard_reference,
             holdings_only=args.holdings_only)
        lf.flush()
        _copy_to_gdrive(log_path, args.market)
    finally:
        sys.stdout = sys.__stdout__
        lf.close()
