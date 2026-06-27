#!/usr/bin/env python3
"""
資金分析  (variant = 'enhanced')
==================================================
引け前30〜60分の候補抽出用。保有銘柄、新規候補、継続性確認を目的別に表示する。

共通の当日フィルタ:
  当日加重Net = 超大口Net + 大口Net * 0.5
  通過条件   = 当日加重Net >= 0

保有銘柄・新規候補:
  過去5日条件 = median(超大口 + 大口) > 0
  ソート     = (超大口Net + 大口Net*0.5 - 小口Net*0.25) / 売買代金
  狙い       = 当日の売買代金に対して大口側が強く食い込んだ銘柄を上位表示する。

継続性確認:
  過去5日条件 = 5日中4日以上、超大口 + 大口 > 0（件数不足時は全日プラス）
  ソート     = 超大口5日中央値 + 大口5日中央値*0.5 - 小口5日中央値*0.25
  狙い       = 継続的に大口側が入っている銘柄を上位表示する。

補助表示:
  - ベアETF: 米国個別株のみ、対応しそうなベアETFコードを参考表示
  - 平均乖離%: last_price / avg_price - 1（表示のみ）
  - 時間外%: 米国のみ、時間外価格と通常終値の乖離（表示のみ）
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
from datetime import datetime, timedelta
import io
import urllib.request
import zipfile
import pandas as pd
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
from moomoo.common.pb import Qot_Common_pb2

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
CAPITAL_GAINS_TAX_RATE = 0.20315

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


def _get_fred_data_with_cache(series_id, cache_dir='config/macro_cache'):
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f"{series_id}.csv")
    
    should_download = True
    if os.path.exists(cache_file):
        mtime = os.path.getmtime(cache_file)
        mtime_date = datetime.fromtimestamp(mtime).date()
        if mtime_date == datetime.now().date():
            should_download = False
            
    if should_download:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as response:
                content_csv = response.read().decode('utf-8')
            if "DATE" in content_csv or "observation_date" in content_csv:
                from io import StringIO
                df = pd.read_csv(StringIO(content_csv))
                if 'observation_date' in df.columns:
                    df = df.rename(columns={'observation_date': 'DATE'})
                if 'DATE' in df.columns and series_id in df.columns:
                    df[series_id] = pd.to_numeric(df[series_id], errors='coerce')
                    df = df.dropna(subset=[series_id])
                    df.to_csv(cache_file, index=False)
        except Exception as e:
            print(f"  [FRED] Download failed for {series_id}: {e}. Fallback to cache if available.")
            
    if not os.path.exists(cache_file):
        return {}
        
    try:
        df = pd.read_csv(cache_file)
        if 'observation_date' in df.columns:
            df = df.rename(columns={'observation_date': 'DATE'})
        if 'DATE' in df.columns and series_id in df.columns:
            df[series_id] = pd.to_numeric(df[series_id], errors='coerce')
            df = df.dropna(subset=[series_id])
            return dict(zip(df['DATE'], df[series_id]))
    except Exception as e:
        print(f"  [FRED] Error parsing cache file for {series_id}: {e}")
    return {}


def _get_cftc_gold_position_with_cache(cache_dir='config/macro_cache'):
    """Return the latest COMEX gold non-commercial net position and 3-year percentile."""
    import json

    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, 'CFTC_GOLD.json')
    cache_max_age = 7 * 24 * 60 * 60

    if os.path.exists(cache_path) and time.time() - os.path.getmtime(cache_path) < cache_max_age:
        try:
            with open(cache_path, encoding='utf-8') as fp:
                return json.load(fp)
        except Exception:
            pass

    try:
        frames = []
        current_year = datetime.now().year
        for year in range(current_year - 2, current_year + 1):
            url = f'https://www.cftc.gov/files/dea/history/deacot{year}.zip'
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=20) as response:
                payload = response.read()
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                filename = archive.namelist()[0]
                with archive.open(filename) as source:
                    frames.append(pd.read_csv(source, low_memory=False))

        df = pd.concat(frames, ignore_index=True)
        market_names = df['Market and Exchange Names'].astype(str)
        gold = df[market_names.str.startswith('GOLD - COMMODITY EXCHANGE', na=False)].copy()
        if gold.empty:
            return {}

        long_col = 'Noncommercial Positions-Long (All)'
        short_col = 'Noncommercial Positions-Short (All)'
        date_col = 'As of Date in Form YYYY-MM-DD'
        gold['net_long'] = (pd.to_numeric(gold[long_col], errors='coerce')
                            - pd.to_numeric(gold[short_col], errors='coerce'))
        gold[date_col] = pd.to_datetime(gold[date_col], errors='coerce')
        gold = gold.dropna(subset=['net_long', date_col]).sort_values(date_col)
        if gold.empty:
            return {}

        latest = gold.iloc[-1]
        latest_net = float(latest['net_long'])
        result = {
            'date': latest[date_col].strftime('%Y-%m-%d'),
            'net_long': latest_net,
            'percentile': float((gold['net_long'] <= latest_net).mean() * 100.0),
            'sample_count': int(len(gold)),
        }
        with open(cache_path, 'w', encoding='utf-8') as fp:
            json.dump(result, fp, ensure_ascii=False, indent=2)
        return result
    except Exception as e:
        print(f'  [CFTC] Gold position download failed: {e}. Fallback to cache if available.')
        if os.path.exists(cache_path):
            try:
                with open(cache_path, encoding='utf-8') as fp:
                    return json.load(fp)
            except Exception:
                pass
    return {}


def _get_gld_flow_4w(quote_ctx):
    """Return GLD's 20-session aggregate net capital flow as a gold ETF flow proxy."""
    try:
        ret, data = quote_ctx.get_capital_flow('US.GLD', period_type=PeriodType.DAY)
        time.sleep(0.1)
        if ret != RET_OK or data.empty or 'in_flow' not in data.columns:
            return {}
        values = pd.to_numeric(data['in_flow'], errors='coerce').dropna().tail(20)
        if values.empty:
            return {}
        date_str = ''
        date_col = 'capital_flow_item_time'
        if date_col in data.columns:
            dates = pd.to_datetime(data.loc[values.index, date_col], errors='coerce').dropna()
            if not dates.empty:
                date_str = dates.iloc[-1].strftime('%Y-%m-%d')
        return {
            'date': date_str,
            'net_flow': float(values.sum()),
            'sessions': int(len(values)),
        }
    except Exception as e:
        print(f'  [GLD] 4-week flow unavailable: {e}')
        return {}


def _get_naaim_index_with_cache(cache_dir='config/macro_cache'):
    import json
    import urllib.request
    import zipfile
    import xml.etree.ElementTree as ET
    from datetime import datetime, timedelta
    
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, "NAAIM.json")
    
    use_cache = False
    if os.path.exists(cache_path):
        mtime = datetime.fromtimestamp(os.path.getmtime(cache_path))
        if mtime.date() == datetime.now().date():
            use_cache = True
            
    if use_cache:
        try:
            with open(cache_path, encoding='utf-8') as fp:
                return json.load(fp)
        except Exception:
            pass
            
    # Compute dates of the last 3 Wednesdays to find the Excel file download URL
    today = datetime.now().date()
    offset = (today.weekday() - 2) % 7
    last_wed = today - timedelta(days=offset)
    
    download_success = False
    temp_xlsx = os.path.join(cache_dir, "temp_naaim.xlsx")
    
    for i in range(3):
        target_date = last_wed - timedelta(weeks=i)
        year = target_date.year
        month = target_date.month
        day = target_date.day
        
        # Example: https://naaim.org/wp-content/uploads/2026/06/USE_Data-since-Inception_2026-06-24.xlsx
        url = f"https://naaim.org/wp-content/uploads/{year}/{month:02d}/USE_Data-since-Inception_{year}-{month:02d}-{day:02d}.xlsx"
        
        try:
            req = urllib.request.Request(
                url, 
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    with open(temp_xlsx, 'wb') as fp:
                        fp.write(response.read())
                    download_success = True
                    break
        except Exception:
            continue
            
    if download_success and os.path.exists(temp_xlsx):
        try:
            ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
            with zipfile.ZipFile(temp_xlsx, 'r') as z:
                with z.open('xl/worksheets/sheet1.xml') as f:
                    tree = ET.parse(f)
                    root = tree.getroot()
                    rows = root.findall(f'.//{ns}row')
                    
                    data = []
                    # Skip header row[0]
                    for row in rows[1:]:
                        cells = row.findall(f'{ns}c')
                        if len(cells) >= 2:
                            v1 = cells[0].find(f'{ns}v')
                            v2 = cells[1].find(f'{ns}v')
                            if v1 is not None and v2 is not None:
                                try:
                                    serial = int(float(v1.text))
                                    val = float(v2.text)
                                    # Convert Excel serial date to string (1899-12-30 base)
                                    date_obj = datetime(1899, 12, 30) + timedelta(days=serial)
                                    date_str = date_obj.strftime("%m/%d/%Y")
                                    sort_key = date_obj.strftime("%Y-%m-%d")
                                    data.append({"date": date_str, "value": val, "sort_key": sort_key})
                                except Exception:
                                    pass
                                    
            if data:
                data.sort(key=lambda x: x["sort_key"], reverse=True)
                for item in data:
                    item.pop("sort_key", None)
                
                with open(cache_path, 'w', encoding='utf-8') as fp:
                    json.dump(data[:10], fp, ensure_ascii=False)
                
                try:
                    os.remove(temp_xlsx)
                except Exception:
                    pass
                return data[:10]
        except Exception as e:
            print(f"  [NAAIM] Error parsing excel data: {e}")
            
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding='utf-8') as fp:
                return json.load(fp)
        except Exception:
            pass
            
    return None


def _get_fms_cash_with_cache(cache_dir='config/macro_cache'):
    import json
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, "FMS_CASH.json")
    
    if not os.path.exists(cache_path):
        default_data = [
            {"date": "2026-06", "value": 4.60}
        ]
        try:
            with open(cache_path, 'w', encoding='utf-8') as fp:
                json.dump(default_data, fp, ensure_ascii=False)
        except Exception:
            pass
            
    try:
        with open(cache_path, encoding='utf-8') as fp:
            return json.load(fp)
    except Exception:
        return None


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


def _us_vwap_window():
    """Return the regular-session date and whether to freeze the 15:00-16:00 ET window."""
    et = datetime.now(ZoneInfo('America/New_York'))
    target = et.date()
    if et.weekday() >= 5 or et.strftime('%H:%M') < '09:30':
        target -= timedelta(days=1)
        while target.weekday() >= 5:
            target -= timedelta(days=1)
    return target.strftime('%Y-%m-%d'), _get_us_market_session() != 'REGULAR'


def _get_vwap_above_ratio_60m(quote_ctx, code, target_date, fixed_close_window):
    """Calculate the share of selected 1-minute closes above cumulative regular-session VWAP."""
    try:
        ret, data, _ = quote_ctx.request_history_kline(
            code,
            start=target_date,
            end=target_date,
            ktype='K_1M',
            autype='qfq',
            max_count=1000,
        )
        time.sleep(0.1)
        if ret != RET_OK or data.empty:
            return None

        frame = data.copy()
        frame['time_key'] = pd.to_datetime(frame['time_key'], errors='coerce')
        frame['volume'] = pd.to_numeric(frame['volume'], errors='coerce')
        frame['turnover'] = pd.to_numeric(frame['turnover'], errors='coerce')
        frame['close'] = pd.to_numeric(frame['close'], errors='coerce')
        frame = frame.dropna(subset=['time_key', 'volume', 'turnover', 'close'])
        minute = frame['time_key'].dt.strftime('%H:%M')
        frame = frame[(minute >= '09:30') & (minute < '16:00') & (frame['volume'] > 0)].copy()
        if frame.empty:
            return None

        frame['cum_vwap'] = frame['turnover'].cumsum() / frame['volume'].cumsum()
        if fixed_close_window:
            minute = frame['time_key'].dt.strftime('%H:%M')
            window = frame[(minute >= '15:00') & (minute < '16:00')]
        else:
            window = frame.tail(60)
        if window.empty:
            return None
        return float((window['close'] > window['cum_vwap']).mean() * 100.0)
    except Exception as e:
        print(f'  [VWAP上60%] {code} 取得失敗: {e}')
        return None


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
                 score_percent=True, show_bear_etf=True, show_change_pct=False,
                 show_forecast_eps_ratio=False):
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
    forecast_header = f" {'予想EPS/EPS':>11}" if show_forecast_eps_ratio else ''
    extra_header = f" {'MTR%':>7} {'Spread':>7}{forecast_header}"

    if score_percent:
        header_str = f"{'Code':<10} {bear_header}{score_header:>11}{change_header}{pl_header}{extra_header}"
    else:
        header_str = f"{'Code':<10} {bear_header}{score_header:>11}{change_header}{pl_header}{extra_header}"
    print("    " + header_str)
    print("    " + "-" * len(header_str))

    for r in display:
        bear_s = f"{(r.get('bear_etf_code') or '--'):>8} " if show_bear_etf else ''
        score = r.get(score_key, 0.0)
        score_s = f"{score*100:>11.3f}" if score_percent else f"{score:>11,.0f}"
        change_s = f" {r.get('today_change_pct', 0.0):>9.3f}" if show_change_pct else ''

        pl_val = r.get('pl_ratio', 0.0)
        pl_val_str = f"{pl_val:+.2f}%"
        pl_s = f" {pl_val_str:>9}" if is_holdings else ''

        sp = r.get('spread_pct')
        sp_str = f"{sp:.2f}%" if sp is not None else '  N/A'
        mtr_pct = r.get('mtr_pct', 0.0)
        forecast_ratio = r.get('forecast_eps_ratio')
        forecast_s = f" {forecast_ratio:>11.3f}" if forecast_ratio is not None else "          --"
        extra_s = f" {mtr_pct:>6.2f}% {sp_str:>7}{forecast_s if show_forecast_eps_ratio else ''}"

        if score_percent:
            print(f"    {r['code']:<10} {bear_s}{score_s}{change_s}{pl_s}{extra_s}")
        else:
            print(f"    {r['code']:<10} {bear_s}{score_s}{change_s}{pl_s}{extra_s}")


def _print_group_enhanced2(label, cands, top_n, total, show_bear_etf=True,
                           show_forecast_eps_ratio=False):
    display = cands if top_n is None else cands[:top_n]
    suffix = '全件' if top_n is None else f'TOP{top_n}'
    print(f"\n  【{label}】{suffix}  ({len(cands)}銘柄合格 / {total}銘柄中)")
    if not display:
        print("    条件を満たす銘柄なし")
        return
    is_holdings = (label == '保有銘柄')
    bear_header = f"{'ベアETF':>8} " if show_bear_etf else ''
    pl_header = f" {'含み益%':>9}" if is_holdings else ''
    forecast_header = f" {'予想EPS/EPS':>11}" if show_forecast_eps_ratio else ''
    vwap_header = f" {'VWAP上60%':>9}" if show_forecast_eps_ratio else ''
    header_str = f"{'Code':<10} {bear_header}{'候補Score':>11}{vwap_header} {'当日変化率%':>11} {'MTR%':>7} {'Spread':>7}{pl_header}{forecast_header}"
    print("    " + header_str)
    print("    " + "-" * len(header_str))

    for r in display:
        bear_s = f"{(r.get('bear_etf_code') or '--'):>8} " if show_bear_etf else ''

        pl_val = r.get('pl_ratio', 0.0)
        pl_val_str = f"{pl_val:+.2f}%"
        pl_s = f" {pl_val_str:>9}" if is_holdings else ''

        sp = r.get('spread_pct')
        sp_str = f"{sp:.2f}%" if sp is not None else '  N/A'
        forecast_ratio = r.get('forecast_eps_ratio')
        forecast_s = f" {forecast_ratio:>11.3f}" if forecast_ratio is not None else "          --"
        vwap_above = r.get('vwap_above_ratio_60m')
        vwap_above_s = f" {vwap_above:>8.1f}%" if vwap_above is not None else "        --"

        print(f"    {r['code']:<10} {bear_s}"
              f"{r.get('enhanced2_score', 0.0):>11.3f}{vwap_above_s if show_forecast_eps_ratio else ''} "
              f"{r.get('today_change_pct', 0.0):>11.3f} "
              f"{r.get('mtr_pct', 0.0):>6.2f}% {sp_str:>7}{pl_s}{forecast_s if show_forecast_eps_ratio else ''}")


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
         holdings_only=False, slim=False):
    cfg = MARKET_CFG[market]
    t0 = datetime.now()
    if slim:
        show_standard_reference = False
    mode_label = '保有のみ' if holdings_only else '通常'
    print(f"\n{'='*78}")
    print(f"  資金分析  {cfg['label']}  {mode_label}  {t0.strftime('%Y-%m-%d %H:%M:%S')}")
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
                    'high': _row_float(row, 'high_price'), 'low': _row_float(row, 'low_price'),
                    'today_change_pct': change,
                    'bid_price': _row_float(row, 'bid_price'),
                    'ask_price': _row_float(row, 'ask_price'),
                }

        elif len(codes) == 1:
            print(f"\n    [snapshot] スキップ: {codes[0]} ({s})", end='')
        else:                                   # 分割して不良銘柄を隔離
            mid = len(codes) // 2
            _snap(codes[:mid])
            _snap(codes[mid:])

    if market == 'us':
        macro_indicators = [
            'US.XLK', 'US.XLF', 'US.XLV', 'US.XLE', 'US.XLY', 'US.XLP',
            'US.XLB', 'US.XLU', 'US.XLRE', 'US.XLC', 'US.IYT'
        ]
    else:
        macro_indicators = ['JP.2516', 'JP.1306']
    snap_targets = sorted(list(set(all_codes) | set(macro_indicators)))

    for i in range(0, len(snap_targets), SNAPSHOT_BATCH):
        _snap(snap_targets[i:i + SNAPSHOT_BATCH])
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

    # MTR計算のヘルパー関数
    def _get_mtr(quote_ctx, code):
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=25)).strftime("%Y-%m-%d")
        ret, df, _ = quote_ctx.request_history_kline(code, start=start_date, end=end_date, ktype='K_DAY', autype='qfq')
        time.sleep(0.1) # レートリミット配慮
        if ret == RET_OK and not df.empty:
            tr1 = df['high'] - df['low']
            tr2 = (df['high'] - df['last_close']).abs()
            tr3 = (df['low'] - df['last_close']).abs()
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            return float(tr.tail(14).median())
        return 0.0

    # ハードフィルタ: 超大口&大口が売り越していない(当日 net≧0) + 大口5日中央値>0 + VWAP足切り。
    passers = []
    passers_strict = []   # 継続性フィルタ (4/5日プラス) を通過した銘柄
    mtr_cache = {}
    vwap_above_ratio_map = {}
    vwap_window_date, fixed_vwap_window = _us_vwap_window() if market == 'us' else ('', False)

    for c, r in results.items():
        d, f = r['dist'], r.get('flow') or {}
        is_holding = c in holding_codes
        if not is_holding:
            if not f.get('ok'):
                continue
        info = snap_info.get(c, {})
        last, avg_price, tov = info.get('last', 0), info.get('avg_price', 0), info.get('turnover', 0)
        
        # MTRの取得
        if c not in mtr_cache:
            try:
                mtr_cache[c] = _get_mtr(q, c)
            except Exception:
                mtr_cache[c] = 0.0
        mtr = mtr_cache[c]
        
        # 新規候補に対する前段のフィルタリング
        if not is_holding:
            # 1. VWAP足切り
            if avg_price > 0 and mtr > 0:
                if last < avg_price - (0.5 * mtr):
                    continue  # 足切り

            # 2. 当日変化率が通常の値幅(MTR%)を超える銘柄は除外
            change_pct = abs(info.get('today_change_pct', 0.0))
            if last > 0 and mtr > 0:
                mtr_pct = (mtr / last) * 100
                if change_pct > mtr_pct:
                    continue  # 足切り
            
            # 3. スプレッドが通常の値動き（MTR%）より広い異常値は弾く
            bid = info.get('bid_price') or 0.0
            ask = info.get('ask_price') or 0.0
            if bid > 0 and ask > 0 and last > 0 and mtr > 0:
                spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
                mtr_pct = (mtr / last) * 100
                if spread_pct > mtr_pct:
                    continue  # 足切り

            if market == 'us':
                vwap_above_ratio_map[c] = _get_vwap_above_ratio_60m(
                    q, c, vwap_window_date, fixed_vwap_window
                )
                    
        avg_price_dev = (last / avg_price - 1.0) if avg_price > 0 else None   # 表示のみ
        passers.append((c, d, f, tov, avg_price_dev, mtr))
        if is_holding or f.get('ok_strict'):
            passers_strict.append((c, d, f, tov, avg_price_dev, mtr))

    forecast_eps_ratio_map = {}
    gld_flow_4w = {}
    if market == 'us':
        valuation_targets = sorted({
            c for c, _, _, _, _, _ in passers
            if not is_etf(c)
        })
        for code in valuation_targets:
            try:
                ret_val, valuation = q.get_valuation_detail(
                    code,
                    valuation_type=Qot_Common_pb2.ValuationType_PE,
                )
                time.sleep(0.1)
                if ret_val != RET_OK:
                    continue
                trend = valuation.get('trend') or {}
                current_pe = trend.get('current_value')
                forward_pe = trend.get('forward_value')
                if current_pe and forward_pe and forward_pe > 0:
                    forecast_eps_ratio_map[code] = float(current_pe) / float(forward_pe)
            except Exception:
                continue
        gld_flow_4w = _get_gld_flow_4w(q)

    try:
        q.close()
    except Exception:
        pass

    print(f"         前段フィルタ通過: {len(passers)}銘柄  /  継続性通過: {len(passers_strict)}銘柄")


    # candidate 構築
    def make(c, d, f, tov, avg_price_dev, mtr):
        big = d['big_net']
        weighted_net = d['super_net'] + big * 0.5
        sort_weighted_net = weighted_net - d['small_net'] * 0.25
        info = snap_info.get(c, {})
        last = info.get('last', 0.0)
        is_target_etf = is_etf(c)
        bear_etf_code = find_bear_etf_code(market, c, info.get('name', ''), is_target_etf)
        ext_dev, ext_sess = ext_confirm(info, market)
        enhanced1_score_pct = ((sort_weighted_net / tov) * 100.0) if tov > 0 else 0.0

        pl_ratio = holding_pls.get(c, 0.0)
        bonus = 0.0
        if c in holding_codes and pl_ratio > 0.0:
            gain_ratio = pl_ratio / 100.0
            bonus = 100.0 * CAPITAL_GAINS_TAX_RATE * gain_ratio / (1.0 + gain_ratio)

        sort_ingest_ratio = (sort_weighted_net / tov) if tov > 0 else 0.0
        vwap_above_ratio_60m = vwap_above_ratio_map.get(c)
        enhanced2_score = enhanced1_score_pct
        if market == 'us' and c not in holding_codes and vwap_above_ratio_60m is not None:
            enhanced2_score *= vwap_above_ratio_60m / 100.0

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
            'today_change_pct': info.get('today_change_pct', 0.0),
            'enhanced1_score_pct': enhanced1_score_pct,
            'enhanced2_score': enhanced2_score,
            'vwap_above_ratio_60m': vwap_above_ratio_60m,
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
            'mtr': mtr,
            'mtr_pct': (mtr / last * 100) if last > 0 else 0.0,
            'spread_pct': ((info.get('ask_price') or 0) - (info.get('bid_price') or 0)) / (((info.get('ask_price') or 0) + (info.get('bid_price') or 0)) / 2) * 100 if (info.get('bid_price') or 0) > 0 and (info.get('ask_price') or 0) > 0 else None,
            'forecast_eps_ratio': forecast_eps_ratio_map.get(c),
        }

    pass_map = {c: make(c, d, f, tov, vd, mtr) for c, d, f, tov, vd, mtr in passers}


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

    def is_new_candidate(r):
        if r['enhanced1_score_pct'] <= 0:
            return False
        return market != 'us' or r['vwap_above_ratio_60m'] is not None

    def build_enhanced2(codes, etf):
        out = [pass_map[c] for c in codes if c in pass_map and bool(pass_map[c]['is_etf']) == etf]
        out = [r for r in out if is_new_candidate(r)]
        out.sort(key=lambda x: x['enhanced2_score'], reverse=True)
        return out

    elapsed = (datetime.now() - t0).total_seconds()
    print(f"\n{'='*78}")
    print(f"  分析結果  {cfg['label']}  ({datetime.now().strftime('%H:%M:%S')}  経過: {elapsed:.0f}秒)")
    print(f"{'='*78}")

    # --- NAAIM Exposure Index Calculation ---
    naaim_str = "N/A"
    try:
        naaim_data = _get_naaim_index_with_cache()
        if naaim_data:
            latest = naaim_data[0]
            val = latest["value"]
            prev_val = naaim_data[1]["value"] if len(naaim_data) > 1 else None
            
            if val >= 80.0:
                level_label = "極端に高い (買い余力低下・過熱)"
            elif val <= 40.0:
                level_label = "極端に低い (既にリスク削減済・底値圏)"
            else:
                level_label = "中立"
                
            if prev_val is not None:
                change = val - prev_val
                trend_label = "リスク増加" if change > 0 else "ヘッジ・ショート増加"
                naaim_str = f"{val:.2f}% (Weekly Change: {change:+.2f}%, {level_label} | {trend_label})"
            else:
                naaim_str = f"{val:.2f}% ({level_label})"
    except Exception as e:
        naaim_str = f"Error ({e})"

    # --- BofA FMS Cash Level ---
    fms_str = "N/A"
    try:
        fms_data = _get_fms_cash_with_cache()
        if fms_data:
            latest = fms_data[0]
            val = latest["value"]
            date_str = latest["date"]
            
            if val >= 5.0:
                level_label = "過度な現金比率 (買い余力豊富・底値圏)"
            elif val <= 4.0:
                level_label = "過小な現金比率 (買い余力低下・過熱圏)"
            else:
                level_label = "平時 (4.0%〜5.0%の間)"
                
            fms_str = f"{val:.2f}% ({date_str}時点, {level_label})"
    except Exception as e:
        fms_str = f"Error ({e})"

    real_yield_str = 'N/A'
    cftc_gold_str = 'N/A'
    gld_flow_str = 'N/A'
    if market == 'us':
        try:
            real_yield_data = _get_fred_data_with_cache('DFII10')
            observations = sorted(real_yield_data.items())
            if observations:
                latest_date, latest_value = observations[-1]
                if len(observations) >= 6:
                    change_5d = latest_value - observations[-6][1]
                    direction = '追い風' if change_5d < 0 else '逆風' if change_5d > 0 else '中立'
                    real_yield_str = (f'{latest_value:.2f}% '
                                      f'(5D {change_5d:+.2f}pt, {direction}, {latest_date})')
                else:
                    real_yield_str = f'{latest_value:.2f}% ({latest_date})'
        except Exception as e:
            real_yield_str = f'Error ({e})'

        if gld_flow_4w:
            net_flow = gld_flow_4w['net_flow']
            if abs(net_flow) >= 1_000_000_000:
                flow_value = f'{net_flow / 1_000_000_000:+.2f}B USD'
            else:
                flow_value = f'{net_flow / 1_000_000:+.1f}M USD'
            flow_label = '流入' if net_flow > 0 else '流出' if net_flow < 0 else '中立'
            gld_flow_str = (f'{flow_value} ({gld_flow_4w["sessions"]}営業日, '
                            f'{flow_label}, {gld_flow_4w.get("date") or "日付不明"})')

        try:
            cftc_gold = _get_cftc_gold_position_with_cache()
            if cftc_gold:
                percentile = cftc_gold['percentile']
                if percentile >= 90.0:
                    position_label = '買い混雑'
                elif percentile <= 10.0:
                    position_label = '買い余地大'
                else:
                    position_label = '中立'
                cftc_gold_str = (f'{cftc_gold["net_long"]:+,.0f} contracts '
                                 f'(3Y {percentile:.1f}pct, {position_label}, '
                                 f'{cftc_gold["date"]})')
        except Exception as e:
            cftc_gold_str = f'Error ({e})'

    if market == 'us':
        # --- Sector VWAP Breadth ---
        sector_etfs = ['US.XLK', 'US.XLF', 'US.XLV', 'US.XLE', 'US.XLY', 'US.XLP', 'US.XLB', 'US.XLU', 'US.XLRE', 'US.XLC', 'US.IYT']
        valid_sectors = [c for c in sector_etfs if c in snap_info and snap_info[c].get('last') is not None and snap_info[c].get('avg_price') is not None]
        if valid_sectors:
            above_vwap_sectors = sum(1 for c in valid_sectors if snap_info[c]['last'] > snap_info[c]['avg_price'])
            sector_breadth = (above_vwap_sectors / len(valid_sectors)) * 100.0
        else:
            sector_breadth = 0.0

        if sector_breadth >= 60.0:
            sector_category = "流動性潤沢（リスクオン）"
        elif sector_breadth >= 40.0:
            sector_category = "流動性平時（選択的物色）"
        else:
            sector_category = "流動性逼迫（本命集中・資金引き揚げ）"

        print("  [マクロ指標・市場流動性]")
        print(f"    NAAIM Exposure     : {naaim_str}")
        print("                        (NAAIM上昇: 株式リスク増, 低下: ヘッジ増, 極端に高(>=80%): 買い余力低下, 極端に低(<=40%): リスク削減済)")
        print(f"    FMS Cash Level     : {fms_str}")
        print("                        (目安: 5.0%以上で底値圏・買いシグナル、4.0%以下で過熱・売りシグナル)")
        print(f"    Sector VWAP Breadth: {sector_breadth:.1f}% ({sector_category})")
        print(f"    US 10Y Real Yield  : {real_yield_str}")
        print(f"    GLD Flow 4W (proxy): {gld_flow_str}")
        print(f"    CFTC Gold Net Long : {cftc_gold_str}")
        print(f"{'='*78}")
    else:
        non_etfs = [c for c in all_codes if not is_etf(c)]
        valid_non_etfs = [c for c in non_etfs if c in snap_info and snap_info[c].get('last') is not None and snap_info[c].get('avg_price') is not None]
        if valid_non_etfs:
            above_vwap = sum(1 for c in valid_non_etfs if snap_info[c]['last'] > snap_info[c]['avg_price'])
            vwap_breadth = (above_vwap / len(valid_non_etfs)) * 100.0
        else:
            vwap_breadth = 0.0

        if vwap_breadth >= 60.0:
            breadth_category = "流動性潤沢（リスクオン）"
        elif vwap_breadth >= 40.0:
            breadth_category = "流動性平時（選択的物色）"
        else:
            breadth_category = "流動性逼迫（本命集中・資金引き揚げ）"

        print("  [マクロ指標・世界流動性]")
        print(f"    NAAIM Exposure    : {naaim_str}")
        print("                       (NAAIM上昇: 株式リスク増, 低下: ヘッジ増, 極端に高(>=80%): 買い余力低下, 極端に低(<=40%): リスク削減済)")
        print(f"    FMS Cash Level    : {fms_str}")
        print("                       (目安: 5.0%以上で底値圏・買いシグナル、4.0%以下で過熱・売りシグナル)")
        print("  [市場流動性分析]")
        print(f"    VWAP Breadth  : {vwap_breadth:.1f}% ({breadth_category})")
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

    # signals は保有判断用候補を維持する。表示対象とは分離する。
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
        _separator('保有銘柄（保有継続/売却判断）')
        c = group_cands.get('保有銘柄', [])
        _print_group('保有銘柄', c, top_n=5 if slim else None,
                     total=len(groups['保有銘柄']),
                     show_bear_etf=(market == 'us'),
                     show_change_pct=holdings_only,
                     show_forecast_eps_ratio=(market == 'us'))
        _print_sell_watch(sell_watch, total=len(groups['保有銘柄']),
                          show_bear_etf=(market == 'us'))

    if not holdings_only:
        _separator('新規候補')
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
                _print_group_enhanced2(g, c, top_n=3 if slim else top_n, total=total, show_bear_etf=False)
        else:
            native_codes_e2 = set()
            for g in [x for x in order if x != '保有銘柄']:
                if g in ETF_NATIVE_WATCHLISTS:
                    c = build_enhanced2(groups[g], etf=True) + build_enhanced2(groups[g], etf=False)
                    c = [r for r in c if r['code'] not in holding_codes]
                    c.sort(key=lambda x: x['enhanced2_score'], reverse=True)
                    native_codes_e2.update(x['code'] for x in c)
                    _print_group_enhanced2(g, c, top_n=3 if slim else top_n,
                                           total=len([x for x in groups[g] if x not in holding_codes]),
                                           show_forecast_eps_ratio=True)
                else:
                    c = build_enhanced2(groups[g], etf=False)
                    c = [r for r in c if r['code'] not in holding_codes]
                    _print_group_enhanced2(g, c, top_n=3 if slim else top_n,
                                           total=len([x for x in groups[g]
                                                      if not is_etf(x) and x not in holding_codes]),
                                           show_forecast_eps_ratio=True)
            etf_pass_e2 = sorted([v for v in pass_map.values()
                                  if v['is_etf']
                                  and v['code'] not in native_codes_e2
                                  and v['code'] not in holding_codes
                                  and is_new_candidate(v)],
                                 key=lambda x: x['enhanced2_score'], reverse=True)
            _print_group_enhanced2('ETF(参考・分散用)', etf_pass_e2, top_n=5 if slim else None,
                                   total=sum(1 for c in all_codes
                                             if is_etf(c)
                                             and c not in native_codes_e2
                                             and c not in holding_codes),
                                   show_forecast_eps_ratio=True)

    if show_standard_reference:
        # ── 継続性確認(参考) ─────────────────────────────────────────────────────
        strict_pass_map = {c: make(c, d, f, tov, vd, mtr) for c, d, f, tov, vd, mtr in passers_strict}


        def build_strict(codes, etf):
            out = [strict_pass_map[c] for c in codes if c in strict_pass_map and bool(strict_pass_map[c]['is_etf']) == etf]
            out.sort(key=lambda x: x['standard_sort_med5'], reverse=True)
            return out

        _separator('継続性確認（参考）')
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
                             score_key='standard_sort_med5', score_header='5日補正Net',
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
                                 score_key='standard_sort_med5', score_header='5日補正Net',
                                 score_percent=False, show_change_pct=holdings_only,
                                 show_forecast_eps_ratio=True)
                else:
                    if g == '保有銘柄':
                        c = build_strict(groups[g], etf=True) + build_strict(groups[g], etf=False)
                        c.sort(key=lambda x: x['standard_sort_med5'], reverse=True)
                        _print_group(g, c, top_n=None, total=len(groups[g]),
                                     score_key='standard_sort_med5', score_header='5日補正Net',
                                     score_percent=False, show_change_pct=holdings_only,
                                     show_forecast_eps_ratio=True)
                    else:
                        c = build_strict(groups[g], etf=False)
                        _print_group(g, c, top_n=top_n, total=len([x for x in groups[g] if not is_etf(x)]),
                                     score_key='standard_sort_med5', score_header='5日補正Net',
                                     score_percent=False, show_change_pct=holdings_only,
                                     show_forecast_eps_ratio=True)
            if not holdings_only:
                etf_strict = sorted([v for v in strict_pass_map.values()
                                     if v['is_etf'] and v['code'] not in native_codes_s],
                                    key=lambda x: x['standard_sort_med5'], reverse=True)
                _print_group('ETF(参考・分散用)', etf_strict, top_n=None,
                             total=sum(1 for c in all_codes if is_etf(c) and c not in native_codes_s),
                             score_key='standard_sort_med5', score_header='5日補正Net',
                             score_percent=False, show_forecast_eps_ratio=True)

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
    ap = argparse.ArgumentParser(description='資金分析')
    ap.add_argument('--market', choices=['us', 'jp'], required=True)
    ap.add_argument('--top', type=int, default=5)
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--log-dir', default='/Users/masaru/Projects/Trading/logs')
    ap.add_argument('--market-window', action='store_true',
                    help='米国市場はNY 15:00-15:59の時だけ実行する(定期実行用)')
    ap.add_argument('--hide-standard-reference', action='store_true',
                    help='継続性確認の結果を下部に表示しない')
    ap.add_argument('--holdings-only', action='store_true',
                    help='保有銘柄のみを分析し、signals.csv には追記しない')
    ap.add_argument('--slim', action='store_true',
                    help='Minimize console output to save tokens')
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
             holdings_only=args.holdings_only,
             slim=args.slim)
        lf.flush()
        _copy_to_gdrive(log_path, args.market)
    finally:
        sys.stdout = sys.__stdout__
        lf.close()
