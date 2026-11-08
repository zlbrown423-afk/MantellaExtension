"""What the performance monitor can see from outside the game.

Each sampler is optional and fails soft: a missing package, a missing SteamVR or a non-Windows machine
just leaves its fields out.

* ``OpenVRSampler``: SteamVR's own frame timings (the numbers in SteamVR's Frame Timing graph), read as a
  background OpenVR application, so it never draws or submits anything. Needs ``pip install openvr``.
* ``GameWindowSampler``: whether the game's main thread is answering. It sends the game window a
  harmless ``WM_NULL`` and times the answer (the game pumps window messages once per frame on its main
  thread, so a frozen main thread never answers), and asks Windows if it considers the window hung.
* ``ProcessSampler``: the game's memory, page faults and CPU, and how much memory the machine has free.
  Needs ``psutil``.
"""
from __future__ import annotations

import logging
import sys
import time
from typing import Any

logger = logging.getLogger("Mantella")

GAME_EXES = ("skyrimvr.exe", "skyrimse.exe", "skyrim.exe")
GAME_TITLES = ("Skyrim VR", "Skyrim Special Edition")


class OpenVRSampler:
    RETRY_SECONDS = 30.0

    def __init__(self) -> None:
        self._vr: Any = None
        self._compositor: Any = None
        self._last_try = -1e9
        self._last_frame = -1
        self._last_stats: dict[str, int] | None = None
        self.available = True
        try:
            import openvr  # noqa: F401
        except Exception:
            self.available = False

    def _connect(self) -> bool:
        if self._compositor is not None:
            return True
        if not self.available or time.monotonic() - self._last_try < self.RETRY_SECONDS:
            return False
        self._last_try = time.monotonic()
        try:
            import openvr
            self._vr = openvr.init(openvr.VRApplication_Background)
            self._compositor = openvr.VRCompositor()
            logger.info("Living World performance monitor: reading SteamVR frame timings")
            return True
        except Exception as e:   # SteamVR not running yet
            logger.debug(f"Living World performance monitor: SteamVR not available ({e})")
            self._compositor = None
            return False

    def close(self) -> None:
        if self._compositor is not None:
            try:
                import openvr
                openvr.shutdown()
            except Exception:
                pass
        self._compositor = None

    def _timing(self, frames_ago: int) -> Any:
        result = self._compositor.getFrameTiming(frames_ago)
        if isinstance(result, tuple):          # (ok, timing) in some pyopenvr versions
            ok, timing = result
            return timing if ok else None
        return result

    def sample(self) -> dict[str, Any]:
        if not self._connect():
            return {}
        try:
            frames = []
            for ago in range(0, 240):          # every frame since the last sample (up to ~2.6 s at 90 Hz)
                t = self._timing(ago)
                if t is None or t.m_nFrameIndex <= self._last_frame:
                    break
                frames.append(t)
            if frames:
                self._last_frame = frames[0].m_nFrameIndex
            out: dict[str, Any] = {"vr_frames": len(frames)}
            if frames:
                interval = [f.m_flClientFrameIntervalMs for f in frames]
                gpu = [f.m_flTotalRenderGpuMs for f in frames]
                pre_gpu = [f.m_flPreSubmitGpuMs for f in frames]
                cpu = [f.m_flCompositorRenderCpuMs for f in frames]
                out.update({
                    "cpu_frame_interval_ms_avg": round(sum(interval) / len(interval), 2),
                    "cpu_frame_interval_ms_max": round(max(interval), 2),
                    "gpu_total_ms_avg": round(sum(gpu) / len(gpu), 2),
                    "gpu_total_ms_max": round(max(gpu), 2),
                    "gpu_app_ms_avg": round(sum(pre_gpu) / len(pre_gpu), 2),
                    "compositor_cpu_ms_avg": round(sum(cpu) / len(cpu), 2),
                    "reprojected_frames": sum(1 for f in frames if f.m_nNumFramePresents > 1),
                    "mispresented": sum(f.m_nNumMisPresented for f in frames),
                    "dropped": sum(f.m_nNumDroppedFrames for f in frames),
                })
            stats = self._cumulative()
            if stats and self._last_stats:
                for key in ("presents", "dropped_total", "reprojected_total", "timed_out"):
                    out["vr_" + key + "_delta"] = stats[key] - self._last_stats[key]
            if stats:
                self._last_stats = stats
            return out
        except Exception as e:
            logger.debug(f"Living World performance monitor: SteamVR timing failed ({e}); reconnecting later")
            self.close()
            return {}

    def _cumulative(self) -> dict[str, int] | None:
        try:
            s = self._compositor.getCumulativeStats()
            if isinstance(s, tuple):
                s = s[-1]
            return {"presents": s.m_nNumFramePresents, "dropped_total": s.m_nNumDroppedFrames,
                    "reprojected_total": s.m_nNumReprojectedFrames, "timed_out": s.m_nNumTimedOut}
        except Exception:
            return None


class GameWindowSampler:
    TIMEOUT_MS = 1000
    FIND_RETRY_SECONDS = 10.0

    def __init__(self) -> None:
        self.available = sys.platform == "win32"
        self._hwnd = 0
        self._last_find = -1e9

    def _find(self) -> int:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        if self._hwnd and user32.IsWindow(self._hwnd):
            return self._hwnd
        if time.monotonic() - self._last_find < self.FIND_RETRY_SECONDS:
            return 0
        self._last_find = time.monotonic()
        for title in GAME_TITLES:
            hwnd = user32.FindWindowW(None, title)
            if hwnd:
                self._hwnd = hwnd
                return hwnd
        # fall back to the first visible top-level window of the game's process
        kernel32 = ctypes.windll.kernel32
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            proc = kernel32.OpenProcess(0x1000, False, pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION
            if proc:
                buf = ctypes.create_unicode_buffer(520)
                size = wintypes.DWORD(520)
                if kernel32.QueryFullProcessImageNameW(proc, 0, buf, ctypes.byref(size)):
                    if buf.value.lower().rsplit("\\", 1)[-1] in GAME_EXES:
                        found.append(hwnd)
                kernel32.CloseHandle(proc)
            return not found
        user32.EnumWindows(each, 0)
        self._hwnd = found[0] if found else 0
        return self._hwnd

    def sample(self) -> dict[str, Any]:
        if not self.available:
            return {}
        try:
            import ctypes
            from ctypes import wintypes
            hwnd = self._find()
            if not hwnd:
                return {"game_window": False}
            user32 = ctypes.windll.user32
            result = wintypes.DWORD()
            start = time.perf_counter()
            # SMTO_ABORTIFHUNG (2): return at once if Windows already thinks the window hangs
            answered = user32.SendMessageTimeoutW(hwnd, 0, 0, 0, 0x0002, self.TIMEOUT_MS, ctypes.byref(result))
            ms = (time.perf_counter() - start) * 1000
            return {"game_window": True, "main_thread_answer_ms": round(ms, 1), "main_thread_answered": bool(answered),
                    "main_thread_hung": bool(user32.IsHungAppWindow(hwnd))}
        except Exception as e:
            logger.debug(f"Living World performance monitor: window probe failed ({e})")
            return {}


class ProcessSampler:
    FIND_RETRY_SECONDS = 10.0

    def __init__(self) -> None:
        try:
            import psutil
            self._psutil: Any = psutil
            self.available = True
        except Exception:
            self._psutil = None
            self.available = False
        self._proc: Any = None
        self._last_find = -1e9
        self._last_faults: int | None = None

    def _find(self) -> Any:
        ps = self._psutil
        if self._proc is not None and self._proc.is_running():
            return self._proc
        if time.monotonic() - self._last_find < self.FIND_RETRY_SECONDS:
            return None
        self._last_find = time.monotonic()
        for p in ps.process_iter(["name"]):
            if (p.info.get("name") or "").lower() in GAME_EXES:
                self._proc = p
                p.cpu_percent(None)
                return p
        self._proc = None
        return None

    def sample(self) -> dict[str, Any]:
        if not self.available:
            return {}
        out: dict[str, Any] = {}
        try:
            vm = self._psutil.virtual_memory()
            out["system_free_mb"] = round(vm.available / 1048576)
            proc = self._find()
            if proc is not None:
                with proc.oneshot():
                    mem = proc.memory_info()
                    out["game_working_set_mb"] = round(mem.rss / 1048576)
                    private = getattr(mem, "private", None)
                    if private is not None:
                        out["game_private_mb"] = round(private / 1048576)
                    faults = getattr(mem, "num_page_faults", None)
                    if faults is not None:
                        if self._last_faults is not None:
                            out["game_page_faults"] = faults - self._last_faults
                        self._last_faults = faults
                    out["game_cpu_percent"] = proc.cpu_percent(None)
                    out["game_threads"] = proc.num_threads()
        except Exception as e:
            logger.debug(f"Living World performance monitor: process sample failed ({e})")
            self._proc = None
        return out
