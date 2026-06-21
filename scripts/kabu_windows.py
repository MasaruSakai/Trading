#!/usr/bin/env python3
"""Mac-side wrapper for managing the Windows kabu Station proxy over SSH."""
import argparse
import base64
import subprocess
import sys
import urllib.error
import urllib.request


DEFAULT_SSH_HOST = "envyx360"
DEFAULT_WINDOWS_PROJECT = r"C:\Users\sbrms\Projects\Trading"
DEFAULT_PROXY_URL = "http://10.215.1.57:18180"


def run(cmd, check=True):
    print("+ " + " ".join(cmd))
    return subprocess.run(cmd, check=check)


def ssh_powershell(host, project, script, extra_args=None):
    extra_args = extra_args or []
    script_path = f"{project}\\kabu_station\\{script}"
    ps_cmd = (
        "Set-Location -LiteralPath " + quote_ps(project) + "; "
        "& " + quote_ps(script_path)
    )
    if extra_args:
        ps_cmd += " " + " ".join(extra_args)
    return ssh_powershell_command(host, ps_cmd)


def ssh_powershell_command(host, command):
    full_command = "$ProgressPreference='SilentlyContinue'; " + command
    encoded = base64.b64encode(full_command.encode("utf-16le")).decode("ascii")
    remote = (
        "powershell -NoProfile -ExecutionPolicy Bypass "
        f"-OutputFormat Text -EncodedCommand {encoded}"
    )
    return run(["ssh", host, remote])


def http_get(url, timeout=8):
    print("+ GET " + url)
    with urllib.request.urlopen(url, timeout=timeout) as res:
        data = res.read().decode("utf-8", errors="replace")
        print(f"HTTP {res.status}")
        print(data[:2000])
        return res.status, data


def cmd_pull(args):
    extra = []
    if args.git:
        extra += ["-Git", quote_ps(args.git)]
    if args.branch:
        extra += ["-Branch", quote_ps(args.branch)]
    return ssh_powershell(args.ssh_host, args.windows_project, "kabu_git_pull.ps1", extra)


def cmd_start(args):
    extra = proxy_args(args)
    return ssh_powershell(args.ssh_host, args.windows_project, "kabu_proxy_start.ps1", extra)


def cmd_stop(args):
    extra = ["-Port", str(args.port)]
    return ssh_powershell(args.ssh_host, args.windows_project, "kabu_proxy_stop.ps1", extra)


def cmd_restart(args):
    extra = proxy_args(args)
    return ssh_powershell(args.ssh_host, args.windows_project, "kabu_proxy_restart.ps1", extra)


def cmd_status(args):
    ps_cmd = (
        "Set-Location -LiteralPath " + quote_ps(args.windows_project) + "; "
        "Write-Output '== git =='; "
        "git status --short; "
        "git log --oneline -3; "
        "Write-Output '== proxy port =='; "
        "netstat -ano | findstr 18180; "
        "Write-Output '== kabu api port =='; "
        "netstat -ano | findstr 18080"
    )
    return ssh_powershell_command(args.ssh_host, ps_cmd)


def cmd_health(args):
    http_get(args.proxy_url.rstrip("/") + "/health", timeout=args.timeout)
    return None


def cmd_board(args):
    url = args.proxy_url.rstrip("/") + f"/kabusapi/board/{args.symbol}@{args.exchange}"
    http_get(url, timeout=args.timeout)
    return None


def quote_ps(value):
    text = str(value)
    return "'" + text.replace("'", "''") + "'"


def proxy_args(args):
    extra = [
        "-HostName", quote_ps(args.host_name),
        "-Port", str(args.port),
    ]
    if args.allow:
        extra += ["-Allow", quote_ps(args.allow)]
    if args.target:
        extra += ["-Target", quote_ps(args.target)]
    if args.python:
        extra += ["-Python", quote_ps(args.python)]
    return extra


def add_common(ap):
    ap.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    ap.add_argument("--windows-project", default=DEFAULT_WINDOWS_PROJECT)
    ap.add_argument("--proxy-url", default=DEFAULT_PROXY_URL)
    ap.add_argument("--timeout", type=float, default=8)


def add_proxy_options(ap):
    ap.add_argument("--host-name", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=18180)
    ap.add_argument("--allow", default="")
    ap.add_argument("--target", default="")
    ap.add_argument("--python", default="python")


def main():
    parser = argparse.ArgumentParser(description="Manage Windows kabu proxy from Mac")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("pull", help="Run git pull on Windows")
    add_common(p)
    p.add_argument("--git", default="")
    p.add_argument("--branch", default="")
    p.set_defaults(func=cmd_pull)

    for name, func, help_text in (
        ("start", cmd_start, "Start proxy on Windows"),
        ("restart", cmd_restart, "Restart proxy on Windows"),
    ):
        p = sub.add_parser(name, help=help_text)
        add_common(p)
        add_proxy_options(p)
        p.set_defaults(func=func)

    p = sub.add_parser("stop", help="Stop proxy on Windows")
    add_common(p)
    p.add_argument("--port", type=int, default=18180)
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("status", help="Show Windows git/proxy status")
    add_common(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("health", help="Check proxy health from Mac")
    add_common(p)
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("board", help="Fetch board through proxy from Mac")
    add_common(p)
    p.add_argument("--symbol", default="7203")
    p.add_argument("--exchange", type=int, default=1)
    p.set_defaults(func=cmd_board)

    args = parser.parse_args()
    try:
        result = args.func(args)
        if result is not None and result.returncode:
            return result.returncode
        return 0
    except subprocess.CalledProcessError as e:
        return e.returncode
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"HTTP check failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
