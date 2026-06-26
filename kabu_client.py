#!/usr/bin/env python3
"""Small stdlib client for kabu Station API board snapshots.

The first goal is to log reproducible Japan-market order-flow proxies without
depending on moomoo capital_distribution/capital_flow.
"""
import csv
import json
import math
import os
from datetime import datetime
from urllib import error, request
from urllib.parse import urlencode


DEFAULT_BASE_URL = "http://localhost:18080"


class KabuApiError(RuntimeError):
    pass


class KabuClient:
    def __init__(self, base_url=DEFAULT_BASE_URL, token=None, timeout=10, require_token=True):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.require_token = require_token

    def token_from_password(self, password):
        body = self._request(
            "POST",
            "/kabusapi/token",
            {"APIPassword": password},
            auth=False,
        )
        token = body.get("Token")
        if not token:
            raise KabuApiError(f"Token not found in response: {body}")
        self.token = token
        return token

    def board(self, symbol, exchange=1):
        return self._request("GET", f"/kabusapi/board/{symbol}@{exchange}")

    def positions(self, product=0, symbol=None, side=None, addinfo=False):
        params = {"product": product}
        if symbol:
            params["symbol"] = symbol
        if side:
            params["side"] = side
        if addinfo:
            params["addinfo"] = "true"
        return self._request("GET", f"/kabusapi/positions?{urlencode(params)}")

    def ranking(self, ranking_type, exchange_division="ALL"):
        params = {
            "Type": int(ranking_type),
            "ExchangeDivision": exchange_division,
        }
        return self._request("GET", f"/kabusapi/ranking?{urlencode(params)}")

    def register(self, symbols):
        payload = {
            "Symbols": [
                {"Symbol": str(symbol), "Exchange": int(exchange)}
                for symbol, exchange in symbols
            ]
        }
        return self._request("PUT", "/kabusapi/register", payload)

    def unregister_all(self):
        return self._request("PUT", "/kabusapi/unregister/all", {})

    def orders(self, product=0):
        params = {"product": product}
        return self._request("GET", f"/kabusapi/orders?{urlencode(params)}")

    def cancelorder(self, order_id):
        payload = {"OrderId": order_id}
        return self._request("PUT", "/kabusapi/cancelorder", payload)

    def sendorder(self, payload):
        return self._request("POST", "/kabusapi/sendorder", payload)

    def _request(self, method, path, payload=None, auth=True):
        data = None
        headers = {"Content-Type": "application/json"}
        if auth:
            if self.token:
                headers["X-API-KEY"] = self.token
            elif self.require_token:
                raise KabuApiError("API token is required")
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as res:
                raw = res.read().decode("utf-8")
        except error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise KabuApiError(f"HTTP {e.code}: {detail}") from e
        except error.URLError as e:
            raise KabuApiError(f"Connection failed: {e}") from e
        return json.loads(raw) if raw else {}


def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _book_qty(board, side):
    total = 0.0
    for i in range(1, 11):
        level = board.get(f"{side}{i}") or {}
        total += _num(level.get("Qty"))
    return total


def _bounded(value, scale):
    if scale <= 0:
        return 0.0
    x = value / scale
    if x > 1:
        return 1.0
    if x < -1:
        return -1.0
    return x


def score_board(board, median_trading_value=None, tick_up_ratio=None):
    current = _num(board.get("CurrentPrice"))
    vwap = _num(board.get("VWAP"))
    trading_value = _num(board.get("TradingValue"))
    market_buy = _num(board.get("MarketOrderBuyQty"))
    market_sell = _num(board.get("MarketOrderSellQty"))
    buy_book = _book_qty(board, "Buy")
    sell_book = _book_qty(board, "Sell")

    # WOBI (Exponentially Weighted OBI)
    weighted_buy = 0.0
    weighted_sell = 0.0
    for i in range(1, 11):
        weight = 0.5 ** (i - 1)
        buy_level = board.get(f"Buy{i}") or {}
        sell_level = board.get(f"Sell{i}") or {}
        weighted_buy += _num(buy_level.get("Qty")) * weight
        weighted_sell += _num(sell_level.get("Qty")) * weight

    weighted_buy += _num(board.get("UnderBuyQty")) * 0.001
    weighted_sell += _num(board.get("OverSellQty")) * 0.001

    if weighted_buy + weighted_sell > 0:
        wobi = (weighted_buy - weighted_sell) / (weighted_buy + weighted_sell)
    else:
        wobi = 0.0

    # MarketOrder Imbalance (Market_Pressure)
    market_pressure_raw = market_buy - market_sell
    if market_buy + market_sell > 0:
        market_pressure = market_pressure_raw / (market_buy + market_sell)
    else:
        market_pressure = 0.0

    # VWAP component scaled using math.tanh(50 * vwap_dev)
    vwap_dev = current / vwap - 1.0 if current > 0 and vwap > 0 else 0.0
    vwap_component = math.tanh(50 * vwap_dev)

    # Continuous score
    score = 0.5 * wobi + 0.2 * market_pressure + 0.3 * vwap_component

    # Liquidity & Universe Filter
    buy1 = board.get("Buy1") or {}
    sell1 = board.get("Sell1") or {}
    bid_price1 = _num(buy1.get("Price"))
    ask_price1 = _num(sell1.get("Price"))
    bid_qty1 = _num(buy1.get("Qty"))
    ask_qty1 = _num(sell1.get("Qty"))
    mid_price = (ask_price1 + bid_price1) / 2.0

    is_valid_universe = False
    if current >= 200 and trading_value >= 100_000_000:
        if mid_price > 0:
            relative_spread = (ask_price1 - bid_price1) / mid_price
            market_depth = (ask_qty1 + bid_qty1) / 2.0
            if relative_spread <= 0.005 and market_depth >= 500:
                is_valid_universe = True

    if median_trading_value and median_trading_value > 0:
        trading_value_surge = max(-1.0, min(1.0, trading_value / median_trading_value - 1.0))
    else:
        trading_value_surge = 0.0

    tick_component = 0.0
    if tick_up_ratio is not None:
        tick_component = max(-1.0, min(1.0, (float(tick_up_ratio) - 0.5) * 2.0))

    return {
        "vwap_dev": vwap_dev,
        "vwap_board_component": vwap_component,
        "market_order_pressure": market_pressure,
        "market_order_pressure_raw": market_pressure_raw,
        "market_order_component": market_pressure,
        "market_order_qty": market_buy + market_sell,
        "book_pressure": wobi,
        "book_pressure_raw": weighted_buy - weighted_sell,
        "buy_book_qty": buy_book,
        "sell_book_qty": sell_book,
        "trading_value_surge": trading_value_surge,
        "tick_component": tick_component,
        "kabu_pressure_score": score,
        "is_valid_universe": is_valid_universe,
    }


CSV_FIELDS = [
    "logged_at",
    "symbol",
    "symbol_name",
    "exchange",
    "current_price",
    "vwap",
    "vwap_dev",
    "vwap_board_component",
    "trading_volume",
    "trading_value",
    "market_order_buy_qty",
    "market_order_sell_qty",
    "market_order_qty",
    "market_order_pressure",
    "market_order_pressure_raw",
    "market_order_component",
    "buy_book_qty",
    "sell_book_qty",
    "book_pressure",
    "book_pressure_raw",
    "trading_value_surge",
    "tick_component",
    "kabu_pressure_score",
    "is_valid_universe",
]


def board_to_row(board, metrics=None, logged_at=None):
    metrics = metrics or score_board(board)
    return {
        "logged_at": logged_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": board.get("Symbol", ""),
        "symbol_name": board.get("SymbolName", ""),
        "exchange": board.get("Exchange", ""),
        "current_price": board.get("CurrentPrice", ""),
        "vwap": board.get("VWAP", ""),
        "vwap_dev": metrics["vwap_dev"],
        "vwap_board_component": metrics["vwap_board_component"],
        "trading_volume": board.get("TradingVolume", ""),
        "trading_value": board.get("TradingValue", ""),
        "market_order_buy_qty": board.get("MarketOrderBuyQty", ""),
        "market_order_sell_qty": board.get("MarketOrderSellQty", ""),
        "market_order_qty": metrics["market_order_qty"],
        "market_order_pressure": metrics["market_order_pressure"],
        "market_order_pressure_raw": metrics["market_order_pressure_raw"],
        "market_order_component": metrics["market_order_component"],
        "buy_book_qty": metrics["buy_book_qty"],
        "sell_book_qty": metrics["sell_book_qty"],
        "book_pressure": metrics["book_pressure"],
        "book_pressure_raw": metrics["book_pressure_raw"],
        "trading_value_surge": metrics["trading_value_surge"],
        "tick_component": metrics["tick_component"],
        "kabu_pressure_score": metrics["kabu_pressure_score"],
        "is_valid_universe": metrics.get("is_valid_universe", False),
    }


def append_csv(path, row):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerow(row)
