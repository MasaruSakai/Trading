#!/usr/bin/env python3
"""Small stdlib client for kabu Station API board snapshots.

The first goal is to log reproducible Japan-market order-flow proxies without
depending on moomoo capital_distribution/capital_flow.
"""
import csv
import json
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
    buy_book = _book_qty(board, "Buy") + _num(board.get("UnderBuyQty"))
    sell_book = _book_qty(board, "Sell") + _num(board.get("OverSellQty"))

    vwap_dev = current / vwap - 1.0 if current > 0 and vwap > 0 else 0.0
    market_pressure_raw = market_buy - market_sell
    market_pressure = _bounded(market_pressure_raw, market_buy + market_sell)
    book_pressure_raw = buy_book - sell_book
    book_pressure = _bounded(book_pressure_raw, buy_book + sell_book)

    if median_trading_value and median_trading_value > 0:
        trading_value_surge = max(-1.0, min(1.0, trading_value / median_trading_value - 1.0))
    else:
        trading_value_surge = 0.0

    tick_component = 0.0
    if tick_up_ratio is not None:
        tick_component = max(-1.0, min(1.0, (float(tick_up_ratio) - 0.5) * 2.0))

    score = (
        0.30 * _bounded(vwap_dev, 0.03)
        + 0.25 * market_pressure
        + 0.20 * book_pressure
        + 0.15 * trading_value_surge
        + 0.10 * tick_component
    )

    return {
        "vwap_dev": vwap_dev,
        "market_order_pressure": market_pressure,
        "market_order_pressure_raw": market_pressure_raw,
        "book_pressure": book_pressure,
        "book_pressure_raw": book_pressure_raw,
        "buy_book_qty": buy_book,
        "sell_book_qty": sell_book,
        "trading_value_surge": trading_value_surge,
        "tick_component": tick_component,
        "kabu_pressure_score": score,
    }


CSV_FIELDS = [
    "logged_at",
    "symbol",
    "symbol_name",
    "exchange",
    "current_price",
    "vwap",
    "vwap_dev",
    "trading_volume",
    "trading_value",
    "market_order_buy_qty",
    "market_order_sell_qty",
    "market_order_pressure",
    "market_order_pressure_raw",
    "buy_book_qty",
    "sell_book_qty",
    "book_pressure",
    "book_pressure_raw",
    "trading_value_surge",
    "tick_component",
    "kabu_pressure_score",
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
        "trading_volume": board.get("TradingVolume", ""),
        "trading_value": board.get("TradingValue", ""),
        "market_order_buy_qty": board.get("MarketOrderBuyQty", ""),
        "market_order_sell_qty": board.get("MarketOrderSellQty", ""),
        "market_order_pressure": metrics["market_order_pressure"],
        "market_order_pressure_raw": metrics["market_order_pressure_raw"],
        "buy_book_qty": metrics["buy_book_qty"],
        "sell_book_qty": metrics["sell_book_qty"],
        "book_pressure": metrics["book_pressure"],
        "book_pressure_raw": metrics["book_pressure_raw"],
        "trading_value_surge": metrics["trading_value_surge"],
        "tick_component": metrics["tick_component"],
        "kabu_pressure_score": metrics["kabu_pressure_score"],
    }


def append_csv(path, row):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerow(row)
