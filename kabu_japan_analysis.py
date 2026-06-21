#!/usr/bin/env python3
"""
kabu Station API based Japan market analysis prototype.

Existing Japan analysis depends on moomoo watchlists and capital-flow fields.
This prototype uses kabu Station only:

  1. Get a temporary universe from kabu Station ranking, optionally keep only
     ETFs/ETNs, and sort it by the Turnover field as a trading-value proxy.
  2. Fetch /board for each symbol.
  3. Rank by board-derived pressure score.

Usage:
  python3 kabu_japan_analysis.py --base-url http://10.215.1.57:18180 --no-token-required
  python3 kabu_japan_analysis.py --universe-size 100 --top 20
"""
import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "kabu_station"))

from kabu_station.kabu_client import (  # noqa: E402
    DEFAULT_BASE_URL,
    KabuApiError,
    KabuClient,
    board_to_row,
    score_board,
)
from kabu_station.kabu_check import (  # noqa: E402
    DEFAULT_PASSWORD_FILE,
    DEFAULT_PASSWORD_SUFFIX,
    read_secret,
)


TOP_N_DEFAULT = 20
UNIVERSE_SIZE_DEFAULT = 100
NUM_WORKERS_DEFAULT = 1
CALL_INTERVAL_DEFAULT = 1.1
RETRY_WAIT_SECONDS = 3.0

# kabu Station ranking Type is kept configurable because this script is a
# prototype and the official enum can differ by installed API version.
RANKING_TYPE_TICK_COUNT = 5
ETF_RANKING_TYPES = [5, 1, 2, 3, 4, 6, 7, 9, 10, 11, 12, 13]

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
    "2524", "1675", "1477", "2516", "1321",
]


def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


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


def fetch_ranked_symbols(client, universe_size, ranking_type, exchange_division, etf_only=False):
    ranking_types = ETF_RANKING_TYPES if etf_only else [ranking_type]
    rows = []
    seen_codes = set()
    for rt in ranking_types:
        try:
            body = client.ranking(rt, exchange_division)
        except Exception:
            continue
        for row in _ranking_rows(body):
            if etf_only and not _is_etf_row(row):
                continue
            code = _code_from_ranking_row(row)
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            rows.append(row)
    rows.sort(key=lambda row: _num(row.get("Turnover") if isinstance(row, dict) else 0), reverse=True)
    symbols = []
    ranking_by_symbol = {}
    for row in rows:
        code = _code_from_ranking_row(row)
        if code and code not in symbols:
            symbols.append(code)
            ranking_by_symbol[code] = row
        if len(symbols) >= universe_size:
            break
    if etf_only and len(symbols) < universe_size:
        for code in FALLBACK_ETF_SYMBOLS:
            if code not in symbols:
                symbols.append(code)
            if len(symbols) >= universe_size:
                break
    return symbols, ranking_by_symbol, ranking_types


def ranking_to_candidate(symbol, row, error):
    row = row or {}
    turnover = _num(row.get("Turnover"))
    if turnover and turnover < 10_000_000:
        turnover *= 1_000_000
    up_count = _num(row.get("UpCount"))
    down_count = _num(row.get("DownCount"))
    if up_count + down_count > 0:
        tick_ratio = up_count / (up_count + down_count)
        score = max(-1.0, min(1.0, (tick_ratio - 0.5) * 2.0))
    else:
        score = 0.0
    return {
        "code": symbol,
        "name": row.get("SymbolName", ""),
        "price": _num(row.get("CurrentPrice")),
        "vwap": 0.0,
        "turnover": turnover,
        "score": score,
        "vwap_dev": 0.0,
        "market_pressure": 0.0,
        "book_pressure": 0.0,
        "buy_book_qty": 0.0,
        "sell_book_qty": 0.0,
        "super_net": 0.0,
        "big_net": 0.0,
        "week_big": score,
        "big_med5": score,
        "source": f"ranking fallback: {error}",
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
        "vwap_dev": row["vwap_dev"],
        "market_pressure": row["market_order_pressure"],
        "book_pressure": row["book_pressure"],
        "buy_book_qty": row["buy_book_qty"],
        "sell_book_qty": row["sell_book_qty"],
        # signals.csv compatibility. These are not moomoo flows.
        "super_net": row["market_order_pressure_raw"],
        "big_net": row["book_pressure_raw"],
        "week_big": row["kabu_pressure_score"],
        "big_med5": row["kabu_pressure_score"],
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
        "vwap_dev": 0.0,
        "market_pressure": 0.0,
        "book_pressure": 0.0,
        "buy_book_qty": 0.0,
        "sell_book_qty": 0.0,
        "super_net": 0.0,
        "big_net": 0.0,
        "week_big": 0.0,
        "big_med5": 0.0,
        "source": f"position fallback: {error}",
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


def _print_candidates(candidates, top_n, source, total):
    display = candidates if top_n is None else candidates[:top_n]
    suffix = "全件" if top_n is None else f"TOP{top_n}"
    print(f"\n  【kabu board 代替分析】{suffix}  ({len(candidates)}銘柄取得 / 候補{total}銘柄)")
    print(f"  universe: {source}")
    if not display:
        print("    表示できる銘柄なし")
        return
    hdr = (
        f"    {'Code':<8} {'Name':<18} {'Score':>8} {'VWAP乖離':>10}"
        f" {'成行圧力':>10} {'板圧力':>10} {'売買代金':>16} {'Price':>10}"
    )
    print(hdr)
    print("    " + "-" * 98)
    for r in display:
        name = (r["name"] or "")[:18]
        print(
            f"    {r['code']:<8} {name:<18} {r['score']:>8.3f}"
            f" {r['vwap_dev']*100:>9.2f}% {r['market_pressure']:>10.3f}"
            f" {r['book_pressure']:>10.3f} {r['turnover']:>16,.0f}"
            f" {r['price']:>10,.1f}"
        )


def _print_holdings(candidates, total):
    print(f"\n  【保有銘柄】全件  ({len(candidates)}銘柄表示 / {total}銘柄中)")
    if not candidates:
        print("    保有銘柄なし")
        return
    hdr = (
        f"    {'Code':<8} {'Name':<18} {'Score':>8} {'VWAP乖離':>10}"
        f" {'板圧力':>10} {'数量':>10} {'評価額':>14} {'損益%':>8} {'損益':>12}"
    )
    print(hdr)
    print("    " + "-" * 104)
    for r in candidates:
        name = (r["name"] or "")[:18]
        print(
            f"    {r['code']:<8} {name:<18} {r['score']:>8.3f}"
            f" {r['vwap_dev']*100:>9.2f}% {r['book_pressure']:>10.3f}"
            f" {r.get('qty', 0.0):>10,.0f} {r.get('valuation', 0.0):>14,.0f}"
            f" {r.get('profit_loss_rate', 0.0):>7.2f}% {r.get('profit_loss', 0.0):>12,.0f}"
        )


def _sell_reason(row):
    if row.get("score", 0.0) <= -0.10:
        return "板/VWAP弱"
    if row.get("vwap_dev", 0.0) < 0 and row.get("book_pressure", 0.0) < 0:
        return "VWAP下・板弱"
    if row.get("profit_loss_rate", 0.0) < -3.0 and row.get("score", 0.0) < 0:
        return "含み損・板弱"
    return ""


def _print_sell_watch(candidates, total):
    print(f"\n  【保有銘柄・売却注意】({len(candidates)}銘柄該当 / {total}銘柄中)")
    if not candidates:
        print("    売却注意に該当する保有銘柄なし")
        return
    hdr = (
        f"    {'Code':<8} {'理由':<12} {'Score':>8} {'VWAP乖離':>10}"
        f" {'板圧力':>10} {'損益%':>8} {'損益':>12}"
    )
    print(hdr)
    print("    " + "-" * 78)
    for r in candidates:
        print(
            f"    {r['code']:<8} {r['sell_reason']:<12} {r['score']:>8.3f}"
            f" {r['vwap_dev']*100:>9.2f}% {r['book_pressure']:>10.3f}"
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

    source = "ranking"
    ranking_by_symbol = {}
    try:
        symbols, ranking_by_symbol, ranking_types = fetch_ranked_symbols(
            client,
            args.universe_size,
            args.ranking_type,
            args.exchange_division,
            etf_only=args.etf_only,
        )
        if not symbols:
            raise KabuApiError("ranking returned no symbols")
        filter_label = ", ETF/ETN only" if args.etf_only else ""
        type_label = ",".join(str(t) for t in ranking_types) if args.etf_only else str(args.ranking_type)
        source = f"ranking Type={type_label}{filter_label}, sorted by Turnover"
    except Exception as e:
        if args.etf_only:
            source = f"fallback ETF list (ranking unavailable: {e})"
            symbols = FALLBACK_ETF_SYMBOLS[: args.universe_size]
        else:
            source = f"fallback liquid list (ranking unavailable: {e})"
            symbols = FALLBACK_SYMBOLS[: args.universe_size]

    holding_symbols = [s for s in positions if s not in symbols]
    analysis_symbols = holding_symbols + symbols

    print(f"  [1/2] universe取得: {len(symbols)}銘柄 ({source})")
    if holding_symbols:
        print(f"         保有銘柄を追加分析: {len(holding_symbols)}銘柄")
    print(f"  [2/2] board取得・スコアリング中... workers={args.workers}", flush=True)

    candidates = []
    holding_candidates = []
    errors = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(fetch_board_candidate, client, s, args.exchange, args.retries): s
            for s in analysis_symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                row = future.result()
                if row["price"] > 0:
                    if symbol in positions:
                        holding_candidates.append(attach_position(row, positions[symbol]))
                    if symbol in symbols:
                        candidates.append(row)
            except Exception as e:
                errors.append((symbol, str(e)))
                if symbol in positions:
                    holding_candidates.append(attach_position(
                        position_to_candidate(symbol, positions[symbol], str(e)),
                        positions[symbol],
                    ))
                if args.etf_only and symbol in ranking_by_symbol and symbol in symbols:
                    candidates.append(ranking_to_candidate(symbol, ranking_by_symbol[symbol], str(e)))
            time.sleep(args.interval)

    candidates.sort(key=lambda r: (0 if r.get("source") else 1, r["score"], r["turnover"]), reverse=True)
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
    print(f"         完了: ETF候補{len(candidates)}銘柄 / 保有{len(holding_candidates)}銘柄 / エラー{len(errors)}件")
    if errors and args.show_errors:
        for symbol, err in errors[:20]:
            print(f"         error {symbol}: {err}")

    if args.include_holdings:
        _print_holdings(holding_candidates, total=len(positions))
        _print_sell_watch(sell_watch, total=len(positions))

    _print_candidates(candidates, args.top, source, len(symbols))

    if not args.no_signals:
        try:
            from signals_log import append_signals

            groups = {"kabu_board": candidates}
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
    parser.add_argument("--workers", type=int, default=NUM_WORKERS_DEFAULT)
    parser.add_argument("--interval", type=float, default=CALL_INTERVAL_DEFAULT)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--exchange", type=int, default=1)
    parser.add_argument("--ranking-type", type=int, default=RANKING_TYPE_TICK_COUNT)
    parser.add_argument("--exchange-division", default="ALL")
    parser.add_argument("--etf-only", action="store_true", help="ランキング候補をETF/ETNだけに絞る")
    parser.add_argument("--include-holdings", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--no-signals", action="store_true")
    parser.add_argument("--show-errors", action="store_true")
    ns = parser.parse_args()
    if ns.top == 0:
        ns.top = None
    main(ns)
