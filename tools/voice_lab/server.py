"""The Voice Lab page: a tiny local web server on 127.0.0.1 only. The page loads nothing from the internet."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

HOST = "127.0.0.1"

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Voice Lab</title>
<style>
:root{--bg:#f6f5f2;--fg:#1d1d1b;--mut:#6b6a66;--card:#fff;--line:#e2e0da;--acc:#2f6f5e;--warn:#b5532a;--bar:#d8d5cc}
@media (prefers-color-scheme:dark){:root{--bg:#181816;--fg:#ecebe6;--mut:#9c9a93;--card:#22221f;--line:#34332f;--acc:#6fbfa6;--warn:#e08a5f;--bar:#3a3934}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,Segoe UI,sans-serif}
main{max-width:1000px;margin:0 auto;padding:16px}h1{font-size:22px;margin:4px 0 12px}h2{font-size:16px;margin:0 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:12px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}button,select{font:inherit;padding:6px 12px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
select{max-width:100%;min-width:0}main>*{min-width:0}
button.primary{background:var(--acc);color:#fff;border-color:var(--acc)}button.danger{color:var(--warn)}
.meter{position:relative;height:14px;background:var(--bar);border-radius:7px;overflow:hidden;flex:1;min-width:100px}
.meter i{position:absolute;left:0;top:0;bottom:0;background:var(--acc)}.meter b{position:absolute;top:0;bottom:0;width:2px;background:var(--warn)}
.mut{color:var(--mut)}.big{font-size:18px}.line{font-size:20px;font-weight:600;margin:8px 0}
table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:4px 6px;border-bottom:1px solid var(--line);text-align:right}td:first-child,th:first-child{text-align:left}
.zb{display:inline-block;width:120px;height:8px;background:var(--bar);position:relative;border-radius:4px;vertical-align:middle}.zb i{position:absolute;top:0;bottom:0;background:var(--acc)}.zb s{position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:var(--mut)}
.bad{color:var(--warn)}.ok{color:var(--acc)}.wrap{overflow-x:auto}
</style></head><body><main>
<h1>Voice Lab</h1>
<div class="card"><h2>Microphone</h2>
 <div class="row"><select id="dev"></select><button onclick="pick()">Use this mic</button><button onclick="act('devices')">Refresh list</button></div>
 <p id="devinfo" class="mut"></p>
 <div class="row"><span>Level</span><div class="meter"><i id="lvl"></i><b id="pk"></b></div><span id="lvltxt" class="mut"></span></div>
 <p class="row"><span>Clipped samples: <b id="clip">0</b> <button onclick="act('reset_clip')">Reset</button></span>
 <span>Noise floor: <b id="noise">?</b></span><span>SNR: <b id="snr">?</b></span>
 <button onclick="act('noise')">Measure noise (10 s)</button></p>
</div>
<div class="card"><h2>Protocol</h2><div id="proto"></div>
 <div class="row"><button class="primary" onclick="act('start')">Start protocol</button><button onclick="act('redo')">Redo last step</button>
 <button onclick="act('skip')">Skip step</button><button onclick="act('stop')">Stop (free talk)</button></div></div>
<div class="card"><h2>Last utterance <span id="src" class="mut"></span></h2><p id="tr" class="mut"></p><div class="wrap"><table id="last"></table></div></div>
<div class="card"><h2>Validation (kept = a deliberate change moved it at least <span id="kf"></span>x its natural spread)</h2><div class="wrap"><table id="val"></table></div><div id="day"></div></div>
<div class="card"><h2>Files</h2><p id="files" class="mut"></p><button class="danger" onclick="act('delete')">Delete recordings</button></div>
<p id="msg" class="big"></p>
</main><script>
let S=null;
async function act(name,body){await fetch('/api/'+name,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});tick()}
function pick(){const o=document.getElementById('dev').selectedOptions[0];if(o)act('select',{name:o.dataset.name,hostapi:o.dataset.api})}
const pct=db=>Math.max(0,Math.min(100,(db+80)/80*100));
function esc(s){return String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function fmt(v){return v==null?'–':v}
function render(s){
 const sel=document.getElementById('dev');
 const sig=JSON.stringify(s.devices);
 if(sel.dataset.sig!==sig){sel.innerHTML=s.devices.map(d=>`<option data-name="${esc(d.name)}" data-api="${esc(d.hostapi)}">${esc(d.name)} — ${esc(d.hostapi)}, ${d.sample_rate} Hz${d.is_default?' (Windows default)':''}</option>`).join('');sel.dataset.sig=sig}
 document.getElementById('devinfo').textContent=s.device?`Using ${s.device.name} (${s.device.hostapi}, ${s.device.sample_rate} Hz): ${s.device.status}. Pitch: ${s.pitch_backend}. Transcript: ${s.stt||'off'}.`:'No mic selected.';
 document.getElementById('lvl').style.width=pct(s.level_db)+'%';document.getElementById('pk').style.left=pct(s.peak_db)+'%';
 document.getElementById('lvltxt').textContent=`${s.level_db} dBFS, peak ${s.peak_db}${s.speaking?' · speech':''}`;
 const c=document.getElementById('clip');c.textContent=s.clipped;c.className=s.clipped?'bad':'';
 document.getElementById('noise').textContent=s.noise_left_s!=null?`measuring… ${s.noise_left_s}s`:(s.noise_floor_db==null?'?':s.noise_floor_db+' dBFS');
 document.getElementById('snr').textContent=s.snr_db==null?'?':s.snr_db+' dB';
 const p=s.protocol,pe=document.getElementById('proto');
 if(!p)pe.innerHTML='<p class="mut">Not running. Lines you say now are measured against your baseline but not saved.</p>';
 else if(p.done)pe.innerHTML='<p>Finished.</p>';
 else pe.innerHTML=`<p class="mut">Step ${p.index+1} of ${p.total}${p.pending?' · analysing…':''}</p><p>${esc(p.step.prompt)}</p>${p.step.line?`<p class="line">“${esc(p.step.line)}”</p>`:''}`;
 const L=s.last;document.getElementById('src').textContent=L?`(${L.source}, ${L.analysis_ms} ms, pitch ${L.pitch_backend}${L.timed_out?', timed out':''})`:'';
 document.getElementById('tr').textContent=L&&L.transcript?'“'+L.transcript+'”':'';
 document.getElementById('last').innerHTML=L?'<tr><th>Measurement</th><th>Value</th><th>Baseline</th><th>vs baseline</th><th>z</th><th>Reliable</th></tr>'+L.measures.map(m=>{
  const z=m.z,w=z==null?0:Math.min(50,Math.abs(z)*10),l=z==null?50:(z<0?50-w:50);
  return `<tr><td>${esc(m.label)} <span class="mut">${esc(m.unit)}</span></td><td>${fmt(m.value)}</td><td>${fmt(m.base)}</td><td><span class="zb"><s></s><i style="left:${l}%;width:${w}%"></i></span></td><td>${fmt(z)}</td><td class="${m.reliable?'ok':'bad'}">${m.reliable?'yes':'no'}${m.note?' <span class="mut">'+esc(m.note)+'</span>':''}</td></tr>`}).join(''):'';
 document.getElementById('kf').textContent=s.keep_factor;
 const V=s.validation,vt=document.getElementById('val');
 if(V){const labs=Object.keys(V[0]).filter(k=>k.startsWith('sep_'));vt.innerHTML='<tr><th>Measurement</th><th>Spread</th>'+labs.map(k=>`<th>${k.slice(4)}</th>`).join('')+'<th>Verdict</th></tr>'+V.map(r=>`<tr><td>${esc(r.measurement)}</td><td>${fmt(r.natural_spread)}</td>${labs.map(k=>`<td>${fmt(r[k])}</td>`).join('')}<td class="${r.verdict==='kept'?'ok':(r.verdict==='dropped'?'bad':'mut')}" title="${esc(r.because)}">${esc(r.verdict)}</td></tr>`).join('')}else vt.innerHTML='<tr><td class="mut">Appears when the protocol finishes.</td></tr>';
 const D=s.day_compare;document.getElementById('day').innerHTML=D?'<h2 style="margin-top:12px">Compared with the previous baseline</h2><table><tr><th>Measurement</th><th>Before</th><th>Now</th><th>Spread</th><th>Held</th></tr>'+D.map(d=>`<tr><td>${esc(d.measurement)}</td><td>${fmt(d.first)}</td><td>${fmt(d.second)}</td><td>${fmt(d.spread)}</td><td class="${d.held?'ok':'bad'}">${d.held?'yes':'no'}</td></tr>`).join('')+'</table>':'';
 document.getElementById('files').textContent=`Session folder: ${s.session_dir}. Recordings on disk: ${s.recordings}.`;
 document.getElementById('msg').textContent=s.message||'';
}
async function tick(){try{const r=await fetch('/api/state');S=await r.json();render(S)}catch(e){}}
setInterval(tick,400);tick();act('devices');
</script></body></html>
"""


def make_handler(lab: Any) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:   # keep the console quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._send(200, json.dumps(lab.state()).encode("utf-8"), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            if not self.path.startswith("/api/"):
                self._send(404, b"not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except ValueError:
                body = {}
            lab.action(self.path[len("/api/"):], body if isinstance(body, dict) else {})
            self._send(200, json.dumps(lab.state()).encode("utf-8"), "application/json")

    return Handler


def serve(lab: Any, port: int) -> ThreadingHTTPServer:
    """Bind to 127.0.0.1 only, so nothing outside this PC can reach the page."""
    server = ThreadingHTTPServer((HOST, port), make_handler(lab))
    server.daemon_threads = True
    return server
