"""Microphone calibration (mic capture design, step 2): a guided five-step recording on the mic Voice Lab is using,
measured with livingworld.audio.metrics and saved as a per-mic profile. Page: http://127.0.0.1:7861/calibrate

Recordings stay on this PC under Documents\\My Games\\Mantella\\data\\audio\\calibration\\<mic>\\ and are removed by
"Delete calibration recordings". Only numbers go into mic_profiles.json."""
from __future__ import annotations

import logging
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from livingworld.audio import metrics as M
from livingworld.audio.profile import MicProfile, ProfileStore, profile_key
from tools.voice_lab.storage import mic_slug

log = logging.getLogger("voice_lab")

MANTELLA_VAD_THRESHOLD = 0.4      # Mantella's default Audio Threshold

RAINBOW = ("When the sunlight strikes raindrops in the air, they act as a prism and form a rainbow. The rainbow is a "
           "division of white light into many beautiful colors. These take the shape of a long round arch, with its "
           "path high above, and its two ends apparently beyond the horizon. There is, according to legend, a boiling "
           "pot of gold at one end. People look, but no one ever finds it. When a man looks for something beyond his "
           "reach, his friends say he is looking for the pot of gold at the end of the rainbow.")


@dataclass(frozen=True)
class CalStep:
    id: str
    seconds: float
    title: str
    prompt: str
    text: str = ""


STEPS = [
    CalStep("silence", 10, "Room silence", "Stay completely quiet, with the game running as you normally play."),
    CalStep("loud", 10, "Loud speech", "Speak as loudly as you would for a shouted threat. If clipping shows, lower "
            "this mic's level in Windows Sound settings and record this step again.",
            "Stand aside! I said stand aside, or you'll regret it!"),
    CalStep("normal", 45, "Normal speech", "Read the passage at your usual conversational level. Repeat it if you "
            "finish early.", RAINBOW),
    CalStep("quiet", 10, "Quiet speech", "Read these lines softly, as if you didn't want to be overheard.",
            "Keep your voice down. Meet me behind the inn after dark, and come alone."),
    CalStep("echo", 20, "Echo check (optional)", "Stay silent while an NPC talks in the game (or play any recorded "
            "speech through your headset). Checks whether NPC voices leak into the mic."),
]
STEP_BY_ID = {s.id: s for s in STEPS}


@dataclass
class Take:
    audio: np.ndarray
    probs: np.ndarray
    sample_rate: int
    block_samples: int
    result: dict[str, Any]
    path: str | None = None


class Calibrator:
    """Taps the blocks Voice Lab's capture already produces; holds one take per step for the current mic."""

    def __init__(self, lab: Any, root: Path) -> None:
        self.lab = lab
        self.root = Path(root)
        self.store = ProfileStore(self.root)
        self.lock = threading.Lock()
        self.takes: dict[str, Take] = {}
        self._mic: str | None = None
        self._rec_step: CalStep | None = None
        self._rec_blocks: list[np.ndarray] = []
        self._rec_probs: list[float] = []
        self._rec_target = 0
        self._finishers: list[threading.Thread] = []
        self.profile: MicProfile | None = None
        self.message = ""
        lab.block_listeners.append(self.on_block)

    # recording ------------------------------------------------------------------------------------------
    def _device(self):
        cap = self.lab.capture
        return None if cap is None else cap.device

    def _check_mic(self) -> None:
        dev = self._device()
        key = None if dev is None else profile_key(dev.name, dev.hostapi)
        if key != self._mic:                      # a different mic: earlier takes don't describe it
            self.takes.clear()
            self.profile = None
            self._mic = key

    def record(self, step_id: str) -> None:
        step = STEP_BY_ID.get(step_id)
        cap = self.lab.capture
        if step is None:
            self.message = f"Unknown step {step_id}"
            return
        if cap is None:
            self.message = "Pick a microphone on the Voice Lab page first."
            return
        with self.lock:
            self._check_mic()
            self._rec_step = step
            self._rec_blocks, self._rec_probs = [], []
            self._rec_target = int(np.ceil(step.seconds * cap.sample_rate / cap.block_samples))
            self.message = f"Recording: {step.title} ({step.seconds:g} s)."

    def cancel(self) -> None:
        with self.lock:
            self._rec_step = None
            self.message = "Recording cancelled."

    def on_block(self, block: np.ndarray, prob: float) -> None:
        with self.lock:
            if self._rec_step is None:
                return
            self._rec_blocks.append(np.asarray(block, dtype=np.float32).copy())
            self._rec_probs.append(float(prob))
            if len(self._rec_blocks) < self._rec_target:
                return
            step, blocks, probs = self._rec_step, self._rec_blocks, self._rec_probs
            self._rec_step, self._rec_blocks, self._rec_probs = None, [], []
        t = threading.Thread(target=self._finish, args=(step, blocks, probs), daemon=True, name="cal_finish")
        self._finishers.append(t)
        t.start()

    def wait(self, timeout: float = 10.0) -> None:
        for t in list(self._finishers):
            t.join(timeout)
        self._finishers.clear()

    def _finish(self, step: CalStep, blocks: list[np.ndarray], probs: list[float]) -> None:
        cap = self.lab.capture
        audio = np.concatenate(blocks)
        take = Take(audio, np.asarray(probs, dtype=np.float32), cap.sample_rate, len(blocks[0]), {})
        try:
            take.path = self._save_wav(step.id, audio, cap.sample_rate)
        except Exception as e:                     # measurements still work without the file
            log.warning("could not save calibration take: %s", e)
        with self.lock:
            self.takes[step.id] = take
            take.result = self.measure(step.id, take)
            self.message = f"{step.title} recorded."

    # measuring ------------------------------------------------------------------------------------------
    def _room_floor(self) -> float | None:
        t = self.takes.get("silence")
        return None if t is None else t.result.get("noise_floor_db")

    def measure(self, step_id: str, t: Take) -> dict[str, Any]:
        sr, bs = t.sample_rate, t.block_samples
        clip = M.clipping(t.audio)
        out: dict[str, Any] = {"seconds": round(len(t.audio) / sr, 1), "rms_db": round(M.rms_db(t.audio), 1),
                               "peak_db": round(M.peak_db(t.audio), 1), "clipped": clip.full_scale,
                               "flat_tops": clip.flat_top_runs,
                               "dc_offset": round(float(np.mean(t.audio)), 5)}
        heard = float(np.sum(t.probs > MANTELLA_VAD_THRESHOLD) * bs / sr)
        mask = M.speech_mask(t.probs, bs, len(t.audio))
        if step_id == "silence":
            out["noise_floor_db"] = round(M.noise_floor_db(t.audio, sr), 1)
            out["speech_heard_s"] = round(heard, 1)
            hum = M.detect_hum(t.audio, sr)
            out["hum_hz"] = hum.base_hz
            out["bands_db"] = M.third_octave_db(t.audio, sr)
        else:
            speech = M.speech_level_db(t.audio, mask)
            out["speech_db"] = None if speech is None else round(speech, 1)
            out["speech_heard_s"] = round(heard, 1)
            floor = self._room_floor()
            if speech is not None and floor is not None:
                out["snr_db"] = round(speech - floor, 1)
        if step_id == "normal":
            up = M.upstream_processing(t.audio, t.probs, bs, self._room_floor(), block_s=bs / sr)
            out["clean"], out["clean_judged"], out["clean_reasons"] = up.clean, up.judged, up.reasons
            out["gap_floor_db"] = up.gap_floor_db
            out["bands_db"] = M.third_octave_db(t.audio, sr, mask=mask)
        if step_id == "quiet":
            voiced = np.sum(t.probs > 0.1)
            out["heard_share"] = round(float(np.sum(t.probs > MANTELLA_VAD_THRESHOLD) / voiced), 2) if voiced else 0.0
        return out

    def build_profile(self) -> MicProfile | None:
        dev = self._device()
        if dev is None or "silence" not in self.takes or "normal" not in self.takes:
            self.message = "Record at least Room silence and Normal speech first."
            return None
        r = {k: t.result for k, t in self.takes.items()}
        p = MicProfile(dev.name, dev.hostapi, int(dev.sample_rate), int(dev.channels))
        p.noise_floor_db = r["silence"]["noise_floor_db"]
        p.speech_level_db = r["normal"].get("speech_db")
        p.snr_db = r["normal"].get("snr_db")
        p.snr_rating = M.snr_rating(p.snr_db)
        p.analysis_gain_db = M.recommended_gain_db(p.speech_level_db)
        p.hum_hz = r["silence"].get("hum_hz")
        p.analysis_clean = bool(r["normal"].get("clean"))
        p.clean_judged = bool(r["normal"].get("clean_judged"))
        p.clean_reasons = list(r["normal"].get("clean_reasons") or [])
        p.noise_bands_db = r["silence"].get("bands_db", {})
        p.speech_bands_db = r["normal"].get("bands_db", {})
        p.clipped_samples = sum(x.get("clipped", 0) for x in r.values())
        p.flat_top_runs = sum(x.get("flat_tops", 0) for x in r.values())
        if "loud" in r:
            p.loud_peak_db = r["loud"]["peak_db"]
        if "quiet" in r:
            p.quiet_detected_share = r["quiet"].get("heard_share")
        if "echo" in r:
            p.echo_speech_s = r["echo"].get("speech_heard_s")
        p.advice = self.advice(p, r)
        if dev.channels > 1:
            p.notes.append(f"The mic offers {dev.channels} channels; channel 1 is recorded.")
        if "steam streaming" in dev.name.lower():
            p.notes.append("Streamed from the headset over Steam Link (compressed); compare with a cable or local mic "
                           "if the vocal measurements look unstable.")
        return p

    @staticmethod
    def advice(p: MicProfile, r: dict[str, dict[str, Any]]) -> list[str]:
        out = []
        if p.clipped_samples or p.flat_top_runs or (p.loud_peak_db is not None and p.loud_peak_db > -3):
            out.append("Loud speech clips or comes within 3 dB of full scale: lower this mic's level in Windows "
                       "Sound settings, then record Loud speech again.")
        elif p.loud_peak_db is not None and p.loud_peak_db < -20:
            out.append("Even loud speech peaks below -20 dBFS: raise this mic's Windows level, then record again.")
        if "loud" not in r:
            out.append("Loud speech not recorded: clipping headroom is unknown.")
        if p.analysis_gain_db is not None and p.analysis_gain_db > 12:
            out.append("Normal speech is very quiet; raising the Windows level beats adding digital gain.")
        if p.snr_rating == "poor":
            out.append("SNR under 15 dB: loudness readings from this mic will be unreliable. Reduce room noise or "
                       "try the other mic.")
        if p.hum_hz:
            out.append(f"Mains hum at {p.hum_hz} Hz: a notch filter is a candidate for the speech-to-text path.")
        if not p.analysis_clean:
            out.append("Processing before Windows can't be ruled out; vocal loudness will be treated as relative only."
                       if p.clean_judged or not p.clean_reasons else
                       "Not enough pauses to judge upstream processing; read the passage with natural pauses and "
                       "record Normal speech again.")
        if p.quiet_detected_share is not None and p.quiet_detected_share < 0.7:
            out.append(f"Mantella's speech detector (threshold {MANTELLA_VAD_THRESHOLD}) heard only "
                       f"{p.quiet_detected_share:.0%} of your quiet speech; quiet lines may be missed.")
        if p.echo_speech_s is not None and p.echo_speech_s > 0.5:
            out.append(f"NPC speech leaked into the mic ({p.echo_speech_s:.1f} s heard as speech): keep Allow "
                       "Interruption off, or lower game volume.")
        return out

    def save_profile(self) -> None:
        p = self.build_profile()
        if p is None:
            return
        key = self.store.save(p, make_active=True)
        self.profile = p
        self.message = f"Saved profile '{key}' to {self.store.path} (now the active mic)."

    # files ----------------------------------------------------------------------------------------------
    @property
    def recordings_dir(self) -> Path:
        return self.root / "calibration"

    def _save_wav(self, name: str, audio: np.ndarray, sr: int) -> str:
        dev = self._device()
        folder = self.recordings_dir / mic_slug(f"{dev.name}_{dev.hostapi}" if dev else "mic")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.wav"
        try:
            import soundfile as sf
            sf.write(str(path), audio, sr, subtype="FLOAT")
        except ImportError:
            import wave
            pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(int(sr))
                wf.writeframes(pcm.tobytes())
        return str(path)

    def recordings_on_disk(self) -> list[Path]:
        d = self.recordings_dir
        return sorted(d.glob("*/*.wav")) if d.exists() else []

    def delete_recordings(self) -> None:
        n = len(self.recordings_on_disk())
        if self.recordings_dir.exists():
            shutil.rmtree(self.recordings_dir, ignore_errors=True)
        for t in self.takes.values():
            t.path = None
        self.message = f"Deleted {n} calibration recording(s). Measurements on this page and saved profiles stay."

    def play(self, step_id: str) -> None:
        t = self.takes.get(step_id)
        if t is None:
            self.message = "Nothing recorded for that step yet."
            return
        try:
            import sounddevice as sd
            sd.play(t.audio, t.sample_rate)
            self.message = f"Playing {STEP_BY_ID[step_id].title} (raw, as recorded)."
        except Exception as e:
            self.message = f"Could not play: {e}"

    # page -----------------------------------------------------------------------------------------------
    def action(self, name: str, body: dict[str, Any]) -> None:
        step = str(body.get("step", ""))
        if name == "record":
            self.record(step)
        elif name == "cancel":
            self.cancel()
        elif name == "play":
            self.play(step)
        elif name == "save":
            self.save_profile()
        elif name == "delete":
            self.delete_recordings()
        elif name == "activate":
            ok = self.store.set_active(str(body.get("key", "")))
            self.message = "Active mic changed." if ok else "No such profile."
        else:
            self.message = f"Unknown calibration action {name}"

    def state(self) -> dict[str, Any]:
        with self.lock:
            self._check_mic()
            dev = self._device()
            rec = None
            if self._rec_step is not None:
                done = len(self._rec_blocks) / max(1, self._rec_target)
                rec = {"step": self._rec_step.id, "left_s": round(self._rec_step.seconds * (1 - done), 1)}
            preview = self.build_profile() if ("silence" in self.takes and "normal" in self.takes) else None
            profiles = self.store.all()
            active = self.store.active()
            return {
                "device": None if dev is None else {"name": dev.name, "hostapi": dev.hostapi,
                                                     "sample_rate": dev.sample_rate, "channels": dev.channels,
                                                     "status": self.lab.capture.status},
                "level_db": round(float(self.lab.level_db), 1), "peak_db": round(float(self.lab.peak_hold_db), 1),
                "clipped": int(self.lab.clipped_total),
                "steps": [{"id": s.id, "title": s.title, "prompt": s.prompt, "text": s.text, "seconds": s.seconds,
                           "result": (self.takes[s.id].result if s.id in self.takes else None)} for s in STEPS],
                "recording": rec,
                "preview": None if preview is None else preview.to_json(),
                "profiles": [{"key": k, "snr_db": p.snr_db, "snr_rating": p.snr_rating, "clean": p.analysis_clean,
                              "created": p.created} for k, p in profiles.items()],
                "active": None if active is None else profile_key(active.device_name, active.hostapi),
                "profile_file": str(self.store.path),
                "recordings_dir": str(self.recordings_dir),
                "recordings": len(self.recordings_on_disk()),
                "message": self.message,
            }


CAL_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mic Calibration</title>
<style>
:root{--bg:#f6f5f2;--fg:#1d1d1b;--mut:#6b6a66;--card:#fff;--line:#e2e0da;--acc:#2f6f5e;--warn:#b5532a;--bar:#d8d5cc}
@media (prefers-color-scheme:dark){:root{--bg:#181816;--fg:#ecebe6;--mut:#9c9a93;--card:#22221f;--line:#34332f;--acc:#6fbfa6;--warn:#e08a5f;--bar:#3a3934}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,Segoe UI,sans-serif}
main{max-width:1000px;margin:0 auto;padding:16px}main>*{min-width:0}h1{font-size:22px;margin:4px 0 12px}h2{font-size:16px;margin:0 0 6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:12px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
button{font:inherit;padding:6px 12px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button.primary{background:var(--acc);color:#fff;border-color:var(--acc)}button.danger{color:var(--warn)}
.meter{position:relative;height:14px;background:var(--bar);border-radius:7px;overflow:hidden;flex:1;min-width:100px}
.meter i{position:absolute;left:0;top:0;bottom:0;background:var(--acc)}.meter b{position:absolute;top:0;bottom:0;width:2px;background:var(--warn)}
.mut{color:var(--mut)}.bad{color:var(--warn)}.ok{color:var(--acc)}.text{font-size:17px;margin:6px 0;max-width:70ch}
table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:4px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.wrap{overflow-x:auto}a{color:var(--acc)}
</style></head><body><main>
<h1>Mic calibration</h1>
<p class="mut">Pick the mic on the <a href="/">Voice Lab page</a> first. Recordings stay on this PC; only numbers go into the profile.</p>
<div class="card"><h2>Microphone</h2><p id="dev" class="mut"></p>
 <div class="row"><span>Level</span><div class="meter"><i id="lvl"></i><b id="pk"></b></div><span id="lvltxt" class="mut"></span></div>
 <p>Clipped samples: <b id="clip">0</b></p></div>
<div id="steps"></div>
<div class="card"><h2>Profile</h2><div id="prev" class="mut">Record Room silence and Normal speech to see the profile.</div>
 <div class="row" style="margin-top:8px"><button class="primary" onclick="act('save')">Save profile for this mic</button></div>
 <div class="wrap" style="margin-top:10px"><table id="profiles"></table></div><p id="pfile" class="mut"></p></div>
<div class="card"><h2>Files</h2><p id="files" class="mut"></p><button class="danger" onclick="act('delete')">Delete calibration recordings</button></div>
<p id="msg" style="font-size:18px"></p>
</main><script>
async function act(n,b){await fetch('/api/cal/'+n,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});tick()}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
const pct=db=>Math.max(0,Math.min(100,(db+80)/80*100));
const LABELS={seconds:'Length (s)',rms_db:'Overall level (dBFS)',peak_db:'Peak (dBFS)',clipped:'Clipped samples',flat_tops:'Flat-top runs',dc_offset:'DC offset',noise_floor_db:'Noise floor (dBFS)',speech_heard_s:'Heard as speech by Mantella (s)',hum_hz:'Mains hum (Hz)',speech_db:'Speech level (dBFS)',snr_db:'SNR (dB)',clean:'Analysis signal looks clean',clean_judged:'Enough pauses to judge',gap_floor_db:'Level in pauses (dBFS)',heard_share:'Share Mantella hears'};
function res(r){if(!r)return '<p class="mut">Not recorded yet.</p>';return '<div class="wrap"><table>'+Object.keys(LABELS).filter(k=>k in r).map(k=>`<tr><td>${LABELS[k]}</td><td>${esc(r[k]==null?'–':r[k])}</td></tr>`).join('')+(r.clean_reasons&&r.clean_reasons.length?`<tr><td>Why</td><td class="bad">${r.clean_reasons.map(esc).join('<br>')}</td></tr>`:'')+'</table></div>'}
function render(s){
 document.getElementById('dev').textContent=s.device?`${s.device.name} (${s.device.hostapi}, ${s.device.sample_rate} Hz, ${s.device.channels} ch): ${s.device.status}`:'No mic selected. Pick one on the Voice Lab page.';
 document.getElementById('lvl').style.width=pct(s.level_db)+'%';document.getElementById('pk').style.left=pct(s.peak_db)+'%';
 document.getElementById('lvltxt').textContent=`${s.level_db} dBFS, peak ${s.peak_db}`;
 const c=document.getElementById('clip');c.textContent=s.clipped;c.className=s.clipped?'bad':'';
 const R=s.recording;
 document.getElementById('steps').innerHTML=s.steps.map((st,i)=>`<div class="card"><h2>${i+1}. ${esc(st.title)} <span class="mut">${st.seconds} s</span></h2><p>${esc(st.prompt)}</p>${st.text?`<p class="text">“${esc(st.text)}”</p>`:''}
 <div class="row">${R&&R.step===st.id?`<b class="bad">Recording… ${R.left_s} s</b><button onclick="act('cancel')">Cancel</button>`:`<button class="primary" ${R?'disabled':''} onclick="act('record',{step:'${st.id}'})">${st.result?'Record again':'Record'}</button>`}
 ${st.result?`<button onclick="act('play',{step:'${st.id}'})">Play back</button>`:''}</div>${res(st.result)}</div>`).join('');
 const P=s.preview;
 document.getElementById('prev').innerHTML=P?`<table>
 <tr><td>Analysis gain (fixed)</td><td>${P.analysis_gain_db==null?'–':P.analysis_gain_db+' dB'}</td></tr>
 <tr><td>Noise floor</td><td>${P.noise_floor_db} dBFS</td></tr><tr><td>Speech level</td><td>${P.speech_level_db==null?'–':P.speech_level_db+' dBFS'}</td></tr>
 <tr><td>SNR</td><td class="${P.snr_rating==='poor'?'bad':'ok'}">${P.snr_db==null?'–':P.snr_db+' dB'} (${P.snr_rating})</td></tr>
 <tr><td>Analysis signal clean</td><td class="${P.analysis_clean?'ok':'bad'}">${P.analysis_clean?'yes':'no'}</td></tr>
 <tr><td>Advice</td><td>${P.advice.length?P.advice.map(esc).join('<br>'):'None: this mic is ready.'}</td></tr>
 ${P.notes.length?`<tr><td>Notes</td><td class="mut">${P.notes.map(esc).join('<br>')}</td></tr>`:''}</table>`:'Record Room silence and Normal speech to see the profile.';
 document.getElementById('profiles').innerHTML=s.profiles.length?'<tr><th>Saved profiles</th><th>SNR</th><th>Clean</th><th>Saved</th><th></th></tr>'+s.profiles.map(p=>`<tr><td>${esc(p.key)}</td><td>${p.snr_db==null?'–':p.snr_db+' dB'} (${esc(p.snr_rating)})</td><td>${p.clean?'yes':'no'}</td><td>${esc(p.created)}</td><td>${p.key===s.active?'<b class="ok">active</b>':`<button onclick='act("activate",{key:${JSON.stringify(p.key)}})'>Make active</button>`}</td></tr>`).join(''):'';
 document.getElementById('pfile').textContent='Profiles file: '+s.profile_file;
 document.getElementById('files').textContent=`Recordings folder: ${s.recordings_dir}. Recordings on disk: ${s.recordings}.`;
 document.getElementById('msg').textContent=s.message||'';
}
async function tick(){try{const r=await fetch('/api/cal/state');render(await r.json())}catch(e){}}
setInterval(tick,400);tick();
</script></body></html>
"""
