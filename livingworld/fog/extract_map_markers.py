"""Read-only: list the map markers in the game's master files, for ``data/map_markers.json``.

Usage:  python extract_map_markers.py "<Skyrim Data folder>" [output.json]
Reads Skyrim.esm, Update.esm, Dawnguard.esm, Dragonborn.esm (those present) and their English .STRINGS
files. Writes only the output file (default: map_markers.json next to this script, in data/).

A map marker is a placed reference (REFR) with an XMRK subrecord; its name is the FULL subrecord after it.
"""
from __future__ import annotations

import json
import struct
import sys
import zlib
from pathlib import Path

MASTERS = ["Skyrim.esm", "Update.esm", "Dawnguard.esm", "Dragonborn.esm"]


def load_strings(path: Path) -> dict[int, str]:
    try:
        d = path.read_bytes()
    except OSError:
        return {}
    n, _ = struct.unpack_from("<II", d, 0)
    base, out = 8 + n * 8, {}
    for i in range(n):
        sid, off = struct.unpack_from("<II", d, 8 + i * 8)
        s = base + off
        out[sid] = d[s:d.index(b"\0", s)].decode("cp1252", "replace")
    return out


def subrecords(data: bytes):
    i, big = 0, None
    while i + 6 <= len(data):
        t = data[i:i + 4]
        size = struct.unpack_from("<H", data, i + 4)[0]
        i += 6
        if t == b"XXXX":
            big = struct.unpack_from("<I", data, i)[0]
            i += size
            continue
        if big is not None:
            size, big = big, None
        yield t, data[i:i + size]
        i += size


def walk(d: bytes, start: int, end: int, cb) -> None:
    i = start
    while i < end:
        t = d[i:i + 4]
        if t == b"GRUP":
            size = struct.unpack_from("<I", d, i + 4)[0]
            walk(d, i + 24, i + size, cb)
            i += size
            continue
        dsize, flags, fid = struct.unpack_from("<III", d, i + 4)
        if t == b"REFR":
            cb(fid, flags, d[i + 24:i + 24 + dsize])
        i += 24 + dsize


def markers_in(path: Path, strings: dict[int, str]) -> list[dict]:
    d = path.read_bytes()
    header_size = struct.unpack_from("<I", d, 4)[0]
    localized = bool(struct.unpack_from("<I", d, 8)[0] & 0x80)
    masters = [v.rstrip(b"\0").decode("ascii", "replace")
               for t, v in subrecords(d[24:24 + header_size]) if t == b"MAST"]
    files = masters + [path.name]
    found: list[dict] = []

    def on_ref(fid: int, flags: int, data: bytes) -> None:
        if b"XMRK" not in data[:4096] and not (flags & 0x40000):
            return
        if flags & 0x40000:
            data = zlib.decompress(data[4:])
        in_marker, name = False, None
        for t, v in subrecords(data):
            if t == b"XMRK":
                in_marker = True
            elif in_marker and t == b"FULL":
                if localized and len(v) == 4:
                    name = strings.get(struct.unpack_from("<I", v)[0])
                else:
                    name = v.rstrip(b"\0").decode("cp1252", "replace")
                break
        if not in_marker or not name:
            return
        index = fid >> 24
        owner = files[index] if index < len(files) else path.name
        found.append({"name": name, "plugin": owner, "form_id": f"0x{fid & 0xFFFFFF:08X}"})

    walk(d, 24 + header_size, len(d), on_ref)
    return found


def main(data_dir: str, out_path: str | None = None) -> int:
    data = Path(data_dir)
    by_ref: dict[tuple[str, str], dict] = {}
    for master in MASTERS:
        esm = data / master
        if not esm.exists():
            continue
        strings = load_strings(data / "Strings" / (esm.stem + "_ENGLISH.STRINGS"))
        for m in markers_in(esm, strings):
            by_ref[(m["plugin"], m["form_id"])] = m    # a later master's edit wins
    out = sorted(by_ref.values(), key=lambda m: (m["name"], m["plugin"], m["form_id"]))
    target = Path(out_path) if out_path else Path(__file__).parent / "data" / "map_markers.json"
    target.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(out)} map markers written to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
