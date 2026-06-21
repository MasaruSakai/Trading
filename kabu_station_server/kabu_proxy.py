#!/usr/bin/env python3
"""Reverse proxy for kabu Station API.

This keeps kabu Station on Windows while allowing the main Mac development
environment to call the same /kabusapi/... endpoints over LAN.
"""
import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error, request
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
sys.path.insert(0, PROJECT_ROOT)

from kabu_client import DEFAULT_BASE_URL, KabuApiError, KabuClient
from kabu_check import (
    DEFAULT_PASSWORD_FILE,
    DEFAULT_PASSWORD_SUFFIX,
    read_secret,
)


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


class TokenCache:
    def __init__(self, base_url, password_file, password_suffix, timeout):
        self.base_url = base_url
        self.password_file = password_file
        self.password_suffix = password_suffix
        self.timeout = timeout
        self._token = None
        self._lock = threading.Lock()

    def get(self, force=False):
        with self._lock:
            if self._token and not force:
                return self._token
            password = read_secret(self.password_file)
            if not password:
                raise KabuApiError(f"Password file not found or empty: {self.password_file}")
            if self.password_suffix:
                password += self.password_suffix
            client = KabuClient(base_url=self.base_url, timeout=self.timeout)
            self._token = client.token_from_password(password)
            return self._token

    def clear(self):
        with self._lock:
            self._token = None


def _client_allowed(client_ip, allowed):
    if not allowed:
        return True
    return client_ip in allowed


def make_handler(target_base_url, token_cache, allowed_clients, timeout, auto_auth):
    class ProxyHandler(BaseHTTPRequestHandler):
        server_version = "KabuStationProxy/0.1"

        def do_GET(self):
            self._proxy()

        def do_POST(self):
            self._proxy()

        def do_PUT(self):
            self._proxy()

        def do_DELETE(self):
            self._proxy()

        def do_PATCH(self):
            self._proxy()

        def do_OPTIONS(self):
            self._proxy()

        def _send_json(self, code, payload):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _proxy(self):
            client_ip = self.client_address[0]
            if not _client_allowed(client_ip, allowed_clients):
                self._send_json(403, {"error": "client not allowed", "client": client_ip})
                return

            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                self._send_json(200, {"ok": True, "time": time.time()})
                return
            if parsed.path == "/token/refresh":
                try:
                    token_cache.get(force=True)
                except Exception as e:
                    self._send_json(502, {"error": str(e)})
                    return
                self._send_json(200, {"ok": True})
                return
            if not parsed.path.startswith("/kabusapi/"):
                self._send_json(404, {"error": "only /kabusapi/... is proxied"})
                return

            content_length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(content_length) if content_length else None

            headers = {}
            for key, value in self.headers.items():
                lk = key.lower()
                if lk in HOP_BY_HOP_HEADERS or lk == "host" or lk == "content-length":
                    continue
                headers[key] = value

            if auto_auth and parsed.path != "/kabusapi/token" and "X-API-KEY" not in headers:
                try:
                    headers["X-API-KEY"] = token_cache.get()
                except Exception as e:
                    self._send_json(502, {"error": str(e)})
                    return

            upstream_url = target_base_url + self.path
            req = request.Request(upstream_url, data=body, headers=headers, method=self.command)

            try:
                with request.urlopen(req, timeout=timeout) as res:
                    data = res.read()
                    self.send_response(res.status)
                    self._copy_response_headers(res.headers, len(data))
                    self.end_headers()
                    self.wfile.write(data)
            except error.HTTPError as e:
                data = e.read()
                if e.code == 401 and auto_auth and parsed.path != "/kabusapi/token":
                    token_cache.clear()
                self.send_response(e.code)
                self._copy_response_headers(e.headers, len(data))
                self.end_headers()
                self.wfile.write(data)
            except error.URLError as e:
                self._send_json(502, {"error": f"upstream connection failed: {e}"})

        def _copy_response_headers(self, src_headers, content_length):
            sent_length = False
            for key, value in src_headers.items():
                lk = key.lower()
                if lk in HOP_BY_HOP_HEADERS:
                    continue
                if lk == "content-length":
                    sent_length = True
                    self.send_header(key, str(content_length))
                else:
                    self.send_header(key, value)
            if not sent_length:
                self.send_header("Content-Length", str(content_length))

        def log_message(self, fmt, *args):
            print(
                f"{self.address_string()} - {self.command} {self.path} - "
                + (fmt % args)
            )

    return ProxyHandler


def _parse_allowed(value):
    if not value:
        return set()
    return {part.strip() for part in value.split(",") if part.strip()}


def main():
    ap = argparse.ArgumentParser(description="LAN reverse proxy for kabu Station API")
    ap.add_argument("--host", default=os.getenv("KABU_PROXY_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("KABU_PROXY_PORT", "18180")))
    ap.add_argument("--target", default=os.getenv("KABU_TARGET_BASE_URL", DEFAULT_BASE_URL))
    ap.add_argument("--timeout", type=float, default=float(os.getenv("KABU_PROXY_TIMEOUT", "15")))
    ap.add_argument("--password-file", default=os.getenv("KABU_PASSWORD_FILE", DEFAULT_PASSWORD_FILE))
    ap.add_argument("--password-suffix", default=os.getenv("KABU_PASSWORD_SUFFIX", DEFAULT_PASSWORD_SUFFIX))
    ap.add_argument(
        "--allow",
        default=os.getenv("KABU_PROXY_ALLOW", "127.0.0.1,::1"),
        help="Comma-separated client IP allowlist. Empty allows all clients.",
    )
    ap.add_argument(
        "--no-auto-auth",
        action="store_true",
        help="Do not inject X-API-KEY automatically for /kabusapi requests.",
    )
    args = ap.parse_args()

    target = args.target.rstrip("/")
    allowed = _parse_allowed(args.allow)
    token_cache = TokenCache(target, args.password_file, args.password_suffix, args.timeout)
    handler = make_handler(
        target_base_url=target,
        token_cache=token_cache,
        allowed_clients=allowed,
        timeout=args.timeout,
        auto_auth=not args.no_auto_auth,
    )

    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"kabu proxy: http://{args.host}:{args.port} -> {target}")
    if allowed:
        print(f"allowed clients: {', '.join(sorted(allowed))}")
    else:
        print("allowed clients: all")
    print("proxied paths: /kabusapi/... ; local helpers: /health, /token/refresh")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping kabu proxy")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
