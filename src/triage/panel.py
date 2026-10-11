"""The control room: one page with the whole state of the system and the levers, served
on the Mac (`triage panel`, kept alive by launchd) and reachable from the phone over
Tailscale. Status refreshes every 3 seconds; actions start the same commands you would
type (`triage run`, `prune`, `undo`, `blast` …) as child processes and stream their
output; Stop interrupts whatever run holds the lock. No framework, one file."""

from __future__ import annotations

import html
import json
import logging
import mimetypes
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import control, ops, progress
from .config import Config, load_config

log = logging.getLogger(__name__)

ACTIONS = {
    # action: (arguments after `triage`, runs in the background?)
    "run": (["run"], True),
    "run_shadow": (["run", "--shadow"], True),
    "run_sync": (["run", "--sync"], True),
    "prune_dry": (["prune", "--dry-run"], False),
    "prune": (["prune"], True),
    "undo": (["undo"], False),
    "bankruptcy_dry": (["bankruptcy", "--dry-run"], False),
    "bankruptcy": (["bankruptcy", "--yes"], True),
    "blast": (["blast", "--yes"], True),
    "weekly": (["weekly"], True),
    "report": (["report"], False),
    "dashboard": (["dashboard"], False),
    "doctor": (["doctor"], False),
}


class Child:
    """One background command at a time, its output kept for the page."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.args: list[str] = []
        self.started = 0.0
        self.ended = 0.0
        self.code: int | None = None
        self.output: deque[str] = deque(maxlen=400)
        self.lock = threading.Lock()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, args: list[str], cwd: Path, env: dict) -> None:
        with self.lock:
            if self.alive():
                raise RuntimeError("a command is already running")
            self.proc = subprocess.Popen(args, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, bufsize=1)
            self.args, self.started, self.ended, self.code = args, time.time(), 0.0, None
            self.output.clear()
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                for piece in raw.replace("\r", "\n").split("\n"):
                    if piece.strip():
                        self.output.append(piece.rstrip()[:300])
        finally:
            proc.stdout.close()  # one descriptor per command would otherwise stay open for the panel's life
        proc.wait()
        with self.lock:
            self.code, self.ended = proc.returncode, time.time()

    def stop(self) -> str:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return "no command is running here"
        proc.send_signal(2)  # SIGINT: the same as Ctrl-C in the terminal
        try:
            proc.wait(timeout=25)
            return "stopped"
        except subprocess.TimeoutExpired:
            proc.terminate()
            return "sent SIGTERM after 25s"

    def state(self) -> dict:
        alive = self.alive()
        return {"running": alive, "args": self.args[1:] if self.args else [], "started": self.started,
                "elapsed": (time.time() - self.started) if alive else (self.ended - self.started if self.started else 0),
                "code": self.code, "output": list(self.output)}


STATUS_CACHE_SECONDS = 2.0   # one page polls every 3 s; several tabs (or the phone) share one query
JOBS_CACHE_SECONDS = 10.0    # four `launchctl print` subprocesses per status were the dearest part


class Panel:
    def __init__(self, cfg: Config, conn: sqlite3.Connection, command: list[str] | None = None,
                 host: str = "127.0.0.1", port: int = 8766) -> None:
        self.cfg = cfg
        self.conn = conn
        self.command = command or [sys.executable, "-m", "triage.cli"]
        self.host, self.port = host, port
        self.child = Child()
        self.db_lock = threading.Lock()
        self.env = {**os.environ, "TRIAGE_HOME": str(cfg.root), "PYTHONUNBUFFERED": "1"}
        self._status_cache: tuple[float, dict] | None = None
        self._jobs_cache: tuple[float, dict] | None = None

    # -- data -----------------------------------------------------------------

    def status(self) -> dict:
        self._reload_config()
        now = time.monotonic()
        with self.db_lock:
            if self._status_cache is None or now - self._status_cache[0] > STATUS_CACHE_SECONDS:
                st = control.status(self.cfg, self.conn, jobs=self._jobs())
                self._status_cache = (now, st)
            st = dict(self._status_cache[1])
        st["child"] = self.child.state()
        return st

    def _jobs(self) -> dict:
        now = time.monotonic()
        if self._jobs_cache is None or now - self._jobs_cache[0] > JOBS_CACHE_SECONDS:
            self._jobs_cache = (now, {name: control.job_state(label) for name, label in control.JOBS.items()})
        return self._jobs_cache[1]

    def forget(self) -> None:
        """After an action: the next status is fresh."""
        self._status_cache = None
        self._jobs_cache = None

    def host_allowed(self, host_header: str | None) -> bool:
        """The page is served on 127.0.0.1 and reached from the phone through tailscale serve;
        a request whose Host is anything else comes from a page that rebound a DNS name to
        this machine, and gets nothing (the X-Triage header alone does not stop that)."""
        host = (host_header or "").strip().lower()
        if host.startswith("["):
            host = host.split("]", 1)[0] + "]"
        else:
            host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return host in {"127.0.0.1", "localhost", "[::1]", "::1", self.host.lower()} or host.endswith(".ts.net")

    def _reload_config(self) -> None:
        """config.yaml edited by hand (or by `triage enforce` in a terminal) shows on the next
        refresh; a file that does not parse leaves the last good configuration in place."""
        path = self.cfg.root / "config.yaml"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if mtime == getattr(self, "_config_mtime", None):
            return
        try:
            self.cfg = load_config(self.cfg.root)
            self._config_mtime = mtime
            self.forget()
        except Exception as exc:  # noqa: BLE001
            log.warning("panel: config.yaml not reloaded: %s", exc)

    def run_action(self, action: str, params: dict) -> dict:
        if action == "stop":
            msg = self.child.stop() if self.child.alive() else ops.stop_process(self.cfg.root / self.cfg.data_dir)
            return {"ok": True, "message": msg}
        if action in ("schedule_pause", "schedule_resume", "schedule_kick"):
            msg = control.schedule(action.split("_", 1)[1], params.get("job", "run"), self.cfg.root / self.cfg.data_dir)
            return {"ok": not msg.startswith("not kicked"), "message": msg}
        if action in ("enforce_on", "enforce_off"):
            # the file is the truth: the page shows what the next run will read, never a wish
            ok, msg = control.set_enforce(self.cfg.root, action == "enforce_on")
            if ok:
                self._config_mtime = None
                self._reload_config()
            return {"ok": ok, "message": msg}
        if action not in ACTIONS:
            return {"ok": False, "message": f"unknown action {action}"}
        args, background = ACTIONS[action]
        args = list(args) + _extra_args(action, params)
        cmd = self.command + args
        if background:
            if self.child.alive():
                return {"ok": False, "message": "a command is already running; stop it first"}
            if ops.running_process(self.cfg.root / self.cfg.data_dir):
                return {"ok": False, "message": "a run holds the lock (started elsewhere); stop it or wait"}
            self.child.start(cmd, self.cfg.root, self.env)
            return {"ok": True, "message": f"started: triage {' '.join(args)}"}
        try:
            res = subprocess.run(cmd, cwd=str(self.cfg.root), env=self.env, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            return {"ok": False, "message": f"triage {' '.join(args)} took longer than 10 minutes"}
        out = (res.stdout + ("\n" + res.stderr if res.stderr.strip() else "")).strip()
        return {"ok": res.returncode == 0, "message": f"triage {' '.join(args)} → exit {res.returncode}", "output": out[-6000:]}

    # -- server ---------------------------------------------------------------

    def serve(self) -> None:
        panel = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # noqa: ANN001 — quiet
                log.debug("panel: " + fmt, *args)

            def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if not panel.host_allowed(self.headers.get("Host")):
                    self._send(403, b'{"error":"forbidden: unexpected Host"}')
                    return
                url = urlsplit(self.path)
                if url.path in ("/", "/index.html"):
                    page = (PAGE.replace("__ROOT__", html.escape(str(panel.cfg.root)))
                                .replace("__KEEP__", str(panel.cfg.cut.target_unread)))
                    self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                elif url.path == "/api/status":
                    try:
                        body = json.dumps(panel.status(), default=str).encode("utf-8")
                    except Exception as exc:  # noqa: BLE001
                        log.exception("panel: status failed")
                        body = json.dumps({"error": str(exc)}).encode("utf-8")
                    self._send(200, body)
                elif url.path == "/api/log":
                    n = int(parse_qs(url.query).get("n", ["120"])[0])
                    lines = progress.log_tail(ops.log_file(panel.cfg.root / panel.cfg.data_dir), n)
                    self._send(200, json.dumps({"lines": lines}).encode("utf-8"))
                elif url.path.startswith("/dashboard/"):
                    self._static(url.path[len("/dashboard/"):] or "index.html")
                else:
                    self._send(404, b'{"error":"not found"}')

            def _static(self, rel: str) -> None:
                base = (panel.cfg.root / panel.cfg.dashboard_dir).resolve()
                path = (base / rel).resolve()
                if base not in path.parents and path != base or not path.is_file():
                    self._send(404, b'{"error":"not found"}')
                    return
                ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
                self._send(200, path.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("json") else ""))

            def do_POST(self) -> None:  # noqa: N802
                if not panel.host_allowed(self.headers.get("Host")):
                    self._send(403, b'{"ok":false,"message":"forbidden: unexpected Host"}')
                    return
                if self.headers.get("X-Triage") != "panel":  # a page of ours, not a stray form from another site
                    self._send(403, b'{"ok":false,"message":"forbidden"}')
                    return
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    payload = {}
                url = urlsplit(self.path)
                if url.path != "/api/action":
                    self._send(404, b'{"ok":false,"message":"not found"}')
                    return
                try:
                    result = panel.run_action(str(payload.get("action", "")), payload)
                except Exception as exc:  # noqa: BLE001
                    log.exception("panel: action failed")
                    result = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
                panel.forget()
                self._send(200, json.dumps(result, default=str).encode("utf-8"))

        data_dir = self.cfg.root / self.cfg.data_dir
        try:
            server = ThreadingHTTPServer((self.host, self.port), Handler)
        except OSError as exc:
            panel_log(data_dir, f"cannot listen on {self.host}:{self.port}: {exc}")
            raise
        server.daemon_threads = True
        self.url = f"http://{self.host}:{self.port}"
        info = data_dir / "panel.json"
        try:
            info.write_text(json.dumps({"pid": os.getpid(), "host": self.host, "port": self.port, "url": self.url,
                                        "started": time.time()}), encoding="utf-8")
        except OSError:
            pass
        panel_log(data_dir, f"serving on {self.url} (pid {os.getpid()})")
        log.info("panel: serving on %s", self.url)
        try:
            server.serve_forever()
        finally:
            server.server_close()
            panel_log(data_dir, "stopped")
            try:
                info.unlink()
            except OSError:
                pass


def panel_log(data_dir: Path, message: str) -> None:
    """A few lines in data/panel.log so a panel that will not start can be diagnosed from
    the project folder, without hunting for launchd's logs. Kept short."""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / "panel.log"
        lines = path.read_text(encoding="utf-8").splitlines()[-200:] if path.exists() else []
        lines.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def _extra_args(action: str, params: dict) -> list[str]:
    out: list[str] = []
    if action in ("prune", "prune_dry"):
        if params.get("keep"):
            out += ["--keep", str(int(params["keep"]))]
        if params.get("below"):
            out += ["--below", str(float(params["below"]))]
    if action in ("bankruptcy", "bankruptcy_dry"):
        out += ["--older-than", str(int(params.get("days") or 14))]
    if action == "blast":
        if params.get("model"):
            out += ["--model", str(params["model"])]
        if params.get("all_ages"):
            out += ["--all-ages"]
        if params.get("workers"):
            out += ["--workers", str(int(params["workers"]))]
        if params.get("budget"):
            out += ["--budget", str(float(params["budget"]))]
    if action in ("run", "run_shadow", "run_sync") and params.get("limit"):
        out += ["--limit", str(int(params["limit"]))]
    return out


PAGE = r"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>triage control room</title>
<style>
body{font:15px/1.45 -apple-system,system-ui,sans-serif;max-width:1100px;margin:18px auto;padding:0 14px;color:#222;background:#fff}
h1{font-size:20px;margin:0 0 2px}h2{font-size:16px;margin:22px 0 8px;border-bottom:1px solid #ddd;padding-bottom:4px}
.muted{color:#777}.ok{color:#2a7}.bad{color:#c33}.warn{color:#c80}
.state{display:flex;align-items:center;gap:10px;font-size:17px;margin:10px 0}
.dot{width:12px;height:12px;border-radius:6px;background:#bbb;display:inline-block}.dot.on{background:#2a7;animation:pulse 1.2s infinite}
@keyframes pulse{0%{opacity:1}50%{opacity:.25}100%{opacity:1}}
.kpis{display:flex;gap:12px;flex-wrap:wrap;margin:8px 0}.kpi{background:#f6f6f6;border-radius:8px;padding:8px 12px;min-width:120px}
.kpi b{display:block;font-size:19px}.kpi small{color:#777}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}
button{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid #ccc;background:#fafafa;cursor:pointer}
button:hover{background:#eee}button.primary{background:#2a7;color:#fff;border-color:#2a7}button.danger{background:#fff;border-color:#c33;color:#c33}
button:disabled{opacity:.45;cursor:default}
input,select{font:inherit;padding:6px 8px;border:1px solid #ccc;border-radius:6px;width:90px}
pre{white-space:pre-wrap;background:#f6f6f6;padding:10px;border-radius:6px;font-size:12.5px;max-height:260px;overflow:auto;margin:6px 0}
table{border-collapse:collapse;width:100%;font-size:13.5px}th,td{text-align:left;padding:4px 8px;border-bottom:1px solid #eee;vertical-align:top}
th{color:#555;font-weight:600}.num{text-align:right}
iframe{width:100%;height:1500px;border:1px solid #ddd;border-radius:8px}
.msg{margin:6px 0;font-size:14px}.flash{background:#fff3b0;transition:background 1.5s}
</style></head><body>
<h1>triage control room</h1>
<div class='muted'>__ROOT__ · <span id='clock'></span></div>

<div class='state'><span id='dot' class='dot'></span><span id='state'>loading…</span></div>
<div id='msg' class='msg'></div>

<div class='actions'>
  <button class='primary' onclick="act('run')" id='b-run'>Run now</button>
  <button onclick="act('run_shadow')">Run (shadow)</button>
  <button onclick="act('run_sync')">Run (sync)</button>
  <button class='danger' onclick="act('stop')" id='b-stop'>Stop</button>
  <button onclick="act('schedule_pause')">Pause schedule</button>
  <button onclick="act('schedule_resume')">Resume schedule</button>
  <button onclick="confirmAct('enforce_on','Turn the automatic floor on? From the next run, articles under their folder floor are marked read in BazQux (second look and breakers apply).')">Enforce on</button>
  <button onclick="act('enforce_off')">Enforce off</button>
</div>
<div class='actions'>
  keep <input id='keep' type='number' value='__KEEP__'>
  <button onclick="act('prune_dry')">Prune: dry run</button>
  <button class='danger' onclick="confirmAct('prune','Prune to the number kept? Everything else that is scored and unread is marked read in BazQux. Undo reverses it.')">Prune</button>
  <button class='danger' onclick="confirmAct('undo','Undo the last cut? Its articles are marked unread again.')">Undo last cut</button>
  older than <input id='days' type='number' value='14'>
  <button onclick="act('bankruptcy_dry')">Bankruptcy: dry run</button>
  <button class='danger' onclick="confirmAct('bankruptcy','Mark everything older than that many days read? Stars and protected articles are exempt. Undo reverses it.')">Bankruptcy</button>
</div>
<div class='actions'>
  model <select id='model'><option value=''>teacher (config)</option><option value='claude-haiku-5-5'>claude-haiku-5-5 (fastest)</option><option value='claude-sonnet-5-5'>claude-sonnet-5-5</option></select>
  workers <input id='workers' type='number' value='8'>
  <label><input id='allages' type='checkbox' style='width:auto'> all ages</label>
  <button onclick="confirmAct('blast','Judge everything pending now, synchronously, with the chosen model?')">Blast</button>
  <button onclick="act('weekly')">Weekly rewrite</button>
  <button onclick="act('report')">Feed report</button>
  <button onclick="act('doctor')">Doctor</button>
  <button onclick="act('dashboard')">Re-render dashboard</button>
</div>

<div class='kpis' id='kpis'></div>

<h2>Command output <span class='muted' id='cmd'></span></h2>
<pre id='out'>(nothing started from here yet)</pre>

<h2>Schedule (launchd)</h2>
<table><tr><th>Job</th><th>State</th><th>Last exit</th><th>Where</th></tr><tbody id='jobs'></tbody></table>

<h2>Last runs</h2>
<table><tr><th>Started</th><th>Kind</th><th>OK</th><th>Summary</th></tr><tbody id='runs'></tbody></table>

<h2>Log</h2>
<pre id='log'></pre>

<h2>Dashboard</h2>
<iframe src='/dashboard/index.html' id='dash'></iframe>

<script>
var fails = 0, lastChildRunning = null;
function n(x){ return x==null ? '–' : Number(x).toLocaleString('en-US'); }
function esc(s){ return String(s==null?'':s).replace(/[&<>]/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c]; }); }
function mins(s){ s = Number(s||0); return s < 90 ? Math.round(s)+' s' : (s/60).toFixed(1)+' min'; }
function msg(t, bad){ var el = document.getElementById('msg'); el.textContent = t; el.className = 'msg ' + (bad ? 'bad' : 'ok'); }
async function act(action, extra){
  var p = Object.assign({action: action, keep: document.getElementById('keep').value, days: document.getElementById('days').value,
    model: document.getElementById('model').value, workers: document.getElementById('workers').value,
    all_ages: document.getElementById('allages').checked}, extra || {});
  msg('working: ' + action + ' …');
  try {
    var r = await fetch('/api/action', {method:'POST', headers:{'Content-Type':'application/json','X-Triage':'panel'}, body: JSON.stringify(p)});
    var d = await r.json();
    msg(d.message || (d.ok ? 'done' : 'failed'), !d.ok);
    if (d.output !== undefined) { document.getElementById('cmd').textContent = '· ' + action; document.getElementById('out').textContent = d.output || '(no output)'; }
    refresh();
  } catch (e) { msg('request failed: ' + e, true); }
}
function confirmAct(action, text){ if (confirm(text)) act(action); }
function apply(d){
  if (d.error) { msg('status error: ' + d.error, true); return; }
  var r = d.running, c = d.child, dot = document.getElementById('dot'), st = document.getElementById('state');
  var running = !!r || (c && c.running);
  dot.className = 'dot' + (running ? ' on' : '');
  if (r) st.innerHTML = '<b>' + esc(r.kind) + ' running</b> for ' + mins(r.elapsed) + (r.phase ? ' · ' + esc(r.phase) : '') + ' · ' + Math.round(d.rate_per_min||0) + ' judged/min';
  else if (c && c.running) st.innerHTML = '<b>triage ' + esc(c.args.join(' ')) + '</b> running for ' + mins(c.elapsed);
  else st.innerHTML = 'idle · <b>' + esc(d.mode) + ' mode</b>' + (d.next_run_in != null ? ' · next scheduled run in ' + mins(d.next_run_in) : ' · schedule ' + esc(d.jobs.run.state));
  document.getElementById('b-stop').disabled = !running;
  var p = d.pile, s = d.spend, cfg = d.config, cp = d.cuts_pending;
  var k = [['unread', n(p.unread)], ['judged', n(p.judged)], ['in flight', n(p.inflight)], ['pending', n(p.pending)],
           ['await re-judge', n(p.rejudge)], ['target / floor', cfg.target + ' / ' + cfg.floor],
           ['spend today', '$' + Number(s.today).toFixed(2) + ' <small>of $' + cfg.budget + '</small>'], ['7 days', '$' + Number(s.week).toFixed(2)],
           ['per 1,000', s.per_1000 == null ? '–' : '$' + Number(s.per_1000).toFixed(2)], ['cache hits', s.cache_hit == null ? '–' : Math.round(s.cache_hit*100) + '%'],
           ['judged today', n(d.today.judged)], ['cut today', n(d.today.cut)]];
  if (cp.untagged || cp.unmarked) k.push(['cuts to write', n(cp.untagged) + ' tag · ' + n(cp.unmarked) + ' read']);
  document.getElementById('kpis').innerHTML = k.map(function(x){ return '<div class="kpi"><small>' + x[0] + '</small><b>' + x[1] + '</b></div>'; }).join('');
  var jb = ''; ['run','weekly','report','panel'].forEach(function(name){ var j = d.jobs[name];
    var cls = !j.installed ? 'muted' : (j.state === 'running' ? 'ok' : (j.last_exit ? 'bad' : ''));
    jb += '<tr><td>' + name + ' <span class="muted">' + esc(j.label) + '</span></td><td class="' + cls + '">' + esc(j.state) + '</td><td>' + (j.last_exit == null ? '–' : j.last_exit) + '</td><td class="muted">' + esc(j.plist || '') + '</td></tr>'; });
  document.getElementById('jobs').innerHTML = jb;
  var rb = ''; (d.runs || []).slice(0, 12).forEach(function(r){
    var ok = r.ok ? '<span class="ok">yes</span>' : (r.finished_at ? '<span class="bad">no</span>' : '<span class="warn">…</span>');
    rb += '<tr><td>' + esc((r.started_at||'').slice(0,16)) + '</td><td>' + esc(r.kind) + '</td><td>' + ok + '</td><td>' + esc(r.summary || r.error || '') + '</td></tr>'; });
  document.getElementById('runs').innerHTML = rb;
  if (c && (c.running || c.output.length)) { document.getElementById('cmd').textContent = '· triage ' + c.args.join(' ') + (c.running ? ' (running)' : ' (exit ' + c.code + ')');
    var out = document.getElementById('out'); var txt = c.output.join('\n'); if (out.textContent !== txt) { out.textContent = txt || '(no output yet)'; out.scrollTop = out.scrollHeight; } }
  if (lastChildRunning && c && !c.running) { document.getElementById('dash').contentWindow.location.reload(); }
  lastChildRunning = c && c.running;
  var lg = document.getElementById('log'); var lt = (d.log || []).join('\n'); if (lg.textContent !== lt) { lg.textContent = lt || '(no log yet)'; lg.scrollTop = lg.scrollHeight; }
}
async function refresh(){
  try { var r = await fetch('/api/status', {cache:'no-store'}); apply(await r.json()); fails = 0; }
  catch (e) { if (++fails > 3) msg('cannot reach the panel server', true); }
}
setInterval(refresh, 3000); refresh();
setInterval(function(){ document.getElementById('clock').textContent = new Date().toLocaleTimeString(); }, 1000);
</script>
</body></html>"""
