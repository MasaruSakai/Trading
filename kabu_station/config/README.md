# Local config

Put kabu Station API secrets in local files under this directory.

Example:

```text
config/kabu_password.txt
```

`kabu_station/config/*.txt` and `kabu_station/config/*.json` are ignored by git.
Do not commit real API passwords or tokens.

The proxy server can expose the local kabu Station API to the Mac:

```powershell
python kabu_station\kabu_proxy.py --host 0.0.0.0 --allow 127.0.0.1,::1,<MAC_IP>
```

It forwards every `/kabusapi/...` endpoint to local kabu Station and injects
`X-API-KEY` automatically when the caller does not provide one.
