# kabu Station proxy

This directory contains the Windows-side kabu Station API bridge.

The main Trading project is operated on Mac with moomoo OpenD/API. kabu Station
only runs on Windows, so this Windows folder acts as a thin gateway to expose
local kabu Station API results to the Mac development environment.

## Current Status

- Windows kabu Station API is reachable locally at `http://localhost:18080`.
- API password is stored locally in `kabu_station_server/config/kabu_password.txt`.
- The production password suffix is `prod`.
- `kabu_check.py` can acquire a token and fetch `/kabusapi/board/7203@1`.
- `kabu_positions.py` can fetch Japanese spot positions with `/kabusapi/positions`.
- `kabu_proxy.py` is a generic reverse proxy for every `/kabusapi/...` endpoint.
- The proxy was tested from Windows and from Mac.

Known addresses at the time of setup:

- Windows: `10.215.1.57`
- Mac: `10.215.1.136`
- Proxy port: `18180`

## Design

The proxy intentionally does not maintain a hand-written allowlist of kabu
Station API endpoints. Any path under `/kabusapi/...` is forwarded to the local
kabu Station API so that the proxy does not drift from the official API
reference.

Requests flow like this:

```text
Mac analysis code
  -> http://10.215.1.57:18180/kabusapi/...
Windows kabu_proxy.py
  -> http://localhost:18080/kabusapi/...
kabu Station
```

If the caller does not provide `X-API-KEY`, the proxy obtains and injects a
token automatically using the local password file. The Mac does not need to
store the kabu Station API password.

Local helper endpoints:

- `/health`
- `/token/refresh`

Only `/kabusapi/...` is proxied to kabu Station.

## Start Proxy

Run from the project root on Windows:

```powershell
python kabu_station_server\kabu_proxy.py --host 0.0.0.0 --port 18180 --allow 127.0.0.1,::1,10.215.1.136
```

Or use the helper scripts:

```powershell
powershell -ExecutionPolicy Bypass -File kabu_station_server\kabu_proxy_start.ps1
powershell -ExecutionPolicy Bypass -File kabu_station_server\kabu_proxy_stop.ps1
powershell -ExecutionPolicy Bypass -File kabu_station_server\kabu_proxy_restart.ps1
powershell -ExecutionPolicy Bypass -File kabu_station_server\kabu_git_pull.ps1
```

For local-only testing:

```powershell
python kabu_station_server\kabu_proxy.py --host 127.0.0.1 --port 18180
```

The current allowlist includes localhost and the Mac IP. Keep `--allow` set
unless there is a specific reason to expose it to the whole LAN.

## Mac-Side Checks

From the Mac:

```bash
curl http://10.215.1.57:18180/health
curl http://10.215.1.57:18180/kabusapi/board/7203@1
curl "http://10.215.1.57:18180/kabusapi/positions?product=1&addinfo=true"
```

Mac-side Windows management wrapper:

```bash
python3 scripts/kabu_windows.py status
python3 scripts/kabu_windows.py pull
python3 scripts/kabu_windows.py restart
python3 scripts/kabu_windows.py health
python3 scripts/kabu_windows.py board --symbol 7203 --exchange 1
```

These calls should not require `X-API-KEY` from the Mac because the proxy
injects it.

Mac-side API contract:

```text
GET  /health
GET  /token/refresh
ANY  /kabusapi/...  -> forwards to local kabu Station /kabusapi/...
```

The Mac side should call this proxy over HTTP. It should not import or share
Python modules from `kabu_station_server`. If `X-API-KEY` is omitted, the
Windows proxy injects a token from the local Windows password file.

## Windows-Side Checks

Token and board:

```powershell
python kabu_station_server\kabu_check.py --symbol 7203 --exchange 1
```

Proxy-backed board check without a local token:

```bash
python kabu_station_server/kabu_check.py --base-url http://10.215.1.57:18180 --no-token-required --symbol 7203 --exchange 1
```

Positions:

```powershell
python kabu_station_server\kabu_positions.py --product 1 --addinfo
```

Proxy health:

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:18180/health'
```

Proxy board:

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:18180/kabusapi/board/7203@1'
```

## Files

- `kabu_station_client.py`: Windows-side stdlib client for local kabu Station checks.
- `kabu_check.py`: Token + board check, also appends board snapshots to CSV.
- `kabu_positions.py`: Positions check and console display.
- `kabu_proxy.py`: Generic `/kabusapi/...` reverse proxy for Mac access.
- `config/README.md`: Local secret placement note.
- `config/kabu_password.txt`: Local ignored secret file, not committed.

## Security Notes

- Do not commit `kabu_station_server/config/kabu_password.txt`.
- Do not expose the proxy outside the trusted LAN.
- Use `--allow` to restrict clients. At setup time, the Mac client is
  `10.215.1.136`.
- Windows Firewall only needs to allow the proxy port, currently `18180`.
- The proxy forwards kabu Station API requests broadly by design, so network
  access control is the main safety boundary.

## Next Step

Keep the Mac repository as the main place for strategy design and analysis.
Use this Windows proxy as the kabu Station access bridge. Mac-side code can call
the proxy with ordinary HTTP requests and compare kabu-derived data against the
existing moomoo signals.
