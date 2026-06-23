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
            '--universe-size', '10',
            '--top', '10',
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
  button.jp-kabu { background: #bf8700; }
  button.jp-kabu:active { background: #d29922; }
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
  .chat-btn {
    text-decoration: none;
    font-size: 13px;
    font-weight: 600;
    color: #c9d1d9;
    background: #21262d;
    border: 1px solid #30363d;
    padding: 6px 12px;
    border-radius: 8px;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    transition: background 0.2s, border-color 0.2s;
  }
  .chat-btn:active {
    background: #30363d;
    border-color: #8b949e;
  }
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
    .chat-btn {
      padding: 4px 8px;
      font-size: 11px;
      border-radius: 6px;
    }
  }
</style>
</head>
<body>
<header>
  <div style="display: flex; justify-content: space-between; align-items: center;">
    <h1>📈 Capital Flow Analysis</h1>
    <a href="/chat" class="chat-btn">💬 相談チャット</a>
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
<!--
<section>
  <div class="sec-label">kabuステーションAPI 代替版</div>
  <div class="btns">
    <button class="runbtn jp-kabu" data-market="jp_kabu">日本市場 kabu ETF代替</button>
  </div>
</section>
-->

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
  jp_holdings: '日本保有のみ',
  jp_kabu: '日本市場 kabu ETF代替'
};
let poll = null;

function setBusy(busy) {
  runbtns.forEach(b => b.disabled = busy);
}

function render(s) {
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  log.textContent = s.lines.length ? s.lines.join('\\n') : '（出力なし）';
  if (atBottom) log.scrollTop = log.scrollHeight;
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

CHAT_PAGE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>AI Advisor Chat</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    color-scheme: dark;
    --bg-color: #0d1117;
    --container-color: #161b22;
    --user-gradient: linear-gradient(135deg, #1f6feb, #388bfd);
    --advisor-bg: #21262d;
    --advisor-border: #30363d;
    --text-primary: #e6edf3;
    --text-secondary: #8b949e;
  }
  * {
    box-sizing: border-box;
    -webkit-tap-highlight-color: transparent;
  }
  html, body {
    margin: 0;
    padding: 0;
    height: 100dvh;
    background-color: var(--bg-color);
    color: var(--text-primary);
    font-family: 'Outfit', -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    overflow: hidden;
  }
  body {
    display: flex;
    flex-direction: column;
    padding-top: env(safe-area-inset-top);
    padding-bottom: env(safe-area-inset-bottom);
  }
  
  /* Header styling */
  header {
    height: 56px;
    background-color: var(--container-color);
    border-bottom: 1px solid var(--advisor-border);
    display: flex;
    align-items: center;
    padding: 0 16px;
    flex-shrink: 0;
  }
  .back-btn {
    text-decoration: none;
    color: var(--text-secondary);
    font-size: 20px;
    margin-right: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 36px;
    height: 36px;
    border-radius: 50%;
    transition: background 0.2s, color 0.2s;
  }
  .back-btn:active {
    background-color: var(--advisor-border);
    color: var(--text-primary);
  }
  header h1 {
    font-size: 17px;
    font-weight: 600;
    margin: 0;
    flex: 1;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .header-status {
    font-size: 11px;
    color: #388bfd;
    background: rgba(56, 139, 253, 0.15);
    padding: 2px 8px;
    border-radius: 12px;
    font-weight: 500;
  }

  /* Messages area */
  .chat-container {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 16px;
    scroll-behavior: smooth;
  }
  
  /* Message bubbles */
  .message {
    display: flex;
    flex-direction: column;
    max-width: 80%;
    opacity: 0;
    transform: translateY(10px);
    animation: fadeIn 0.3s ease-out forwards;
  }
  @keyframes fadeIn {
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }
  .message.user {
    align-self: flex-end;
  }
  .message.advisor {
    align-self: flex-start;
  }
  
  .bubble {
    padding: 12px 16px;
    border-radius: 18px;
    font-size: 15px;
    line-height: 1.5;
    word-break: break-all;
    white-space: pre-wrap;
  }
  .user .bubble {
    background: var(--user-gradient);
    color: #ffffff;
    border-bottom-right-radius: 4px;
    box-shadow: 0 4px 12px rgba(31, 111, 235, 0.2);
  }
  .advisor .bubble {
    background-color: var(--advisor-bg);
    color: var(--text-primary);
    border: 1px solid var(--advisor-border);
    border-bottom-left-radius: 4px;
  }
  
  .time {
    font-size: 10px;
    color: var(--text-secondary);
    margin-top: 4px;
    align-self: flex-end;
  }
  .advisor .time {
    align-self: flex-start;
  }
  
  /* Typing Indicator */
  .typing-indicator {
    display: none;
    align-items: center;
    gap: 4px;
    padding: 12px 16px;
    border-radius: 18px;
    background-color: var(--advisor-bg);
    border: 1px solid var(--advisor-border);
    border-bottom-left-radius: 4px;
    width: fit-content;
    align-self: flex-start;
  }
  .typing-dot {
    width: 6px;
    height: 6px;
    background-color: var(--text-secondary);
    border-radius: 50%;
    animation: typingBounce 1.4s infinite ease-in-out both;
  }
  .typing-dot:nth-child(1) { animation-delay: -0.32s; }
  .typing-dot:nth-child(2) { animation-delay: -0.16s; }
  @keyframes typingBounce {
    0%, 80%, 100% { transform: scale(0); }
    40% { transform: scale(1); }
  }

  /* Bottom Area (Quick actions + Input) */
  .footer-container {
    background-color: var(--container-color);
    border-top: 1px solid var(--advisor-border);
    padding: 12px 16px max(12px, env(safe-area-inset-bottom));
    display: flex;
    flex-direction: column;
    gap: 10px;
    flex-shrink: 0;
  }

  /* Quick Actions (horizontal scroll) */
  .quick-actions {
    display: flex;
    gap: 8px;
    overflow-x: auto;
    padding-bottom: 4px;
    scrollbar-width: none;
  }
  .quick-actions::-webkit-scrollbar {
    display: none;
  }
  .action-btn {
    flex-shrink: 0;
    background-color: var(--advisor-bg);
    border: 1px solid var(--advisor-border);
    color: var(--text-primary);
    padding: 8px 14px;
    border-radius: 20px;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: background 0.2s, border-color 0.2s;
  }
  .action-btn:active {
    background-color: var(--advisor-border);
    border-color: var(--text-secondary);
  }

  /* Input bar */
  .input-bar {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  .chat-input {
    flex: 1;
    background-color: var(--bg-color);
    border: 1px solid var(--advisor-border);
    color: var(--text-primary);
    padding: 12px 16px;
    border-radius: 24px;
    font-size: 15px;
    outline: none;
    font-family: inherit;
    transition: border-color 0.2s;
  }
  .chat-input:focus {
    border-color: #388bfd;
  }
  .send-btn {
    width: 44px;
    height: 44px;
    border-radius: 50%;
    background: var(--user-gradient);
    border: none;
    color: white;
    font-size: 18px;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    transition: opacity 0.2s;
  }
  .send-btn:active {
    opacity: 0.8;
  }
</style>
</head>
<body>
<header>
  <a href="/" class="back-btn">←</a>
  <h1>💬 AI Advisor <span class="header-status">Online</span></h1>
</header>

<div class="chat-container" id="chatContainer">
  <div class="message advisor">
    <div class="bubble">こんにちは！ポートフォリオの運用アドバイザーです。本日の取引状況やスイングトレード方針、引き継ぎノートについて何でもご相談ください。</div>
    <div class="time" id="greetingTime">12:00</div>
  </div>
  
  <div class="typing-indicator" id="typingIndicator">
    <div class="typing-dot"></div>
    <div class="typing-dot"></div>
    <div class="typing-dot"></div>
  </div>
</div>

<div class="footer-container">
  <div class="quick-actions">
    <button class="action-btn" onclick="sendQuickPrompt('本日の日本市場振り返り')">本日の日本市場振り返り</button>
    <button class="action-btn" onclick="sendQuickPrompt('今夜の米国株の見通し')">今夜の米国株の見通し</button>
    <button class="action-btn" onclick="sendQuickPrompt('引き継ぎノートの状況確認')">引き継ぎノートの状況確認</button>
    <button class="action-btn" onclick="sendQuickPrompt('米国保有銘柄の資金状況')">米国保有銘柄の資金状況</button>
  </div>
  
  <div class="input-bar">
    <input type="text" id="chatInput" class="chat-input" placeholder="メッセージを入力..." autocomplete="off" onkeydown="if(event.key==='Enter') sendMessage()">
    <button class="send-btn" onclick="sendMessage()">➔</button>
  </div>
</div>

<script>
const container = document.getElementById('chatContainer');
const input = document.getElementById('chatInput');
const typing = document.getElementById('typingIndicator');
const greetingTime = document.getElementById('greetingTime');

function formatTime(date) {
  const h = String(date.getHours()).padStart(2, '0');
  const m = String(date.getMinutes()).padStart(2, '0');
  return `${h}:${m}`;
}
greetingTime.textContent = formatTime(new Date());

function appendMessage(text, isUser) {
  const msgDiv = document.createElement('div');
  msgDiv.className = `message ${isUser ? 'user' : 'advisor'}`;
  
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = text;
  
  const timeDiv = document.createElement('div');
  timeDiv.className = 'time';
  timeDiv.textContent = formatTime(new Date());
  
  msgDiv.appendChild(bubble);
  msgDiv.appendChild(timeDiv);
  
  container.insertBefore(msgDiv, typing);
  container.scrollTop = container.scrollHeight;
}

function showTyping(show) {
  typing.style.display = show ? 'flex' : 'none';
  container.scrollTop = container.scrollHeight;
}

async function sendMessage() {
  const text = input.value.trim();
  if (!text) return;
  
  input.value = '';
  appendMessage(text, true);
  
  showTyping(true);
  
  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ message: text })
    });
    const data = await response.json();
    
    setTimeout(() => {
      showTyping(false);
      appendMessage(data.reply || data.message || "エラーが発生しました。", false);
    }, 600);
  } catch (error) {
    showTyping(false);
    appendMessage("サーバーとの接続に失敗しました。", false);
  }
}

function sendQuickPrompt(promptText) {
  input.value = promptText;
  sendMessage();
}
</script>
</body>
</html>
"""


# ── AI Advisor Integration ───────────────────────────────────────────────────
def _load_gemini_api_key():
    # 1. Read from os.environ
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key.strip()

    # 2. Try config/gemini_key.txt relative to HERE
    config_path = os.path.join(HERE, 'config', 'gemini_key.txt')
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                key = f.read().strip()
                if key:
                    return key
        except Exception:
            pass

    # 3. Try .env relative to HERE
    env_path = os.path.join(HERE, '.env')
    if os.path.exists(env_path):
        try:
            with open(env_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('GEMINI_API_KEY='):
                        parts = line.split('=', 1)
                        if len(parts) == 2:
                            key = parts[1].strip()
                            if (key.startswith('"') and key.endswith('"')) or (key.startswith("'") and key.endswith("'")):
                                key = key[1:-1].strip()
                            if key:
                                return key
        except Exception:
            pass

    return None


def _get_project_context():
    context_parts = []
    
    # 1. Read .agents/handover.md if it exists
    handover_path = os.path.join(HERE, '.agents', 'handover.md')
    if os.path.exists(handover_path):
        try:
            with open(handover_path, 'r', encoding='utf-8') as f:
                context_parts.append(f"--- handover.md ---\n{f.read()}")
        except Exception as e:
            context_parts.append(f"--- handover.md ---\nError reading: {e}")
            
    # 2. Read .agents/AGENTS.md if it exists
    agents_path = os.path.join(HERE, '.agents', 'AGENTS.md')
    if os.path.exists(agents_path):
        try:
            with open(agents_path, 'r', encoding='utf-8') as f:
                context_parts.append(f"--- AGENTS.md ---\n{f.read()}")
        except Exception as e:
            context_parts.append(f"--- AGENTS.md ---\nError reading: {e}")
            
    # 3. Find the latest .log file in logs/ (excluding launchd logs) and read its contents
    log_dir = os.path.join(HERE, 'logs')
    if os.path.exists(log_dir):
        try:
            logs = [f for f in os.listdir(log_dir)
                    if f.endswith('.log') and not f.startswith('launchd')]
            if logs:
                newest = max(logs, key=lambda f: os.path.getmtime(os.path.join(log_dir, f)))
                newest_path = os.path.join(log_dir, newest)
                with open(newest_path, 'r', encoding='utf-8') as f:
                    context_parts.append(f"--- Latest Log ({newest}) ---\n{f.read()}")
            else:
                context_parts.append("--- Latest Log ---\nNo logs found.")
        except Exception as e:
            context_parts.append(f"--- Latest Log ---\nError reading logs: {e}")
    else:
        context_parts.append("--- Latest Log ---\nlogs/ directory does not exist.")
        
    return "\n\n".join(context_parts)


def _call_gemini_api(api_key, context, message):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
    
    prompt_text = f"System Context:\n{context}\n\nUser Question: {message}"
    
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": prompt_text
                    }
                ]
            }
        ],
        "systemInstruction": {
            "parts": [
                {
                    "text": "あなたは優秀な資産運用アドバイザーおよびテクニカルリード補佐です。ユーザーから提供されたプロジェクト状況（handover.md）、ルール（AGENTS.md）、最新のログファイルの内容を完全に把握した上で、質問に日本語で親身かつ的確に答えてください。必要に応じて次のアクションや改善案を提案してください。"
                }
            ]
        }
    }
    
    headers = {
        "Content-Type": "application/json"
    }
    
    req_data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=req_data, headers=headers, method='POST')
    
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            res_data = response.read().decode('utf-8')
            res_json = json.loads(res_data)
            
            candidates = res_json.get("candidates", [])
            if candidates:
                content = candidates[0].get("content", {})
                parts = content.get("parts", [])
                if parts:
                    return parts[0].get("text", "")
            return "エラー: レスポンスを解析できませんでした。"
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode('utf-8')
            err_json = json.loads(err_body)
            err_msg = err_json.get("error", {}).get("message", str(e))
        except Exception:
            err_msg = str(e)
        return f"Gemini API エラー (HTTP {e.code}): {err_msg}"
    except urllib.error.URLError as e:
        return f"Gemini API 接続エラー: {e.reason}"
    except Exception as e:
        return f"システムエラー: {e}"


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
        elif path == '/chat':
            self._send(200, CHAT_PAGE)
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
        elif path == '/api/chat':
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            try:
                payload = json.loads(post_data.decode('utf-8'))
                msg_text = payload.get('message', '')
            except Exception:
                msg_text = ''
            
            api_key = _load_gemini_api_key()
            if not api_key:
                reply = "エラー: APIキーが設定されていません。config/gemini_key.txt にキーを保存してください。"
            else:
                context = _get_project_context()
                reply = _call_gemini_api(api_key, context, msg_text)
                
            self._send(200, json.dumps({'reply': reply}), 'application/json; charset=utf-8')
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
