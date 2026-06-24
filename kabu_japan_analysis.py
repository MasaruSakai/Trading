#!/usr/bin/env python3
"""
kabu Station API based Japan market analysis prototype.

Existing Japan analysis depends on moomoo watchlists and capital-flow fields.
This prototype uses kabu Station only:

  1. Get two temporary ETF universes from kabu Station ranking:
     Type=4 (売買代金) as the moomoo standard-like view, and
     Type=7 (売買代金急増) as the improved view.
  2. Fetch /board only for holdings and top-ranked candidates.
  3. Keep each table sorted by its ranking metric; board score is displayed
     only as a pressure reference.

Usage:
  python3 kabu_japan_analysis.py --base-url http://10.215.1.57:18180 --no-token-required
  python3 kabu_japan_analysis.py --universe-size 100 --top 20
"""
import argparse
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from kabu_client import (  # noqa: E402
    DEFAULT_BASE_URL,
    KabuApiError,
    KabuClient,
    board_to_row,
    score_board,
)


TOP_N_DEFAULT = 0
UNIVERSE_SIZE_DEFAULT = 100
BOARD_LIMIT_DEFAULT = 35
NUM_WORKERS_DEFAULT = 1
CALL_INTERVAL_DEFAULT = 1.1
RETRY_WAIT_SECONDS = 3.0
DEFAULT_LOG_DIR = os.path.join(HERE, "logs")
DEFAULT_PASSWORD_FILE = os.path.join(HERE, "kabu_station_server", "config", "kabu_password.txt")
DEFAULT_PASSWORD_SUFFIX = "prod"
GDRIVE_LOG_DIR = (
    "/Users/masaru/Library/CloudStorage/GoogleDrive-sbrmsj@gmail.com/"
    "マイドライブ/AssetManagement/日本logs_kabu"
)

RANKING_TYPE_TURNOVER = 4
RANKING_TYPE_TURNOVER_SURGE = 7
RANKING_TYPE_TICK_COUNT = 5

# Fallback used only when ranking is unavailable. These are liquid JP names
# commonly useful for smoke-testing board based logic.
FALLBACK_SYMBOLS = [
    "7203", "8306", "9984", "6758", "8035", "6861", "9432", "8316", "8058", "6501",
    "8411", "7011", "6098", "7974", "4063", "8766", "6954", "7267", "8001", "4568",
    "7751", "8031", "4502", "2914", "3382", "4519", "6702", "6301", "6594", "9433",
    "6981", "4901", "7741", "7269", "4661", "6857", "6902", "4503", "9020", "8801",
    "6503", "8802", "5401", "5108", "2413", "1605", "6178", "4689", "7733", "9613",
    "7012", "7013", "5020", "8591", "7201", "7261", "9843", "4755", "4307", "3659",
    "4188", "6723", "7270", "8604", "8725", "8750", "1925", "1928", "2502", "2503",
    "2802", "3402", "3436", "4005", "4151", "4452", "4523", "4578", "5019", "5201",
    "5713", "5802", "6273", "6326", "6367", "6506", "6645", "6762", "6920", "6963",
    "6971", "7182", "7453", "7832", "8267", "8830", "9022", "9101", "9104", "9201",
]

FALLBACK_ETF_SYMBOLS = [
    "1623", "1306", "1579", "563A", "521A", "1360", "1475", "1459", "1545", "318A",
    "1357", "2563", "1366", "314A", "2638", "2033", "1456", "2868", "2625", "235A",
    "2627", "2648", "1494", "237A", "2513", "2518", "1356", "1570", "200A", "492A",
    "182A", "1479", "2088", "360A", "2066", "2851", "2082", "2047", "183A", "2525",
    "2524", "1675", "1477", "2516", "1321", "1655",
]

EXCLUDE_INDEX_ETFS = {
    "1320", "1346", "1397", "2525", "1570", "1357", "1458", "1459", "1579", "1360", "1366", "1456",
    "1308", "1348", "1475", "2524", "2088", "1568", "1356", "1457", "1368", "1367",
    "2568", "2631", "2840", "2841", "2243",
    "2558", "2521", "2563", "2633",
    "1591", "1592", "1593", "1599", "1305", "1489", "1478", "1330"
}


class Tee:
    def __init__(self, *files):
        self.files = files

    def write(self, obj):
        for fp in self.files:
            fp.write(obj)
            fp.flush()

    def flush(self):
        for fp in self.files:
            fp.flush()


def _copy_to_gdrive(log_path):
    try:
        os.makedirs(GDRIVE_LOG_DIR, exist_ok=True)
        dest = os.path.join(GDRIVE_LOG_DIR, os.path.basename(log_path))
        shutil.copy2(log_path, dest)
        print(f"  [GDrive] コピー完了: {dest}")
        return dest
    except Exception as e:
        print(f"  [GDrive] コピー失敗: {e}")
        return None


def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def read_secret(path):
    try:
        with open(path, encoding="utf-8") as fp:
            return fp.read().strip()
    except FileNotFoundError:
        return None


def _code_from_ranking_row(row):
    if not isinstance(row, dict):
        return None
    for key in ("Symbol", "symbol", "Code", "code"):
        value = row.get(key)
        if value:
            return str(value).split(".")[-1]
    return None


def _pick(row, *keys):
    if not isinstance(row, dict):
        return ""
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return ""


def _ranking_rows(body):
    if isinstance(body, list):
        return body
    if not isinstance(body, dict):
        return []
    for key in ("Ranking", "ranking", "Items", "items", "Result", "result"):
        value = body.get(key)
        if isinstance(value, list):
            return value
    for value in body.values():
        if isinstance(value, list):
            return value
    return []


def _is_etf_row(row):
    if not isinstance(row, dict):
        return False
    exchange_name = str(row.get("ExchangeName") or "")
    category_name = str(row.get("CategoryName") or "")
    symbol_name = str(row.get("SymbolName") or "")
    return (
        "ETF" in exchange_name
        or "ETN" in exchange_name
        or "ETF" in symbol_name
        or category_name == "その他" and ("ETF" in exchange_name or "ETN" in exchange_name)
    )


def _merge_ranking_row(base, row, ranking_type):
    merged = dict(base or {})
    if not merged or ranking_type == RANKING_TYPE_TURNOVER:
        for key in (
            "No", "Trend", "AverageRanking", "Symbol", "SymbolName", "CurrentPrice",
            "ChangeRatio", "ChangePercentage", "CurrentPriceTime", "TradingVolume",
            "Turnover", "ExchangeName", "CategoryName",
        ):
            if row.get(key) not in (None, ""):
                merged[key] = row.get(key)
    else:
        for key in ("Symbol", "SymbolName", "CurrentPrice", "ExchangeName", "CategoryName"):
            if key not in merged and row.get(key) not in (None, ""):
                merged[key] = row.get(key)
        if _num(row.get("Turnover")) > _num(merged.get("Turnover")):
            merged["Turnover"] = row.get("Turnover")

    types = set(merged.get("_RankingTypes", []))
    types.add(int(ranking_type))
    merged["_RankingTypes"] = sorted(types)
    if ranking_type == RANKING_TYPE_TURNOVER:
        merged["_TurnoverRank"] = row.get("No")
    elif ranking_type == RANKING_TYPE_TURNOVER_SURGE:
        merged["RapidPaymentPercentage"] = row.get("RapidPaymentPercentage")
        merged["_TurnoverSurgeRank"] = row.get("No")
    elif ranking_type == RANKING_TYPE_TICK_COUNT:
        merged["TickCount"] = row.get("TickCount")
        merged["UpCount"] = row.get("UpCount")
        merged["DownCount"] = row.get("DownCount")
        merged["_TickRank"] = row.get("No")
    return merged


def _ranking_sort_value(row, sort_by):
    if sort_by == "rapid_payment_pct":
        return _num(row.get("RapidPaymentPercentage"))
    if sort_by == "tick_count":
        return _num(row.get("TickCount"))
    return _num(row.get("Turnover"))


def fetch_ranked_symbols(client, universe_size, ranking_type, exchange_division, etf_only=False, sort_by="turnover"):
    ranking_by_symbol = {}

    try:
        body = client.ranking(ranking_type, exchange_division)
    except Exception:
        body = {}
    for row in _ranking_rows(body):
        if etf_only and not _is_etf_row(row):
            continue
        code = _code_from_ranking_row(row)
        if not code:
            continue
        if etf_only and code in EXCLUDE_INDEX_ETFS:
            continue
        ranking_by_symbol[code] = _merge_ranking_row(None, row, ranking_type)

    rows = list(ranking_by_symbol.values())
    rows.sort(key=lambda row: _ranking_sort_value(row, sort_by), reverse=True)
    symbols = []
    for row in rows:
        code = _code_from_ranking_row(row)
        if code and code not in symbols and _ranking_sort_value(row, sort_by) > 0:
            symbols.append(code)
        if len(symbols) >= universe_size:
            break
    if etf_only and len(symbols) < universe_size:
        for code in FALLBACK_ETF_SYMBOLS:
            if code in EXCLUDE_INDEX_ETFS:
                continue
            if code not in symbols:
                symbols.append(code)
                ranking_by_symbol.setdefault(code, {"Symbol": code, "SymbolName": "", "_RankingTypes": []})
            if len(symbols) >= universe_size:
                break
    return symbols, ranking_by_symbol


def _merge_ranking_maps(*maps):
    merged = {}
    for mapping in maps:
        for symbol, row in mapping.items():
            if symbol not in merged:
                merged[symbol] = dict(row)
                continue
            current = dict(merged[symbol])
            for key, value in row.items():
                if key == "_RankingTypes":
                    current[key] = sorted(set(current.get(key, [])) | set(value or []))
                elif value not in (None, ""):
                    current[key] = value
            merged[symbol] = current
    return merged


def attach_ranking(row, ranking_row):
    ranking_row = ranking_row or {}
    out = dict(row)
    base_score = _num(out.get("base_score", out.get("score", 0.0)))
    ranked_turnover = _num(ranking_row.get("Turnover"))
    rapid_payment_pct = _num(ranking_row.get("RapidPaymentPercentage"))
    if ranked_turnover and ranked_turnover < 10_000_000:
        ranked_turnover *= 1_000_000
    if ranked_turnover and not out.get("turnover"):
        out["turnover"] = ranked_turnover
    out["ranking_turnover"] = ranked_turnover
    out["base_score"] = base_score
    if rapid_payment_pct > 0:
        # スコアの急増率偏重を防ぐため、乗算処理を廃止し純粋な板スコアを維持する
        out["score"] = base_score
        out["week_big"] = base_score
        out["big_med5"] = base_score
    out.update({
        "change_pct": _num(ranking_row.get("ChangePercentage")),
        "rapid_payment_pct": rapid_payment_pct,
        "tick_count": _num(ranking_row.get("TickCount")),
        "turnover_rank": ranking_row.get("_TurnoverRank"),
        "turnover_surge_rank": ranking_row.get("_TurnoverSurgeRank"),
    })
    if not out.get("name") and ranking_row.get("SymbolName"):
        out["name"] = ranking_row.get("SymbolName")
    if not out.get("price") and ranking_row.get("CurrentPrice"):
        out["price"] = _num(ranking_row.get("CurrentPrice"))
    return out


def ranking_to_candidate(symbol, row, source_note="ranking"):
    row = row or {}
    turnover = _num(row.get("Turnover"))
    if turnover and turnover < 10_000_000:
        turnover *= 1_000_000
    return {
        "code": symbol,
        "name": row.get("SymbolName", ""),
        "price": _num(row.get("CurrentPrice")),
        "vwap": 0.0,
        "turnover": turnover,
        "ranking_turnover": turnover,
        "score": 0.0,
        "base_score": 0.0,
        "vwap_dev": 0.0,
        "vwap_board_component": 0.0,
        "market_pressure": 0.0,
        "market_order_qty": 0.0,
        "market_order_buy_qty": 0.0,
        "market_order_sell_qty": 0.0,
        "book_pressure": 0.0,
        "buy_book_qty": 0.0,
        "sell_book_qty": 0.0,
        "super_net": 0.0,
        "big_net": 0.0,
        "week_big": 0.0,
        "big_med5": 0.0,
        "change_pct": _num(row.get("ChangePercentage")),
        "rapid_payment_pct": _num(row.get("RapidPaymentPercentage")),
        "tick_count": _num(row.get("TickCount")),
        "turnover_rank": row.get("_TurnoverRank"),
        "turnover_surge_rank": row.get("_TurnoverSurgeRank"),
        "source": source_note,
        "is_valid_universe": False,
    }


def fetch_board_candidate(client, symbol, exchange=1, retries=2):
    last_error = None
    for attempt in range(retries + 1):
        try:
            board = client.board(symbol, exchange)
            break
        except KabuApiError as e:
            last_error = e
            if "API実行回数エラー" not in str(e) or attempt >= retries:
                raise
            time.sleep(RETRY_WAIT_SECONDS)
    else:
        raise last_error
    metrics = score_board(board)
    row = board_to_row(board, metrics)
    return {
        "code": str(row["symbol"] or symbol),
        "name": row["symbol_name"],
        "price": _num(row["current_price"]),
        "vwap": _num(row["vwap"]),
        "turnover": _num(row["trading_value"]),
        "score": row["kabu_pressure_score"],
        "base_score": row["kabu_pressure_score"],
        "vwap_dev": row["vwap_dev"],
        "vwap_board_component": row["vwap_board_component"],
        "market_pressure": row.get("market_order_component", row["market_order_pressure"]),
        "market_order_pressure": row["market_order_pressure"],
        "market_order_qty": _num(row["market_order_qty"]),
        "market_order_buy_qty": _num(row["market_order_buy_qty"]),
        "market_order_sell_qty": _num(row["market_order_sell_qty"]),
        "book_pressure": row["book_pressure"],
        "buy_book_qty": row["buy_book_qty"],
        "sell_book_qty": row["sell_book_qty"],
        # signals.csv compatibility. These are not moomoo flows.
        "super_net": row["market_order_pressure_raw"],
        "big_net": row["book_pressure_raw"],
        "week_big": row["kabu_pressure_score"],
        "big_med5": row["kabu_pressure_score"],
        "is_valid_universe": metrics.get("is_valid_universe", False),
    }


def fetch_positions(client):
    positions = client.positions(product=1, addinfo=True)
    out = {}
    for pos in positions if isinstance(positions, list) else []:
        symbol = str(_pick(pos, "Symbol", "symbol"))
        if not symbol:
            continue
        out[symbol] = {
            "qty": _num(_pick(pos, "LeavesQty", "HoldQty", "Qty", "qty")),
            "avg_price": _num(_pick(pos, "Price", "AvgPrice", "avg_price")),
            "current_price": _num(_pick(pos, "CurrentPrice", "current_price")),
            "valuation": _num(_pick(pos, "Valuation", "valuation")),
            "profit_loss": _num(_pick(pos, "ProfitLoss", "pl")),
            "profit_loss_rate": _num(_pick(pos, "ProfitLossRate", "ProfitLossRatio", "pl_rate")),
            "name": str(_pick(pos, "SymbolName", "SymbolNameFull", "name")),
            "exchange": _pick(pos, "Exchange", "exchange") or 1,
        }
    return out


def position_to_candidate(symbol, pos, error):
    return {
        "code": symbol,
        "name": pos.get("name", ""),
        "price": pos.get("current_price", 0.0),
        "vwap": 0.0,
        "turnover": pos.get("valuation", 0.0),
        "score": 0.0,
        "base_score": 0.0,
        "vwap_dev": 0.0,
        "vwap_board_component": 0.0,
        "market_pressure": 0.0,
        "market_order_qty": 0.0,
        "market_order_buy_qty": 0.0,
        "market_order_sell_qty": 0.0,
        "book_pressure": 0.0,
        "buy_book_qty": 0.0,
        "sell_book_qty": 0.0,
        "super_net": 0.0,
        "big_net": 0.0,
        "week_big": 0.0,
        "big_med5": 0.0,
        "source": f"position fallback: {error}",
        "is_valid_universe": False,
    }


def attach_position(row, pos):
    row = dict(row)
    row.update({
        "qty": pos.get("qty", 0.0),
        "avg_price": pos.get("avg_price", 0.0),
        "valuation": pos.get("valuation", 0.0),
        "profit_loss": pos.get("profit_loss", 0.0),
        "profit_loss_rate": pos.get("profit_loss_rate", 0.0),
    })
    return row


def _print_candidates(candidates, top_n, source, total, title="kabu board 代替分析"):
    display = candidates if top_n is None else candidates[:top_n]
    suffix = "全件" if top_n is None else f"TOP{top_n}"
    print(f"\n  【{title}】{suffix}  ({len(candidates)}銘柄取得 / 候補{total}銘柄)")
    print(f"  universe: {source}")
    if not display:
        print("    表示できる銘柄なし")
        return
    hdr = f"    {'Code':<8} {'Name':<24} {'Score':>8} {'Price':>12}"
    print(hdr)
    print("    " + "-" * 56)
    for r in display:
        name = (r["name"] or "")[:24]
        print(
            f"    {r['code']:<8} {name:<24} {r['score']:>8.3f} {r['price']:>12,.1f}"
        )


def _print_holdings(candidates, total):
    print(f"\n  【保有銘柄】全件  ({len(candidates)}銘柄表示 / {total}銘柄中)")
    if not candidates:
        print("    保有銘柄なし")
        return
    hdr = (
        f"    {'Code':<8} {'Name':<18} {'Score':>8}"
        f" {'VWAP乖離':>10} {'板圧力':>10} {'成行残':>10}"
        f" {'数量':>10} {'評価額':>14} {'損益%':>8} {'損益':>12}"
    )
    print(hdr)
    print("    " + "-" * 114)
    for r in candidates:
        name = (r["name"] or "")[:18]
        print(
            f"    {r['code']:<8} {name:<18} {r['score']:>8.3f}"
            f" {r.get('vwap_dev', 0.0) * 100:>9.2f}%"
            f" {r['book_pressure']:>10.3f}"
            f" {r.get('market_order_qty', 0.0):>10,.0f}"
            f" {r.get('qty', 0.0):>10,.0f} {r.get('valuation', 0.0):>14,.0f}"
            f" {r.get('profit_loss_rate', 0.0):>7.2f}% {r.get('profit_loss', 0.0):>12,.0f}"
        )


def _sell_reason(row):
    if row.get("score", 0.0) <= -0.10:
        return "需給弱"
    if row.get("book_pressure", 0.0) < -0.25:
        return "板弱"
    if row.get("profit_loss_rate", 0.0) < -3.0 and row.get("score", 0.0) < 0:
        return "含み損・需給弱"
    return ""


def _print_sell_watch(candidates, total):
    print(f"\n  【保有銘柄・売却注意】({len(candidates)}銘柄該当 / {total}銘柄中)")
    if not candidates:
        print("    売却注意に該当する保有銘柄なし")
        return
    hdr = (
        f"    {'Code':<8} {'理由':<12} {'Score':>8}"
        f" {'板圧力':>10} {'損益%':>8} {'損益':>12}"
    )
    print(hdr)
    print("    " + "-" * 67)
    for r in candidates:
        print(
            f"    {r['code']:<8} {r['sell_reason']:<12} {r['score']:>8.3f}"
            f" {r['book_pressure']:>10.3f}"
            f" {r.get('profit_loss_rate', 0.0):>7.2f}% {r.get('profit_loss', 0.0):>12,.0f}"
        )


def main(args):
    t0 = datetime.now()
    client = KabuClient(
        base_url=args.base_url,
        token=args.token,
        timeout=args.timeout,
        require_token=not args.no_token_required,
    )
    if not args.no_token_required and not args.token:
        password = args.password or read_secret(args.password_file)
        if not password:
            raise KabuApiError(
                "Set KABU_API_PASSWORD, pass --password, or use --no-token-required for proxy access"
            )
        if args.password_suffix:
            password += args.password_suffix
        client.token_from_password(password)

    print(f"\n{'=' * 72}")
    print(f"  kabu Station Japan Board Analysis  {t0.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 72}\n")

    positions = {}
    if args.include_holdings:
        try:
            positions = fetch_positions(client)
            print(f"  [0/2] 保有銘柄取得: {len(positions)}銘柄")
        except Exception as e:
            print(f"  [0/2] 保有銘柄取得スキップ: {e}")

    standard_source = "ranking Type=4"
    surge_source = "ranking Type=7"
    ranking_by_symbol = {}
    try:
        standard_symbols, standard_ranking = fetch_ranked_symbols(
            client,
            args.universe_size,
            RANKING_TYPE_TURNOVER,
            args.exchange_division,
            etf_only=args.etf_only,
            sort_by="turnover",
        )
        surge_symbols, surge_ranking = fetch_ranked_symbols(
            client,
            args.universe_size,
            RANKING_TYPE_TURNOVER_SURGE,
            args.exchange_division,
            etf_only=args.etf_only,
            sort_by="rapid_payment_pct",
        )
        tick_symbols, tick_ranking = fetch_ranked_symbols(
            client,
            args.universe_size,
            RANKING_TYPE_TICK_COUNT,
            args.exchange_division,
            etf_only=args.etf_only,
            sort_by="tick_count",
        )
        if not standard_symbols and not surge_symbols and not tick_symbols:
            raise KabuApiError("ranking returned no symbols")
        filter_label = ", ETF/ETN only" if args.etf_only else ""
        standard_source = f"ranking Type=4 売買代金順{filter_label}, sorted by Score"
        surge_source = f"ranking Type=7 売買代金急増候補{filter_label}, sorted by Score"
        tick_source = f"ranking Type=5 約定回数順{filter_label}, sorted by Score"
        ranking_by_symbol = _merge_ranking_maps(standard_ranking, surge_ranking, tick_ranking)
    except Exception as e:
        if args.etf_only:
            standard_source = f"fallback ETF list (ranking unavailable: {e})"
            surge_source = standard_source
            tick_source = standard_source
            standard_symbols = FALLBACK_ETF_SYMBOLS[: args.universe_size]
            surge_symbols = list(standard_symbols)
            tick_symbols = list(standard_symbols)
            ranking_by_symbol = {s: {"Symbol": s, "SymbolName": "", "_RankingTypes": []} for s in standard_symbols}
        else:
            standard_source = f"fallback liquid list (ranking unavailable: {e})"
            surge_source = standard_source
            tick_source = standard_source
            standard_symbols = FALLBACK_SYMBOLS[: args.universe_size]
            surge_symbols = list(standard_symbols)
            tick_symbols = list(standard_symbols)
            ranking_by_symbol = {s: {"Symbol": s, "SymbolName": "", "_RankingTypes": []} for s in standard_symbols}

    symbols = list(dict.fromkeys(standard_symbols + surge_symbols + tick_symbols))
    holding_symbols = list(positions)
    extra_holding_symbols = [s for s in holding_symbols if s not in symbols]
    board_symbols = list(dict.fromkeys(holding_symbols + symbols[: max(0, args.board_limit)]))
    board_symbols = board_symbols[:40]

    print(f"  [1/2] universe取得: 標準{len(standard_symbols)}銘柄 / 改善{len(surge_symbols)}銘柄 / 約定{len(tick_symbols)}銘柄")
    print(f"         標準: {standard_source}")
    print(f"         改善: {surge_source}")
    print(f"         約定: {tick_source}")
    if extra_holding_symbols:
        print(f"         保有銘柄を追加分析: {len(extra_holding_symbols)}銘柄")
    print(f"  [2/2] board取得・スコアリング中... {len(board_symbols)}銘柄 / workers={args.workers}", flush=True)

    candidate_by_symbol = {
        symbol: ranking_to_candidate(symbol, ranking_by_symbol.get(symbol), "ranking")
        for symbol in symbols
    }
    holding_candidates = []
    errors = []
    try:
        # 古い登録をまずクリアする
        client.unregister_all()
        register_symbols = [(s, args.exchange) for s in board_symbols]
        client.register(register_symbols)
        print(f"         kabu Station API に {len(register_symbols)} 銘柄を登録しました。")
    except Exception as e:
        print(f"         [WARNING] kabu Station API への銘柄登録に失敗しました: {e}")

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                fetch_board_candidate,
                client,
                s,
                args.exchange,
                args.retries,
            ): s
            for s in board_symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                row = future.result()
                if row["price"] > 0:
                    row = attach_ranking(row, ranking_by_symbol.get(symbol))
                    if symbol in positions:
                        holding_candidates.append(attach_position(row, positions[symbol]))
                    if symbol in symbols:
                        candidate_by_symbol[symbol] = row
            except Exception as e:
                errors.append((symbol, str(e)))
                if symbol in positions:
                    holding_candidates.append(attach_position(
                        position_to_candidate(symbol, positions[symbol], str(e)),
                        positions[symbol],
                    ))
            time.sleep(args.interval)

    try:
        client.unregister_all()
        print("         kabu Station API の登録を全解除しました。")
    except Exception as e:
        print(f"         [WARNING] kabu Station API の登録解除に失敗しました: {e}")

    standard_candidates = [candidate_by_symbol[s] for s in standard_symbols if s in candidate_by_symbol]
    surge_candidates = [
        candidate_by_symbol[s]
        for s in surge_symbols
        if s in candidate_by_symbol
    ]
    tick_candidates = [
        candidate_by_symbol[s]
        for s in tick_symbols
        if s in candidate_by_symbol
    ]
    standard_candidates.sort(key=lambda r: r.get("score", -999.0), reverse=True)
    surge_candidates.sort(key=lambda r: r.get("score", -999.0), reverse=True)
    tick_candidates.sort(key=lambda r: r.get("score", -999.0), reverse=True)
    holding_candidates.sort(key=lambda r: (r["score"], r.get("profit_loss_rate", 0.0)), reverse=True)
    sell_watch = []
    for row in holding_candidates:
        reason = _sell_reason(row)
        if reason:
            item = dict(row)
            item["sell_reason"] = reason
            sell_watch.append(item)
    sell_watch.sort(key=lambda r: (r["score"], r.get("profit_loss_rate", 0.0)))
    elapsed = (datetime.now() - t0).total_seconds()
    print(
        f"         完了: ETF標準{len(standard_candidates)}銘柄 / "
        f"ETF改善{len(surge_candidates)}銘柄 / ETF約定{len(tick_candidates)}銘柄 / "
        f"保有{len(holding_candidates)}銘柄 / エラー{len(errors)}件"
    )
    if errors and args.show_errors:
        for symbol, err in errors[:20]:
            print(f"         error {symbol}: {err}")

    if args.include_holdings:
        _print_holdings(holding_candidates, total=len(positions))
        _print_sell_watch(sell_watch, total=len(positions))

    _print_candidates(
        standard_candidates,
        args.top,
        standard_source,
        len(standard_symbols),
        title="売買代金順（Score順）",
    )
    _print_candidates(
        surge_candidates,
        args.top,
        surge_source,
        len(surge_symbols),
        title="改善版（Score順）",
    )
    _print_candidates(
        tick_candidates,
        args.top,
        tick_source,
        len(tick_symbols),
        title="約定回数順（Score順）",
    )

    if not args.no_signals:
        try:
            from signals_log import append_signals

            groups = {
                "売買代金順": standard_candidates,
                "改善版(Score順)": surge_candidates,
                "約定回数順(Score順)": tick_candidates,
            }
            if holding_candidates:
                groups["保有銘柄"] = holding_candidates
            path = append_signals("jp", t0, groups, variant="kabu_board")
            if path:
                print(f"\n  [signals] CSV追記: {sum(len(v) for v in groups.values())}行 -> {path}")
        except Exception as e:
            print(f"\n  [signals] 追記スキップ: {e}")

    print(f"\n  合計所要時間: {elapsed:.1f}秒\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="kabu Station based Japan market analysis prototype")
    parser.add_argument("--base-url", default=os.getenv("KABU_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--token", default=os.getenv("KABU_API_TOKEN"))
    parser.add_argument("--password", default=os.getenv("KABU_API_PASSWORD"))
    parser.add_argument("--password-file", default=os.getenv("KABU_PASSWORD_FILE", DEFAULT_PASSWORD_FILE))
    parser.add_argument("--password-suffix", default=os.getenv("KABU_PASSWORD_SUFFIX", DEFAULT_PASSWORD_SUFFIX))
    parser.add_argument("--no-token-required", action="store_true", help="Use when calling the Windows proxy")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--universe-size", type=int, default=UNIVERSE_SIZE_DEFAULT)
    parser.add_argument("--top", type=int, default=TOP_N_DEFAULT, help="表示件数。0なら全件")
    parser.add_argument("--board-limit", type=int, default=BOARD_LIMIT_DEFAULT,
                        help="ランキング候補のうちboard取得する上位件数")
    parser.add_argument("--workers", type=int, default=NUM_WORKERS_DEFAULT)
    parser.add_argument("--interval", type=float, default=CALL_INTERVAL_DEFAULT)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--exchange", type=int, default=1)
    parser.add_argument("--ranking-type", type=int, default=RANKING_TYPE_TURNOVER)
    parser.add_argument("--exchange-division", default="ALL")
    parser.add_argument("--etf-only", action=argparse.BooleanOptionalAction, default=True, help="ランキング候補をETF/ETNだけに絞る")
    parser.add_argument("--include-holdings", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--no-signals", action="store_true")
    parser.add_argument("--show-errors", action="store_true")
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    parser.add_argument("--no-log", action="store_true")
    parser.add_argument("--no-gdrive-copy", action="store_true")
    ns = parser.parse_args()
    if ns.top == 0:
        ns.top = None

    log_file = None
    log_path = None
    if not ns.no_log:
        os.makedirs(ns.log_dir, exist_ok=True)
        log_path = os.path.join(ns.log_dir, f"kabu_japan_{datetime.now().strftime('%Y%m%d_%H%M')}.log")
        log_file = open(log_path, "w", encoding="utf-8")
        sys.stdout = Tee(sys.__stdout__, log_file)

    try:
        main(ns)
        if log_file:
            log_file.flush()
        if log_path and not ns.no_gdrive_copy:
            _copy_to_gdrive(log_path)
    finally:
        if log_file:
            sys.stdout = sys.__stdout__
            log_file.close()
