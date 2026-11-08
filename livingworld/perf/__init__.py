"""VR performance monitoring for the Living World build (``MANTELLA_LW_PERF=1``).

``stats``: counters the server always keeps about its own work (cheap, read-only for everyone else).
``monitor``: the background thread that samples SteamVR, the game window and the game process with those
counters, and writes a timeline. ``game_records``: the game's own timing markers. ``report``: reads a
timeline and says whether trouble came from rendering, the main thread, scripts, AI processing or memory.
"""
