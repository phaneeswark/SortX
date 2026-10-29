#!/usr/bin/env python3
"""
.Image - finds pictures in a folder that are exact duplicates, or that look alike.

Exact duplicates (pixel for pixel):
Two pictures count as duplicates when they show exactly the same pixels, even if the
files themselves differ (different names, metadata, or a lossless format change such as
PNG vs TIFF). Photos are compared upright, using their rotation tag.

How it stays fast:
  1. Read each picture's dimensions (header only).
  2. Only pictures with the same dimensions can match, so only those are looked at further.
  3. Byte-identical files are spotted with a file hash, so each unique file is decoded once.
  4. Decoded pixels are hashed and compared.

HEIC, RAW and other formats Pillow can't read are decoded with macOS's built-in `sips`.

Usage:
    python3 image_dupes.py ~/Pictures
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import threading
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import file_sorter as fs

try:
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = 400_000_000  # large panoramas are fine
    HAVE_PIL = True
except Exception:  # pragma: no cover
    HAVE_PIL = False

IMAGE_EXTS = {
    "jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp", "heic", "heif", "avif",
    "ico", "psd", "cr2", "cr3", "nef", "arw", "dng", "orf", "rw2", "raf", "srw",
}
SKIP_DIRS = (".app", ".photoslibrary", ".bundle")
DECODE_LOCK = threading.BoundedSemaphore(4)  # limits memory use when decoding big photos

Progress = Callable[[str, int, int], None]


def is_image(path: Path) -> bool:
    return path.suffix.lower().lstrip(".") in IMAGE_EXTS


# --------------------------------------------------------------------------- #
# Reading pictures
# --------------------------------------------------------------------------- #

def _sips_dims(path: Path) -> Optional[Tuple[int, int]]:
    proc = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
                          capture_output=True, text=True)
    w = h = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("pixelWidth:"):
            w = int(line.split(":")[1])
        elif line.startswith("pixelHeight:"):
            h = int(line.split(":")[1])
    return (w, h) if w and h else None


def picture_size(path: Path) -> Optional[Tuple[int, int]]:
    """Upright (width, height) from the file header, or None if it can't be read."""
    if HAVE_PIL:
        try:
            with Image.open(path) as im:
                w, h = im.size
                try:
                    if im.getexif().get(0x0112) in (5, 6, 7, 8):  # rotated 90/270 degrees
                        w, h = h, w
                except Exception:
                    pass
                return w, h
        except Exception:
            pass
    if sys.platform == "darwin":
        try:
            return _sips_dims(path)
        except Exception:
            return None
    return None


def _open_picture(path: Path) -> "Image.Image":
    """Open a picture with Pillow, falling back to macOS sips for HEIC/RAW."""
    try:
        im = Image.open(path)
        im.load()
        return im
    except Exception:
        if sys.platform != "darwin":
            raise
    with tempfile.TemporaryDirectory(prefix="sortx-") as tmp:
        out = Path(tmp) / "decoded.png"
        proc = subprocess.run(["sips", "-s", "format", "png", str(path), "--out", str(out)],
                              capture_output=True, text=True)
        if proc.returncode != 0 or not out.exists():
            raise OSError(f"Can't read {path.name}")
        im = Image.open(out)
        im.load()
        return im


def pixel_hash(path: Path) -> str:
    """Hash of the picture's pixels (upright). Same picture = same hash, whatever the file."""
    with DECODE_LOCK:
        im = _open_picture(path)
        try:
            if getattr(im, "n_frames", 1) > 1:
                # animations: compare the whole file instead of just the first frame
                return "file:" + fs.full_hash(path)
            im = ImageOps.exif_transpose(im)
            rgba = im.convert("RGBA")
            h = hashlib.blake2b(digest_size=20)
            h.update(f"{rgba.width}x{rgba.height}|".encode())
            h.update(rgba.tobytes())
            return "px:" + h.hexdigest()
        finally:
            im.close()


# --------------------------------------------------------------------------- #
# Look-alike matching ("similar pictures")
# --------------------------------------------------------------------------- #
# Each picture gets two 64-bit "difference hashes": the picture is shrunk to a 9x8 (and 8x9)
# grey thumbnail and each bit says whether a pixel is brighter than its neighbour. Resizing,
# re-compressing, format changes and small edits barely change these bits, while different
# pictures differ in about half of them. Because those hashes ignore colour, a small colour
# signature (3x3 cells, brightness removed) is also compared, so the same design in another
# colour isn't called "similar" unless you ask for loose matching.
#
# Limits were tuned on real photos: resized / re-compressed / brighter / blurred copies differ
# by 0-3 bits, watermarks and small crops by up to about 13, different photos by 13 or more.

SIMILARITY_LEVELS = {     # (max differing bits out of 64, max colour difference or None)
    "strict": (3, 4.0),       # very close: resized, re-compressed or re-saved copies
    "balanced": (12, 9.0),    # also light edits: brightness, watermark, small crop, dark/light
    "loose": (16, None),      # also greyscale / recoloured copies; check these sets carefully
}


def _bits(x: int) -> int:
    return bin(x).count("1")


def _small_picture(path: Path) -> "Image.Image":
    """A small, upright, opaque RGB copy of the picture, read as cheaply as possible."""
    im = None
    try:
        im = Image.open(path)
        if im.format == "JPEG":
            im.draft("RGB", (160, 160))     # JPEGs can be decoded at reduced size: much faster
        im.load()
    except Exception:
        if sys.platform != "darwin":
            raise
        with tempfile.TemporaryDirectory(prefix="sortx-") as tmp:
            out = Path(tmp) / "small.png"
            subprocess.run(["sips", "-s", "format", "png", "-Z", "160", str(path), "--out", str(out)],
                           capture_output=True)
            if not out.exists():
                raise OSError(f"Can't read {path.name}")
            im = Image.open(out)
            im.load()
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.alpha_composite(im.convert("RGBA"))
        im = bg
    return im.convert("RGB")


def look_hash(path: Path) -> Tuple[int, int, tuple]:
    """(horizontal hash, vertical hash, colour signature) of the picture."""
    with DECODE_LOCK:
        rgb = _small_picture(path)
    colour = []
    for r, g_, b in rgb.resize((3, 3), Image.BOX).getdata():
        m = (r + g_ + b) / 3
        colour += [r - m, g_ - m, b - m]
    g = rgb.convert("L")
    hz = list(g.resize((9, 8), Image.LANCZOS).getdata())
    vt = list(g.resize((8, 9), Image.LANCZOS).getdata())
    h = v = 0
    for row in range(8):
        for col in range(8):
            h = (h << 1) | (hz[row * 9 + col] > hz[row * 9 + col + 1])
            v = (v << 1) | (vt[row * 8 + col] > vt[(row + 1) * 8 + col])
    return h, v, tuple(colour)


def colour_difference(a: tuple, b: tuple) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / max(1, len(a))


class _BKTree:
    """Finds hashes within a few bits of each other without comparing every pair."""

    def __init__(self) -> None:
        self.root = None   # [hash, [items], {distance: child}]

    def add(self, key: int, item) -> None:
        if self.root is None:
            self.root = [key, [item], {}]
            return
        node = self.root
        while True:
            d = _bits(key ^ node[0])
            if d == 0:
                node[1].append(item)
                return
            nxt = node[2].get(d)
            if nxt is None:
                node[2][d] = [key, [item], {}]
                return
            node = nxt

    def near(self, key: int, radius: int) -> List[tuple]:
        found, stack = [], [self.root] if self.root else []
        while stack:
            node = stack.pop()
            d = _bits(key ^ node[0])
            if d <= radius:
                found.extend((item, d) for item in node[1])
            for cd, child in node[2].items():
                if d - radius <= cd <= d + radius:
                    stack.append(child)
        return found


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #

class ImageScanner:
    def __init__(self, source: Path, recursive: bool = True, similar: bool = True,
                 sensitivity: str = "balanced",
                 progress: Optional[Progress] = None, log: Callable[[str], None] = print,
                 stop_event: Optional[threading.Event] = None):
        self.source = source.expanduser().resolve()
        self.recursive = recursive
        self.similar = similar
        self.sensitivity = sensitivity if sensitivity in SIMILARITY_LEVELS else "balanced"
        self.progress = progress or (lambda phase, done, total: None)
        self.log = log
        self.stop_event = stop_event or threading.Event()

    def _check_stop(self) -> None:
        if self.stop_event.is_set():
            raise fs.Cancelled()

    def find_pictures(self) -> List[Path]:
        found: List[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.source):
            self._check_stop()
            dirnames[:] = [n for n in dirnames if not n.startswith(".") and not n.endswith(SKIP_DIRS)]
            if not self.recursive:
                dirnames[:] = []
            for name in filenames:
                if name.startswith(".") or name in fs.IGNORED_NAMES:
                    continue
                p = Path(dirpath) / name
                if is_image(p) and p.is_file() and not p.is_symlink():
                    found.append(p)
        return sorted(found)

    @staticmethod
    def _rank(info: dict):
        """Which copy to treat as the original: a clean name, then the oldest, then the shortest path."""
        p = Path(info["path"])
        return (1 if fs.COPY_NAME_RE.search(p.stem) else 0, info["mtime"], len(info["path"]))

    def scan(self) -> dict:
        if not self.source.is_dir():
            raise NotADirectoryError(f"Not a folder: {self.source}")
        self.log(f"Looking for pictures in {self.source} ...")
        self.progress("Finding pictures", 0, 0)
        paths = self.find_pictures()
        self.log(f"Found {len(paths)} picture(s).")

        # 1. dimensions from the file header
        infos: List[dict] = []
        errors: List[str] = []
        for i, p in enumerate(paths, 1):
            self._check_stop()
            try:
                st = p.stat()
            except OSError:
                continue
            dims = picture_size(p)
            infos.append({"path": str(p), "size": st.st_size, "mtime": st.st_mtime,
                          "width": dims[0] if dims else None, "height": dims[1] if dims else None})
            if i % 25 == 0 or i == len(paths):
                self.progress("Reading pictures", i, len(paths))

        # 2. only pictures with the same dimensions can be the same picture
        #    (unreadable ones fall back to comparing file contents, grouped by file size)
        buckets: Dict[tuple, List[dict]] = defaultdict(list)
        for info in infos:
            key = ("dims", info["width"], info["height"]) if info["width"] else ("bytes", info["size"])
            buckets[key].append(info)
        candidates = [b for b in buckets.values() if len(b) > 1]
        total = sum(len(b) for b in candidates)
        self.log(f"{total} picture(s) share their size with another picture; comparing those.")

        # 3 + 4. file hash, then pixel hash once per unique file
        done = 0
        decoded: Dict[str, str] = {}   # file hash -> pixel hash
        by_content: Dict[str, List[dict]] = defaultdict(list)
        self.progress("Comparing pictures", 0, total)
        for bucket in candidates:
            for info in bucket:
                self._check_stop()
                p = Path(info["path"])
                try:
                    fh = fs.full_hash(p, check=self._check_stop)
                    info["file_hash"] = fh
                    if info["width"] is None:
                        key = "file:" + fh
                    else:
                        if fh not in decoded:
                            try:
                                decoded[fh] = pixel_hash(p)
                            except fs.Cancelled:
                                raise
                            except Exception as e:
                                errors.append(f"{p.name}: couldn't read the picture ({e}); compared by file instead")
                                decoded[fh] = "file:" + fh
                        key = decoded[fh]
                    by_content[key].append(info)
                except fs.Cancelled:
                    raise
                except OSError as e:
                    errors.append(f"Could not read {p}: {e}")
                done += 1
                self.progress("Comparing pictures", done, total)

        groups = []
        for members in by_content.values():
            if len(members) < 2:
                continue
            members.sort(key=self._rank)
            original = members[0]
            for j, m in enumerate(members):
                m["state"] = "original" if j == 0 else "duplicate"
                m["same_file"] = m.get("file_hash") == original.get("file_hash")
            groups.append(members)
        groups.sort(key=lambda g: -sum(m["size"] for m in g[1:]))  # biggest savings first

        for gid, g in enumerate(groups):
            for m in g:
                m["group"] = gid
        extra = sum(len(g) - 1 for g in groups)
        saved = sum(m["size"] for g in groups for m in g[1:])
        self.log(f"Found {len(groups)} set(s) of exact duplicates: {extra} extra copies, "
                 f"{fs.human_size(saved)} you could free up.")

        similar = self._find_similar(infos, errors) if self.similar else []
        return {"source": str(self.source), "pictures": infos, "groups": groups,
                "extra_copies": extra, "bytes_saved": saved,
                "similar": similar, "similar_checked": self.similar, "sensitivity": self.sensitivity,
                "similar_extra": sum(len(g) - 1 for g in similar),
                "similar_bytes": sum(m["size"] for g in similar for m in g[1:]),
                "errors": errors[:200]}

    @staticmethod
    def _best_rank(info: dict):
        """Which look-alike to suggest keeping: most pixels, then a clean name, then the oldest
        (edited copies are usually newer). File size isn't used: a PNG is bigger than a JPEG of
        the same picture without being better."""
        p = Path(info["path"])
        pixels = (info["width"] or 0) * (info["height"] or 0)
        return (-pixels, 1 if fs.COPY_NAME_RE.search(p.stem) else 0, info["mtime"], len(info["path"]))

    def _find_similar(self, infos: List[dict], errors: List[str]) -> List[List[dict]]:
        """Sets of pictures that look alike. Extra exact copies are left out (the Exact tab
        handles those), so each picture is compared once."""
        units = [i for i in infos if i.get("state") != "duplicate"]
        limit, colour_limit = SIMILARITY_LEVELS[self.sensitivity]
        self.log(f"Looking for similar pictures among {len(units)} (sensitivity: {self.sensitivity}).")
        hashed = []
        self.progress("Finding look-alikes", 0, len(units))
        for i, info in enumerate(units, 1):
            self._check_stop()
            try:
                info["_h"], info["_v"], info["_c"] = look_hash(Path(info["path"]))
                hashed.append(info)
            except fs.Cancelled:
                raise
            except Exception as e:
                errors.append(f"{Path(info['path']).name}: skipped for look-alikes ({e})")
            if i % 10 == 0 or i == len(units):
                self.progress("Finding look-alikes", i, len(units))

        # pairs within the limit on both hashes, joined into sets
        tree = _BKTree()
        for idx, info in enumerate(hashed):
            tree.add(info["_h"], idx)
        parent = list(range(len(hashed)))

        def find(a: int) -> int:
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for idx, info in enumerate(hashed):
            self._check_stop()
            for other, _dh in tree.near(info["_h"], limit):
                if other <= idx:
                    continue
                o = hashed[other]
                if _bits(info["_v"] ^ o["_v"]) > limit:
                    continue
                if colour_limit is not None and colour_difference(info["_c"], o["_c"]) > colour_limit:
                    continue
                parent[find(other)] = find(idx)
        sets: Dict[int, List[dict]] = defaultdict(list)
        for idx, info in enumerate(hashed):
            sets[find(idx)].append(info)

        similar = []
        for members in sets.values():
            if len(members) < 2:
                continue
            members.sort(key=self._best_rank)
            best = members[0]
            for j, m in enumerate(members):
                dist = _bits(m["_h"] ^ best["_h"]) + _bits(m["_v"] ^ best["_v"])
                m["sstate"] = "best" if j == 0 else "similar"
                m["likeness"] = max(0, 100 - dist)      # 100 = looks the same
            similar.append(members)
        similar.sort(key=lambda g: (-len(g), -sum(m["size"] for m in g[1:])))
        for gid, g in enumerate(similar):
            for m in g:
                m["sgroup"] = gid
        for info in hashed:   # internal only
            for k in ("_h", "_v", "_c"):
                info.pop(k, None)
        self.log(f"Found {len(similar)} set(s) of similar pictures "
                 f"({sum(len(g) - 1 for g in similar)} look-alikes).")
        return similar


# --------------------------------------------------------------------------- #
# Thumbnails and previews (for the window)
# --------------------------------------------------------------------------- #

CACHE_DIR = Path.home() / "Library" / "Caches" / "Sort X" / "previews"
_RECENT: Dict[Tuple[str, int], Tuple[Path, str]] = {}   # keeps previews of pictures moved to the Trash


def preview(path: Path, max_side: int) -> Tuple[bytes, str]:
    """A JPEG (or PNG, for transparent pictures) no bigger than max_side, cached on disk."""
    if not path.exists() and (str(path), max_side) in _RECENT:   # e.g. just moved to the Trash
        cached, ctype = _RECENT[(str(path), max_side)]
        if cached.exists():
            return cached.read_bytes(), ctype
    st = path.stat()
    key = hashlib.blake2b(f"{path}|{st.st_size}|{st.st_mtime}|{max_side}".encode(),
                          digest_size=16).hexdigest()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for ext, ctype in ((".jpg", "image/jpeg"), (".png", "image/png")):
        cached = CACHE_DIR / (key + ext)
        if cached.exists():
            _RECENT[(str(path), max_side)] = (cached, ctype)
            return cached.read_bytes(), ctype
    with DECODE_LOCK:
        try:
            im = _open_picture(path)
        except Exception:
            im = None
        if im is not None:
            try:
                im = ImageOps.exif_transpose(im)
                im.thumbnail((max_side, max_side))
                has_alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
                out = CACHE_DIR / (key + (".png" if has_alpha else ".jpg"))
                if has_alpha:
                    im.convert("RGBA").save(out, "PNG", optimize=True)
                else:
                    im.convert("RGB").save(out, "JPEG", quality=84)
            finally:
                im.close()
            ctype = "image/png" if has_alpha else "image/jpeg"
            _RECENT[(str(path), max_side)] = (out, ctype)
            return out.read_bytes(), ctype
    # last resort: let macOS make it
    out = CACHE_DIR / (key + ".jpg")
    subprocess.run(["sips", "-s", "format", "jpeg", "-Z", str(max_side), str(path), "--out", str(out)],
                   capture_output=True)
    if not out.exists():
        raise OSError(f"No preview for {path.name}")
    _RECENT[(str(path), max_side)] = (out, "image/jpeg")
    return out.read_bytes(), "image/jpeg"


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=".Image: find pictures that are exact duplicates.")
    ap.add_argument("folder")
    ap.add_argument("--no-recursive", action="store_true", help="ignore subfolders")
    ap.add_argument("--similar", choices=sorted(SIMILARITY_LEVELS), nargs="?", const="balanced",
                    help="also list similar (look-alike) pictures: strict, balanced or loose")
    args = ap.parse_args(argv)
    res = ImageScanner(Path(args.folder), recursive=not args.no_recursive,
                       similar=bool(args.similar), sensitivity=args.similar or "balanced",
                       log=lambda m: None).scan()
    print(f"{len(res['pictures'])} pictures checked, {len(res['groups'])} duplicate sets, "
          f"{res['extra_copies']} extra copies ({fs.human_size(res['bytes_saved'])}).\n")
    for n, g in enumerate(res["groups"], 1):
        print(f"Set {n}: {g[0]['width']}x{g[0]['height']}")
        for m in g:
            tag = "ORIGINAL " if m["state"] == "original" else ("DUPLICATE" if m["same_file"] else "DUP*     ")
            print(f"  {tag} {m['path']}")
    if any(not m["same_file"] for g in res["groups"] for m in g):
        print("\n* same picture, different file (e.g. other metadata or format)")
    if res["similar_checked"]:
        print(f"\nSimilar pictures ({res['sensitivity']}): {len(res['similar'])} sets\n")
        for n, g in enumerate(res["similar"], 1):
            print(f"Similar set {n}:")
            for m in g:
                tag = "KEEP (best)" if m["sstate"] == "best" else f"{m['likeness']:>3}% alike"
                print(f"  {tag:<11} {m['width']}x{m['height']}  {m['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
