#!/usr/bin/env python3
"""
朝の資金分析  ローカル Web サーバー
iPhone など同一 Wi-Fi の端末から実行ボタンを押して改善版分析を起動する。

依存ライブラリなし(標準ライブラリのみ)。

使い方:
  python3 server.py                # http://0.0.0.0:8080 で起動
  python3 server.py --port 9000    # ポート変更

起動後、iPhone のブラウザで  http://<MacのIP>:8080  を開く。
MacのIP は起動時に表示される。
"""
import sys, os, json, time, socket, argparse, threading, subprocess
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, 'logs')
PYTHON = sys.executable

# 分析ボタンごとのスクリプトと起動引数
MARKETS = {
    'us': {
        'label': '米国市場',
        'script': os.path.join(HERE, 'analysis_enhanced.py'),
        'args': ['--market', 'us'],
    },
    'jp': {
        'label': '日本市場',
        'script': os.path.join(HERE, 'analysis_enhanced.py'),
        'args': ['--market', 'jp'],
    },
    'us_holdings': {
        'label': '米国保有のみ',
        'script': os.path.join(HERE, 'analysis_enhanced.py'),
        'args': ['--market', 'us', '--holdings-only', '--top', '10'],
    },
    'jp_holdings': {
        'label': '日本保有のみ',
        'script': os.path.join(HERE, 'analysis_enhanced.py'),
        'args': ['--market', 'jp', '--holdings-only', '--top', '10'],
    },
    'jp_kabu': {
        'label': '日本市場 kabu ETF代替',
        'script': os.path.join(HERE, 'kabu_japan_analysis.py'),
        'args': [
            '--base-url', os.getenv('KABU_BASE_URL', 'http://10.215.1.57:18180'),
            '--no-token-required',
            '--etf-only',
            '--universe-size', '15',
            '--top', '0',
        ],
    },
}

# ── 実行状態 (単一ジョブ) ────────────────────────────────────────────────────
_lock = threading.Lock()
_state = {
    'running': False,
    'market': None,         # 実行中/直近の市場キー
    'lines': [],            # 出力行(逐次追記)
    'started_at': None,
    'finished_at': None,
    'returncode': None,
}


def _run_analysis(market):
    """別スレッドで対象市場のスクリプトを起動し、出力を逐次 _state に蓄積。"""
    m = MARKETS[market]
    cmd = [PYTHON, '-u', m['script']] + m['args']
    with _lock:
        _state['market'] = market
        _state['lines'] = [f'[{m["label"]}]  $ {" ".join(cmd)}', '']
        _state['started_at'] = datetime.now().isoformat()
        _state['finished_at'] = None
        _state['returncode'] = None
    try:
        proc = subprocess.Popen(
            cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env={**os.environ, 'PYTHONUNBUFFERED': '1'})
        for line in proc.stdout:
            with _lock:
                _state['lines'].append(line.rstrip('\n'))
        proc.wait()
        rc = proc.returncode
    except Exception as e:
        with _lock:
            _state['lines'].append(f'[server] 起動失敗: {e}')
        rc = -1
    finally:
        with _lock:
            _state['running'] = False
            _state['finished_at'] = datetime.now().isoformat()
            _state['returncode'] = rc


# ── HTML ─────────────────────────────────────────────────────────────────────
PAGE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Capital Flow Analysis</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, sans-serif;
         background: #0d1117; color: #e6edf3; padding: env(safe-area-inset-top) 16px 32px; }
  header { padding: 20px 0 12px; }
  h1 { font-size: 20px; margin: 0; }
  .sub { color: #8b949e; font-size: 13px; margin-top: 4px; }
  section { margin: 18px 0; }
  .sec-label { font-size: 12px; font-weight: 700; color: #8b949e;
               text-transform: uppercase; letter-spacing: .04em; margin-bottom: 8px; }
  .btns { display: flex; gap: 12px; }
  button { flex: 1; padding: 18px 12px; font-size: 17px; font-weight: 700;
           border: none; border-radius: 14px; color: #fff;
           cursor: pointer; transition: background .15s; }
  button.us { background: #1f6feb; }
  button.us:active { background: #388bfd; }
  button.jp { background: #238636; }
  button.jp:active { background: #2ea043; }
  button.us-holdings { background: #388bfd; }
  button.us-holdings:active { background: #58a6ff; }
  button.jp-holdings { background: #2ea043; }
  button.jp-holdings:active { background: #56d364; }
  button:disabled { background: #30363d !important; color: #8b949e; cursor: not-allowed; }
  /* 改善版エリア(実験中) */
  .variants { display: flex; flex-wrap: wrap; gap: 12px; }
  .variants .placeholder { flex: 1; padding: 22px 14px; border: 1px dashed #30363d;
           border-radius: 14px; color: #6e7681; font-size: 13px; text-align: center; }
  button.variant { background: #6e40c9; }
  button.variant:active { background: #8957e5; }
  .status { display: flex; align-items: center; gap: 8px; }
  .refresh { flex: none; margin-left: auto; padding: 8px 14px; font-size: 13px;
             font-weight: 600; border-radius: 10px; background: #21262d; color: #c9d1d9; }
  .refresh:active { background: #30363d; }
  .status { display: flex; align-items: center; gap: 8px; font-size: 14px;
            margin: 8px 0; min-height: 22px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: #8b949e; flex: none; }
  .dot.run { background: #d29922; animation: pulse 1s infinite; }
  .dot.ok  { background: #238636; }
  .dot.err { background: #da3633; }
  @keyframes pulse { 50% { opacity: .3; } }

  .back-link { font-size: 14px; color: #58a6ff; text-decoration: none; font-weight: 600; padding: 6px 10px; border-radius: 8px; background: #21262d; }
  .back-link:active { background: #30363d; }

  pre { background: #010409; border: 1px solid #30363d; border-radius: 12px;
        padding: 14px; font-size: 12px; line-height: 1.5; overflow-x: auto;
        white-space: pre; min-height: 200px; max-height: 65vh; overflow-y: auto;
        font-family: ui-monospace, "SF Mono", Menlo, monospace; }
  @media (max-width: 480px) {
    html, body {
      height: 100dvh;
      overflow: hidden;
    }
    body {
      display: flex;
      flex-direction: column;
      padding: env(safe-area-inset-top) 6px max(8px, env(safe-area-inset-bottom) - 16px);
    }
    header {
      padding: 8px 0 4px;
    }
    h1 {
      font-size: 18px;
    }
    .sub {
      font-size: 11px;
      margin-top: 2px;
    }
    section {
      margin: 6px 0;
    }
    .sec-label {
      font-size: 11px;
      margin-bottom: 4px;
    }
    .btns {
      gap: 8px;
    }
    button {
      padding: 12px 8px;
      font-size: 14px;
      border-radius: 10px;
    }
    .status {
      margin: 6px 0;
      font-size: 12px;
      min-height: 18px;
    }
    .refresh {
      padding: 6px 10px;
      font-size: 11px;
      border-radius: 8px;
    }
    pre {
      flex: 1;
      margin: 0;
      min-height: 0;
      max-height: none;
      font-size: 11px;
      padding: 6px;
      border-radius: 8px;
    }

  }
</style>
</head>
<body>
<header>
  <div style="display: flex; justify-content: space-between; align-items: center;">
    <h1>📈 Capital Flow Analysis</h1>
    <a href="/kabu" class="back-link">kabu分析</a>
  </div>
  <div class="sub">食込率 + 連続性 + 時間外確認 + ETF分離</div>
</header>
<section>
  <div class="sec-label">改善版分析</div>
  <div class="btns">
    <button class="runbtn us" data-market="us">米国市場</button>
    <button class="runbtn jp" data-market="jp">日本市場</button>
  </div>
</section>
<section>
  <div class="sec-label">保有のみ分析</div>
  <div class="btns">
    <button class="runbtn us-holdings" data-market="us_holdings">米国保有のみ</button>
    <button class="runbtn jp-holdings" data-market="jp_holdings">日本保有のみ</button>
  </div>
</section>

<div class="status">
  <span id="dot" class="dot"></span><span id="msg">待機中</span>
  <button id="refresh" class="refresh">🔄 更新</button>
</div>
<pre id="log">（ここに実行ログが表示されます）</pre>

<script>
const runbtns = [...document.querySelectorAll('.runbtn')];
const dot = document.getElementById('dot');
const msg = document.getElementById('msg');
const log = document.getElementById('log');
const LABEL = {
  us: '米国市場',
  jp: '日本市場',
  us_holdings: '米国保有のみ',
  jp_holdings: '日本保有のみ'
};
let poll = null;

function setBusy(busy) {
  runbtns.forEach(b => b.disabled = busy);
}

function render(s) {
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 100;
  const isRunning = s.running;
  log.textContent = s.lines.length ? s.lines.join('\\n') : '（出力なし）';
  if (isRunning || atBottom) {
    setTimeout(() => {
      log.scrollTop = log.scrollHeight;
    }, 50);
  }
  const name = LABEL[s.market] || '';
  if (s.running) {
    setBusy(true);
    dot.className = 'dot run'; msg.textContent = name + ' 分析中…';
  } else {
    setBusy(false);
    if (s.returncode === 0) { dot.className = 'dot ok'; msg.textContent = name + ' 完了'; }
    else if (s.returncode === null) { dot.className = 'dot'; msg.textContent = '待機中'; }
    else { dot.className = 'dot err'; msg.textContent = name + ' 失敗 (code ' + s.returncode + ')'; }
    if (poll) { clearInterval(poll); poll = null; }
  }
}

function tsPath(path) {
  return path + (path.includes('?') ? '&' : '?') + 't=' + Date.now();
}

async function refresh() {
  try { render(await (await fetch(tsPath('/status'), { cache: 'no-store' })).json()); }
  catch (e) { msg.textContent = '接続エラー'; }
}

// 更新ボタン: HTML/JSごとタイムスタンプ付きで再読み込みする
function manualRefresh() {
  const btn = document.getElementById('refresh');
  btn.disabled = true;
  btn.textContent = '再読み込み…';
  window.location.replace(tsPath('/'));
}

async function run(market) {
  setBusy(true);
  await fetch(tsPath('/run?market=' + encodeURIComponent(market)), { method: 'POST', cache: 'no-store' });
  if (!poll) poll = setInterval(refresh, 1000);
  refresh();
}

runbtns.forEach(b => b.onclick = () => run(b.dataset.market));
document.getElementById('refresh').onclick = manualRefresh;

refresh();
// 実行中に取りこぼさないよう、開いている間は軽くポーリング
setInterval(() => { if (!poll) refresh(); }, 5000);
</script>
</body>
</html>
"""


KABU_PAGE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>kabu Station ETF Analysis</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, sans-serif;
         background: #0d1117; color: #e6edf3; padding: env(safe-area-inset-top) 16px 32px; }
  header { padding: 20px 0 12px; display: flex; justify-content: space-between; align-items: center; }
  .title-group h1 { font-size: 20px; margin: 0; }
  .title-group .sub { color: #8b949e; font-size: 13px; margin-top: 4px; }
  .back-link { font-size: 14px; color: #58a6ff; text-decoration: none; font-weight: 600; padding: 6px 10px; border-radius: 8px; background: #21262d; }
  .back-link:active { background: #30363d; }
  section { margin: 18px 0; }
  .sec-label { font-size: 12px; font-weight: 700; color: #8b949e;
               text-transform: uppercase; letter-spacing: .04em; margin-bottom: 8px; }
  .btns { display: flex; gap: 12px; }
  button { flex: 1; padding: 18px 12px; font-size: 17px; font-weight: 700;
           border: none; border-radius: 14px; color: #fff;
           cursor: pointer; transition: background .15s; }
  button.jp-kabu { background: #bf8700; }
  button.jp-kabu:active { background: #d29922; }
  button:disabled { background: #30363d !important; color: #8b949e; cursor: not-allowed; }
  .status { display: flex; align-items: center; gap: 8px; }
  .refresh { flex: none; margin-left: auto; padding: 8px 14px; font-size: 13px;
             font-weight: 600; border-radius: 10px; background: #21262d; color: #c9d1d9; }
  .refresh:active { background: #30363d; }
  .status { display: flex; align-items: center; gap: 8px; font-size: 14px;
            margin: 8px 0; min-height: 22px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: #8b949e; flex: none; }
  .dot.run { background: #d29922; animation: pulse 1s infinite; }
  .dot.ok  { background: #238636; }
  .dot.err { background: #da3633; }
  @keyframes pulse { 50% { opacity: .3; } }

  pre { background: #010409; border: 1px solid #30363d; border-radius: 12px;
        padding: 14px; font-size: 12px; line-height: 1.5; overflow-x: auto;
        white-space: pre; min-height: 200px; max-height: 65vh; overflow-y: auto;
        font-family: ui-monospace, "SF Mono", Menlo, monospace; }
  @media (max-width: 480px) {
    html, body {
      height: 100dvh;
      overflow: hidden;
    }
    body {
      display: flex;
      flex-direction: column;
      padding: env(safe-area-inset-top) 6px max(8px, env(safe-area-inset-bottom) - 16px);
    }
    header {
      padding: 8px 0 4px;
    }
    h1 {
      font-size: 18px;
    }
    .sub {
      font-size: 11px;
      margin-top: 2px;
    }
    section {
      margin: 6px 0;
    }
    .sec-label {
      font-size: 11px;
      margin-bottom: 4px;
    }
    .btns {
      gap: 8px;
    }
    button {
      padding: 12px 8px;
      font-size: 14px;
      border-radius: 10px;
    }
    .status {
      margin: 6px 0;
      font-size: 12px;
      min-height: 18px;
    }
    .refresh {
      padding: 6px 10px;
      font-size: 11px;
      border-radius: 8px;
    }
    pre {
      flex: 1;
      margin: 0;
      min-height: 0;
      max-height: none;
      font-size: 11px;
      padding: 6px;
      border-radius: 8px;
    }
  }
</style>
</head>
<body>
<header>
  <div class="title-group">
    <h1>📈 kabu ETF代替分析</h1>
    <div class="sub">kabuステーションAPI 経由で板圧力・需給分析を行います</div>
  </div>
  <a href="/" class="back-link">メイン分析</a>
</header>
<section>
  <div class="sec-label">代替スキャン</div>
  <div class="btns">
    <button class="runbtn jp-kabu" data-market="jp_kabu">日本市場 kabu ETF代替</button>
  </div>
</section>

<div class="status">
  <span id="dot" class="dot"></span><span id="msg">待機中</span>
  <button id="refresh" class="refresh">🔄 更新</button>
</div>
<pre id="log">（ここに実行ログが表示されます）</pre>

<script>
const runbtns = [...document.querySelectorAll('.runbtn')];
const dot = document.getElementById('dot');
const msg = document.getElementById('msg');
const log = document.getElementById('log');
const LABEL = {
  jp_kabu: '日本市場 kabu ETF代替'
};
let poll = null;

function setBusy(busy) {
  runbtns.forEach(b => b.disabled = busy);
}

function render(s) {
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 100;
  const isRunning = s.running;
  log.textContent = s.lines.length ? s.lines.join('\\n') : '（出力なし）';
  if (isRunning || atBottom) {
    setTimeout(() => {
      log.scrollTop = log.scrollHeight;
    }, 50);
  }
  const name = LABEL[s.market] || '';
  if (s.running) {
    setBusy(true);
    dot.className = 'dot run'; msg.textContent = name + ' 分析中…';
  } else {
    setBusy(false);
    if (s.returncode === 0) { dot.className = 'dot ok'; msg.textContent = name + ' 完了'; }
    else if (s.returncode === null) { dot.className = 'dot'; msg.textContent = '待機中'; }
    else { dot.className = 'dot err'; msg.textContent = name + ' 失敗 (code ' + s.returncode + ')'; }
    if (poll) { clearInterval(poll); poll = null; }
  }
}

function tsPath(path) {
  return path + (path.includes('?') ? '&' : '?') + 't=' + Date.now();
}

async function refresh() {
  try { render(await (await fetch(tsPath('/status'), { cache: 'no-store' })).json()); }
  catch (e) { msg.textContent = '接続エラー'; }
}

function manualRefresh() {
  const btn = document.getElementById('refresh');
  btn.disabled = true;
  btn.textContent = '再読み込み…';
  window.location.replace(tsPath('/kabu'));
}

async function run(market) {
  setBusy(true);
  await fetch(tsPath('/run?market=' + encodeURIComponent(market)), { method: 'POST', cache: 'no-store' });
  if (!poll) poll = setInterval(refresh, 1000);
  refresh();
}

runbtns.forEach(b => b.onclick = () => run(b.dataset.market));
document.getElementById('refresh').onclick = manualRefresh;

refresh();
setInterval(() => { if (!poll) refresh(); }, 5000);
</script>
</body>
</html>
"""

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype='text/html; charset=utf-8'):
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/' or path.startswith('/index'):
            self._send(200, PAGE)
        elif path == '/kabu':
            self._send(200, KABU_PAGE)

        elif path == '/latest':
            # logs内の最新の分析ログ(launchd等は除外)を返す
            try:
                logs = [f for f in os.listdir(LOG_DIR)
                        if f.endswith('.log') and not f.startswith('launchd')]
                if logs:
                    newest = max(logs, key=lambda f: os.path.getmtime(
                        os.path.join(LOG_DIR, f)))
                    text = open(os.path.join(LOG_DIR, newest),
                                encoding='utf-8').read()
                    body = json.dumps({'file': newest, 'lines': text.splitlines()})
                else:
                    body = json.dumps({'file': None, 'lines': []})
            except Exception as e:
                body = json.dumps({'file': None, 'lines': [f'読込エラー: {e}']})
            self._send(200, body, 'application/json; charset=utf-8')
        elif path == '/status':
            with _lock:
                snap = json.dumps({
                    'running': _state['running'],
                    'market': _state['market'],
                    'lines': _state['lines'],
                    'returncode': _state['returncode'],
                    'started_at': _state['started_at'],
                    'finished_at': _state['finished_at'],
                })
            self._send(200, snap, 'application/json; charset=utf-8')
        elif path == '/api/restart':
            client_ip = self.client_address[0]
            if client_ip not in ('127.0.0.1', '::1', 'localhost'):
                self._send(403, 'Forbidden: localhost only')
                return
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
            self.send_header('Connection', 'close')
            response_bytes = json.dumps({'ok': True, 'message': 'restarting'}).encode('utf-8')
            self.send_header('Content-Length', str(len(response_bytes)))
            self.end_headers()
            self.wfile.write(response_bytes)
            self.wfile.flush()
            
            time.sleep(0.5)
            os._exit(0)
        else:
            self._send(404, 'not found')

    def do_POST(self):
        path, _, query = self.path.partition('?')
        if path == '/run':
            params = parse_qs(query)
            market = (params.get('market') or ['us'])[0]
            if market not in MARKETS:
                self._send(400, json.dumps({'error': 'unknown market'}),
                           'application/json')
                return
            with _lock:
                if _state['running']:
                    self._send(409, json.dumps({'error': 'already running'}),
                               'application/json')
                    return
                _state['running'] = True
            threading.Thread(target=_run_analysis, args=(market,), daemon=True).start()
            self._send(200, json.dumps({'ok': True, 'market': market}), 'application/json')

        else:
            self._send(404, 'not found')

    def handle(self):
        # クライアント(スマホ等)がポーリング接続を途中で切るのは正常。
        # ConnectionReset/BrokenPipe はノイズなので握りつぶす。
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass

    def log_message(self, *args):
        pass  # アクセスログ抑制


def _local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='朝の資金分析 ローカルWebサーバー')
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--host', default='0.0.0.0')
    args = ap.parse_args()

    ip = _local_ip()
    print('=' * 60)
    print('  📈 朝の資金分析 Web サーバー起動')
    print('=' * 60)
    print(f'  Mac:    http://127.0.0.1:{args.port}')
    print(f'  iPhone: http://{ip}:{args.port}   (同一 Wi-Fi)')
    print('  停止: Ctrl+C')
    print('=' * 60)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n停止しました')
        httpd.shutdown()
