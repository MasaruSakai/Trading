#!/usr/bin/env python3
"""Fetch and print kabu Station API positions."""
import argparse
import os

from kabu_client import DEFAULT_BASE_URL, KabuClient
from kabu_check import (
    DEFAULT_PASSWORD_FILE,
    DEFAULT_PASSWORD_SUFFIX,
    read_secret,
)


PRODUCT_LABELS = {
    0: "all",
    1: "spot",
    2: "margin",
    3: "future",
    4: "option",
}


def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _fmt_num(value, decimals=0):
    n = _num(value)
    if decimals:
        return f"{n:,.{decimals}f}"
    return f"{n:,.0f}"


def _pick(pos, *keys):
    for key in keys:
        if key in pos and pos[key] not in (None, ""):
            return pos[key]
    return ""


def print_positions(positions):
    if not positions:
        print("No positions.")
        return

    rows = []
    total_profit_loss = 0.0
    for pos in positions:
        symbol = _pick(pos, "Symbol", "symbol")
        name = _pick(pos, "SymbolName", "SymbolNameFull", "name")
        side = _pick(pos, "Side", "side")
        qty = _pick(pos, "LeavesQty", "HoldQty", "Qty", "qty")
        price = _pick(pos, "Price", "AvgPrice", "avg_price")
        current = _pick(pos, "CurrentPrice", "current_price")
        profit_loss = _pick(pos, "ProfitLoss", "Valuation", "valuation")
        profit_loss_rate = _pick(pos, "ProfitLossRate", "ProfitLossRatio")
        total_profit_loss += _num(profit_loss)
        rows.append(
            {
                "symbol": str(symbol),
                "name": str(name),
                "side": str(side),
                "qty": qty,
                "price": price,
                "current": current,
                "pl": profit_loss,
                "pl_rate": profit_loss_rate,
            }
        )

    print(
        f"{'Code':<8} {'Name':<24} {'Side':<4} {'Qty':>10} "
        f"{'Avg':>10} {'Current':>10} {'P/L':>12} {'P/L%':>8}"
    )
    print("-" * 94)
    for r in rows:
        name = r["name"][:24]
        print(
            f"{r['symbol']:<8} {name:<24} {r['side']:<4} "
            f"{_fmt_num(r['qty']):>10} {_fmt_num(r['price'], 2):>10} "
            f"{_fmt_num(r['current'], 2):>10} {_fmt_num(r['pl']):>12} "
            f"{_fmt_num(r['pl_rate'], 2):>8}"
        )
    print("-" * 94)
    print(f"positions={len(rows)} total_profit_loss={total_profit_loss:,.0f}")


def main():
    ap = argparse.ArgumentParser(description="kabu Station API positions check")
    ap.add_argument("--base-url", default=os.getenv("KABU_BASE_URL", DEFAULT_BASE_URL))
    ap.add_argument("--password", default=os.getenv("KABU_API_PASSWORD"))
    ap.add_argument(
        "--password-file",
        default=os.getenv("KABU_PASSWORD_FILE", DEFAULT_PASSWORD_FILE),
    )
    ap.add_argument(
        "--password-suffix",
        default=os.getenv("KABU_PASSWORD_SUFFIX", DEFAULT_PASSWORD_SUFFIX),
    )
    ap.add_argument("--token", default=os.getenv("KABU_API_TOKEN"))
    ap.add_argument("--product", type=int, default=1, choices=PRODUCT_LABELS)
    ap.add_argument("--symbol")
    ap.add_argument("--side", help="1: sell, 2: buy")
    ap.add_argument("--addinfo", action="store_true")
    args = ap.parse_args()

    client = KabuClient(base_url=args.base_url, token=args.token)
    if not client.token:
        password = args.password or read_secret(args.password_file)
        if not password:
            raise SystemExit(
                "Set KABU_API_PASSWORD, pass --password, or create "
                f"{args.password_file}"
            )
        if args.password_suffix:
            password += args.password_suffix
        client.token_from_password(password)
        print("[ok] token acquired")
    else:
        print("[ok] using existing token")

    positions = client.positions(
        product=args.product,
        symbol=args.symbol,
        side=args.side,
        addinfo=args.addinfo,
    )
    print(f"[ok] positions product={PRODUCT_LABELS[args.product]}")
    print_positions(positions)


if __name__ == "__main__":
    main()
