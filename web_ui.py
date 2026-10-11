# SPDX-FileCopyrightText: Copyright (c) 2026 Loran Hughes
# SPDX-License-Identifier: GPL-3.0-only
"""Optional LAN web interface for PHD2 Guard: a live read-only view plus token-protected controls.

Standard library only (works inside the PyInstaller exe). The server never talks to NINA or PHD2 itself: it serves a
`WebState` snapshot that the app keeps up to date and forwards three commands (arm / disarm / manual) to callbacks the
app supplies, so it can only do what the desktop buttons can do.

Security model (see README): clients must be on a private / loopback / link-local address; the live view is open on the
LAN unless `view_needs_token` is set; every control needs the access token (header `X-Token`, constant-time compare,
lock-out after repeated failures); there is no TLS, so it is meant for a trusted home network, not the internet.
"""
from __future__ import annotations

import collections
import hmac
import html
import ipaddress
import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MAX_BODY = 1024
FAIL_LIMIT, FAIL_WINDOW_S = 5, 60.0

# The app icon as SVG. LOGO_SVG is the full drawing used in the page header; FAVICON_SVG is the simplified one the app uses at
# 16-32 px (plain ring, bolder star), so the tab icon stays legible.
_STAR = "M50 14Q54 46 86 50Q54 54 50 86Q46 54 14 50Q46 46 50 14z"
LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" role="img" aria-label="PHD2 Guard logo">'
    '<rect width="100" height="100" rx="20" fill="#0f1b33"/>'
    '<g fill="none" stroke="#2ec4b6" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M50 16A34 34 0 1 1 25.96 25.96" stroke-width="5"/>'
    '<path d="M31 21 29.5 29.5 22.5 22.5z" fill="#2ec4b6" stroke-width="2"/>'
    '<path d="M50 3v7M50 90v7M3 50h7M90 50h7" stroke-width="4"/></g>'
    f'<path transform="translate(19 19) scale(.62)" fill="#ffd166" d="{_STAR}"/></svg>')
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<rect width="100" height="100" rx="20" fill="#0f1b33"/>'
    '<circle cx="50" cy="50" r="36" fill="none" stroke="#2ec4b6" stroke-width="9"/>'
    f'<path transform="translate(12 12) scale(.76)" fill="#ffd166" d="{_STAR}"/></svg>')


class WebState:
    """Thread-safe snapshot of what the page shows (written by the app, read by request threads)."""
    LOG_MAX = 500

    def __init__(self):
        self._lock = threading.Lock()
        self._log: collections.deque[dict] = collections.deque(maxlen=self.LOG_MAX)
        self._seq = 0
        self._data: dict = {
            "nina_connected": None, "phd2_connected": None, "phd2_detail": "", "armed": False,
            "phase": {"kind": "idle", "text": "Idle - not armed"}, "status": {}, "attempts": 0,
            "reference": "", "alert": "", "settings": {},
        }

    def update(self, **kw) -> None:
        with self._lock:
            self._data.update(kw)

    def add_log(self, ts: str, level: str, msg: str) -> None:
        with self._lock:
            self._seq += 1
            self._log.append({"seq": self._seq, "ts": ts, "level": level, "msg": msg})

    def snapshot(self) -> dict:
        with self._lock:
            d = dict(self._data)
            d["log_seq"] = self._seq
        d["time"] = datetime.now().strftime("%H:%M:%S")
        return d

    def log_after(self, after: int) -> list[dict]:
        with self._lock:
            return [e for e in self._log if e["seq"] > after]

    @property
    def phase_kind(self) -> str:
        with self._lock:
            return self._data["phase"]["kind"]


class WebServer:
    """ThreadingHTTPServer wrapper. `commands` maps 'arm' / 'disarm' / 'manual' to zero-argument callables."""

    def __init__(self, state: WebState, commands: dict, host: str, port: int, token: str,
                 view_needs_token: bool = False, title: str = "PHD2 Guard", tagline: str = "", version: str = "",
                 icons: dict | None = None):
        self.state, self.commands, self.host, self.port = state, commands, host, port
        self.token, self.view_needs_token, self.title = token, view_needs_token, title
        self.tagline, self.version = tagline, version
        self.icons = {k: v for k, v in (icons or {}).items() if v}         # "ico" (favicon.ico), "apple" (180 px PNG)
        self._fails: dict[str, list[float]] = {}
        self._fail_lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ----- lifecycle ------------------------------------------------------------------
    def start(self) -> None:
        outer = self

        class Handler(_Handler):
            ctx = outer

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)     # raises OSError if the port is taken
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="web-ui", daemon=True,
                                        kwargs={"poll_interval": 0.3})
        self._thread.start()

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()

    # ----- helpers used by the handler ----------------------------------------------
    def page(self) -> bytes:
        links = ""
        if "ico" in self.icons:
            links += '<link rel="icon" href="/favicon.ico" sizes="16x16 32x32 48x48">'
        if "apple" in self.icons:
            links += '<link rel="apple-touch-icon" href="/apple-touch-icon.png">'
        text = (PAGE.replace("__ICON_LINKS__", links)
                .replace("__LOGO__", LOGO_SVG)
                .replace("__TAGLINE__", html.escape(self.tagline))
                .replace("__VERSION__", html.escape(f"Version {self.version}" if self.version else ""))
                .replace("__TITLE__", html.escape(self.title)))
        return text.encode()

    def asset(self, path: str) -> tuple[bytes, str] | None:
        """Static icon files: (body, content type), or None when this server has no such file."""
        if path == "/favicon.svg":
            return FAVICON_SVG.encode(), "image/svg+xml"
        if path == "/favicon.ico" and "ico" in self.icons:
            return self.icons["ico"], "image/x-icon"
        if path == "/apple-touch-icon.png" and "apple" in self.icons:
            return self.icons["apple"], "image/png"
        return None

    def token_ok(self, supplied: str) -> bool:
        return bool(self.token) and hmac.compare_digest(supplied.encode(), self.token.encode())

    def locked_out(self, ip: str) -> bool:
        now = time.monotonic()
        with self._fail_lock:
            recent = [t for t in self._fails.get(ip, []) if now - t < FAIL_WINDOW_S]
            self._fails[ip] = recent
            return len(recent) >= FAIL_LIMIT

    def note_failure(self, ip: str) -> None:
        with self._fail_lock:
            self._fails.setdefault(ip, []).append(time.monotonic())


def client_allowed(addr: str) -> bool:
    """Private LAN, loopback or link-local only - never a routable public address."""
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


class _Handler(BaseHTTPRequestHandler):
    ctx: WebServer
    server_version = "PHD2Guard"
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):                       # keep the console / log quiet
        pass

    # ----- plumbing -------------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, cache: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; "
                         "img-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json; charset=utf-8")

    def _gate(self) -> tuple[bool, bool] | None:
        """(token valid, ok to continue). Sends the error response itself and returns None when blocked."""
        ip = self.client_address[0]
        if not client_allowed(ip):
            self._json(403, {"error": "local network only"})
            return None
        supplied = self.headers.get("X-Token", "")
        if supplied:
            if self.ctx.locked_out(ip):
                self._json(429, {"error": "too many failed attempts - wait a minute"})
                return None
            if self.ctx.token_ok(supplied):
                return True, True
            self.ctx.note_failure(ip)
            return False, True
        return False, True

    # ----- routes ---------------------------------------------------------------------
    def do_GET(self) -> None:                           # noqa: N802 - http.server API
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            if not client_allowed(self.client_address[0]):
                self._json(403, {"error": "local network only"})
                return
            self._send(200, self.ctx.page(), "text/html; charset=utf-8")
            return
        if url.path in ("/favicon.svg", "/favicon.ico", "/apple-touch-icon.png"):
            if not client_allowed(self.client_address[0]):
                self._json(403, {"error": "local network only"})
                return
            found = self.ctx.asset(url.path)
            if found is None:                            # e.g. /favicon.ico when no ICO was supplied: browsers just skip it
                self._send(204, b"", "image/x-icon")
            else:
                self._send(200, found[0], found[1], cache=True)
            return
        if url.path not in ("/api/state", "/api/log"):
            self._json(404, {"error": "not found"})
            return
        gate = self._gate()
        if gate is None:
            return
        valid, _ = gate
        if self.ctx.view_needs_token and not valid:
            self._json(401, {"error": "token required"})
            return
        if url.path == "/api/state":
            snap = self.ctx.state.snapshot()
            snap["can_control"] = valid
            snap["view_needs_token"] = self.ctx.view_needs_token
            self._json(200, snap)
        else:
            try:
                after = int(parse_qs(url.query).get("after", ["0"])[0])
            except ValueError:
                after = 0
            self._json(200, {"entries": self.ctx.state.log_after(after)})

    def do_POST(self) -> None:                          # noqa: N802
        name = urlparse(self.path).path.rsplit("/", 1)[-1]
        if not urlparse(self.path).path.startswith("/api/") or name not in ("arm", "disarm", "manual"):
            self._json(404, {"error": "not found"})
            return
        gate = self._gate()
        if gate is None:
            return
        if not gate[0]:
            self._json(401, {"error": "token required"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            self._json(413, {"error": "body too large"})
            return
        body = {}
        if length:
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json(400, {"error": "invalid JSON"})
                return
        busy = self.ctx.state.phase_kind == "recovering"
        if name == "manual" and body.get("confirm") is not True:
            self._json(400, {"error": "manual recovery needs {\"confirm\": true}"})
            return
        if name in ("manual", "arm") and busy:
            self._json(409, {"error": "a recovery is in progress"})
            return
        self.ctx.commands[name]()
        self._json(200, {"ok": True, "command": name})


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">__ICON_LINKS__
<meta name="theme-color" content="#0f1b33">
<style>
:root{--bg:#0f1b33;--card:#16264a;--line:#27407a;--text:#e8edf8;--mute:#93a3c4;--teal:#2ec4b6;--amber:#ffd166;
--red:#ef5350;--green:#4caf50;--blue:#4a90e2;--violet:#9b6ee0;--orange:#e0892b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:14px 16px 40px}
.hero{display:flex;align-items:center;gap:18px;padding:16px 20px;margin-bottom:12px;border-radius:14px;border:1px solid var(--line);
background:linear-gradient(135deg,#0f1b33,#1b3a63)}
.logo svg{display:block;width:72px;height:72px}
.name{font-size:26px;font-weight:700;letter-spacing:.5px;line-height:1.15}
.rule{width:48px;height:3px;border-radius:2px;background:var(--amber);margin:7px 0}
.tag{color:var(--teal);font-size:14.5px}.ver{color:var(--mute);font-size:12.5px;margin-top:2px}
.status{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}
@media(max-width:520px){.hero{gap:14px;padding:12px 14px}.logo svg{width:56px;height:56px}.name{font-size:21px}.tag{font-size:13px}}
.pill{padding:3px 10px;border-radius:999px;border:1px solid var(--line);font-size:12.5px;color:var(--mute)}
.pill.ok{color:var(--green);border-color:var(--green)}.pill.bad{color:var(--red);border-color:var(--red)}
.banner{border-radius:12px;padding:12px 14px;margin-bottom:12px;font-weight:600;background:var(--card);border:1px solid var(--line)}
.k-monitoring{border-color:var(--green)}.k-pending{border-color:var(--amber);color:var(--amber)}
.k-recovering{border-color:var(--blue);color:var(--blue)}.k-cooldown{border-color:var(--violet);color:var(--violet)}
.k-waiting{border-color:var(--amber);color:var(--amber)}.k-retry{border-color:var(--orange);color:var(--orange)}
.k-halted{border-color:var(--red);color:var(--red);background:#3a1620}.k-idle{color:var(--mute)}
.alert{background:#3a1620;border:1px solid var(--red);color:#ffb4b0;border-radius:12px;padding:12px 14px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-bottom:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.card h2{font-size:12.5px;font-weight:500;color:var(--mute);margin:0 0 6px;text-transform:uppercase;letter-spacing:.04em}
.big{font-size:34px;font-weight:600;line-height:1.1}.big.hot{color:var(--red)}
.row{display:flex;justify-content:space-between;gap:10px;padding:2px 0}.row span:first-child{color:var(--mute)}
.btns{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px}
button{background:var(--teal);color:#06231f;border:0;border-radius:10px;padding:11px 16px;font:600 14.5px system-ui;cursor:pointer}
button.alt{background:transparent;color:var(--text);border:1px solid var(--line)}
button.warn{background:var(--amber);color:#3b2a00}button:disabled{opacity:.35;cursor:not-allowed}
#log{height:300px;overflow:auto;background:#0a1226;border:1px solid var(--line);border-radius:12px;padding:8px 10px;
font:12.5px/1.5 Consolas,ui-monospace,monospace;white-space:pre-wrap;word-break:break-word}
.l-WARN{color:var(--amber)}.l-ERROR{color:var(--red)}.l-OK{color:var(--green)}.l-API{color:#6fb3d9}.l-INFO{color:#d4d4d4}
.ts{color:#6b7a99}small{color:var(--mute)}
</style></head><body><div class="wrap">
<header class="hero"><div class="logo">__LOGO__</div>
<div><div class="name">__TITLE__</div><div class="rule"></div><div class="tag">__TAGLINE__</div><div class="ver">__VERSION__</div></div></header>
<div class="status"><span class="pill" id="nina">NINA</span><span class="pill" id="phd2">PHD2 server</span>
<span class="pill" id="dither" hidden style="color:var(--amber);border-color:var(--amber)">dithering</span><span class="pill" id="armed">not armed</span><button class="alt" id="unlock">Unlock controls</button></div>
<div class="alert" id="alert" hidden></div>
<div class="banner k-idle" id="phase">Connecting...</div>
<div class="btns"><button id="arm">Start Guarding</button><button class="alt" id="disarm">Stop Guarding</button>
<button class="warn" id="manual">Manual recovery</button><small id="ctlnote"></small></div>
<div class="grid">
<div class="card"><h2>Total RMS</h2><div class="big" id="total">-</div><small id="rmsnote"></small></div>
<div class="card"><h2>Guiding</h2>
<div class="row"><span>PHD2 state</span><b id="gstate">-</b></div><div class="row"><span>RA RMS</span><b id="ra">-</b></div>
<div class="row"><span>Dec RMS</span><b id="dec">-</b></div><div class="row"><span>Recent steps</span><b id="recent">-</b></div>
<div class="row"><span>Frames lost</span><b id="lost">-</b></div><div class="row"><span>Settling</span><b id="settle">-</b></div><div class="row"><span>Dithering</span><b id="dith">-</b></div></div>
<div class="card"><h2>NINA</h2>
<div class="row"><span>Target</span><b id="target">-</b></div><div class="row"><span>Activity</span><b id="activity">-</b></div>
<div class="row"><span>Frame</span><b id="frame">-</b></div><div class="row"><span>Recoveries / hour</span><b id="attempts">-</b></div>
<div class="row"><span>Recenter target</span><b id="ref" style="text-align:right">-</b></div></div>
<div class="card"><h2>Rule</h2><div class="row"><span>Threshold</span><b id="thr">-</b></div>
<div class="row"><span>Bad for</span><b id="sustain">-</b></div><div class="row"><span>Star-loss rate</span><b id="lostrule">-</b></div>
<div class="row"><span>Wait for star</span><b id="waitrule">-</b></div><div class="row"><span>Cooldown</span><b id="cool">-</b></div></div>
</div>
<div class="card"><h2>Log</h2><div id="log"></div></div>
<p><small id="foot"></small></p></div>
<script>
const $=id=>document.getElementById(id);
let token=localStorage.getItem('phd2guard_token')||'',after=0,can=false,lastOk=0;
const f=(v,u)=>v==null?'-':(typeof v==='number'?v.toFixed(2):v)+(u||'');
function hdr(){return token?{'X-Token':token}:{}}
async function post(cmd,body){
  const r=await fetch('/api/'+cmd,{method:'POST',headers:Object.assign({'Content-Type':'application/json'},hdr()),body:JSON.stringify(body||{})});
  const j=await r.json().catch(()=>({}));
  if(r.status===401){alert('The access token is missing or wrong.');return}
  if(!r.ok){alert(j.error||('Request failed ('+r.status+')'));return}
  tick();
}
$('unlock').onclick=()=>{const t=prompt('Access token (shown in the app\'s Web interface settings)',token);
  if(t!==null){token=t.trim();localStorage.setItem('phd2guard_token',token);tick()}};
$('arm').onclick=()=>post('arm');$('disarm').onclick=()=>post('disarm');
$('manual').onclick=()=>{if(confirm('Stop the exposure and guiding, re-center the target, restart guiding and resume imaging now?'))post('manual',{confirm:true})};
function render(s){
  const st=s.status||{},set=s.settings||{},p=s.phase||{kind:'idle',text:''};
  const pill=(el,ok,txt)=>{el.textContent=txt;el.className='pill '+(ok===true?'ok':ok===false?'bad':'')};
  pill($('nina'),s.nina_connected,'NINA '+(s.nina_connected?'connected':s.nina_connected===false?'unreachable':'...'));
  pill($('phd2'),s.phd2_connected,s.phd2_connected==null?'PHD2 server n/a':'PHD2 server '+(s.phd2_connected?'connected':'unreachable'));
  pill($('armed'),s.armed?true:null,s.armed?'armed':'not armed');
  $('phase').textContent=p.text;$('phase').className='banner k-'+p.kind;
  const a=$('alert');a.hidden=!s.alert;a.textContent=s.alert?('Safety stop: '+s.alert):'';
  const u=st.unit==='pixel'?' px':'"';
  $('total').textContent=f(st.total,st.total==null?'':u);
  $('total').className='big'+(st.total!=null&&set.threshold&&st.total>set.threshold?' hot':'');
  $('rmsnote').textContent=st.samples!=null?('window '+st.samples+'/'+st.window+' steps'):'RMS from NINA';
  $('gstate').textContent=st.state||'-';$('ra').textContent=f(st.ra,st.ra==null?'':u);$('dec').textContent=f(st.dec,st.dec==null?'':u);
  $('recent').textContent=f(st.recent,st.recent==null?'':u);
  const l=st.lost;$('lost').textContent=l?(l.count+'/'+l.frames+(l.pct!=null?'  ('+Math.round(l.pct)+'%)':'')):'-';
  $('settle').textContent=st.settle||'-';const dt=!!st.dithering;$('dith').textContent=dt?'Yes':'-';$('dither').hidden=!dt;$('target').textContent=st.target||'-';$('activity').textContent=st.activity||'-';
  $('frame').textContent=(st.exposure_s!=null?st.exposure_s+' s':'-')+(st.exposing===true?' (exposing)':st.exposing===false?' (idle)':'');
  $('attempts').textContent=s.attempts+(set.max_attempts_per_hour?' / '+set.max_attempts_per_hour:'');$('ref').textContent=s.reference||'-';
  $('thr').textContent=set.threshold!=null?set.threshold+(st.unit==='pixel'?' px':'"')+(set.recent_steps?' (+ last '+set.recent_steps+' steps)':''):'-';
  $('sustain').textContent=set.sustain_s!=null?set.sustain_s+' s':'-';
  $('lostrule').textContent=set.lost_pct_enabled?(set.lost_pct+'% of '+set.lost_window+' frames'):'off';
  $('waitrule').textContent=set.wait_for_star_s?(Math.round(set.wait_for_star_s/60)+' min'):'off';
  $('cool').textContent=set.cooldown_s!=null?set.cooldown_s+' s':'-';
  can=!!s.can_control;const busy=p.kind==='recovering';
  $('arm').disabled=!can||s.armed||busy;$('disarm').disabled=!can||(!s.armed&&!busy);$('manual').disabled=!can||busy||s.nina_connected!==true;
  $('ctlnote').textContent=can?'':'view only - use "Unlock controls" and enter the access token';
  $('unlock').hidden=can;
}
async function tick(){
  try{
    const r=await fetch('/api/state',{headers:hdr(),cache:'no-store'});
    if(r.status===401){$('phase').textContent='Access token required to view';$('phase').className='banner k-halted';$('unlock').hidden=false;return}
    if(r.status===403){$('phase').textContent='Only available from the local network';return}
    if(r.status===429){$('phase').textContent='Too many wrong tokens - wait a minute';return}
    const s=await r.json();render(s);lastOk=Date.now();
    const lr=await fetch('/api/log?after='+after,{headers:hdr(),cache:'no-store'});
    if(lr.ok){const j=await lr.json();const box=$('log');const stick=box.scrollTop+box.clientHeight>=box.scrollHeight-30;
      for(const e of j.entries){after=Math.max(after,e.seq);const d=document.createElement('div');d.className='l-'+e.level;
        const t=document.createElement('span');t.className='ts';t.textContent=e.ts+' ';d.appendChild(t);
        d.appendChild(document.createTextNode('['+e.level+'] '+e.msg));box.appendChild(d)}
      while(box.childNodes.length>600)box.removeChild(box.firstChild);if(stick)box.scrollTop=box.scrollHeight}
    $('foot').textContent='Updated '+s.time;
  }catch(e){$('foot').textContent='No connection to the app - retrying...'}
}
setInterval(tick,1000);tick();
</script></body></html>"""
