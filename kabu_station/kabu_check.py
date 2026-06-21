#!/usr/bin/env python3
"""Check kabu Station API token + board access and append one CSV snapshot."""
import argparse
import json
import os

from kabu_client import (
    DEFAULT_BASE_URL,
    KabuClient,
    append_csv,
    board_to_row,
    score_board,
)


HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)

DEFAULT_PASSWORD_FILE = os.path.join(HERE, "config", "kabu_password.txt")
DEFAULT_PASSWORD_SUFFIX = "prod"
DEFAULT_OUT = os.path.join(PROJECT_ROOT, "logs", "kabu_board.csv")


def read_secret(path):
    try:
        with open(path, encoding="utf-8") as fp:
            return fp.read().strip()
    except FileNotFoundError:
        return None


def main():
    ap = argparse.ArgumentParser(description="kabu Station API board snapshot check")
    ap.add_argument("--base-url", default=os.getenv("KABU_BASE_URL", DEFAULT_BASE_URL))
    ap.add_argument("--password", default=os.getenv("KABU_API_PASSWORD"))
    ap.add_argument(
        "--password-file",
        default=os.getenv("KABU_PASSWORD_FILE", DEFAULT_PASSWORD_FILE),
        help=f"Local password file, default: {DEFAULT_PASSWORD_FILE}",
    )
    ap.add_argument(
        "--password-suffix",
        default=os.getenv("KABU_PASSWORD_SUFFIX", DEFAULT_PASSWORD_SUFFIX),
        help=f"Suffix appended to the API password, default: {DEFAULT_PASSWORD_SUFFIX}",
    )
    ap.add_argument("--token", default=os.getenv("KABU_API_TOKEN"))
    ap.add_argument(
        "--no-token-required",
        action="store_true",
        help="Send requests without a local token; use this when the proxy injects X-API-KEY.",
    )
    ap.add_argument("--symbol", default="7203", help="Japanese stock code, e.g. 7203")
    ap.add_argument("--exchange", type=int, default=1, help="1 is TSE")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--median-trading-value", type=float)
    ap.add_argument("--tick-up-ratio", type=float)
    ap.add_argument("--register", action="store_true", help="Register the symbol for PUSH")
    ap.add_argument("--json", action="store_true", help="Print raw board JSON")
    args = ap.parse_args()

    client = KabuClient(
        base_url=args.base_url,
        token=args.token,
        require_token=not args.no_token_required,
    )
    if args.no_token_required:
        print("[ok] token not required by client")
    elif not client.token:
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

    if args.register:
        client.register([(args.symbol, args.exchange)])
        print(f"[ok] registered {args.symbol}@{args.exchange}")

    board = client.board(args.symbol, args.exchange)
    metrics = score_board(
        board,
        median_trading_value=args.median_trading_value,
        tick_up_ratio=args.tick_up_ratio,
    )
    row = board_to_row(board, metrics)
    append_csv(args.out, row)

    print(f"[ok] board saved: {args.out}")
    print(
        "symbol={symbol} price={current_price} vwap={vwap} "
        "vwap_dev={vwap_dev:.4%} book={book_pressure:.3f} "
        "mkt={market_order_pressure:.3f} score={kabu_pressure_score:.3f}".format(**row)
    )
    if args.json:
        print(json.dumps(board, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
