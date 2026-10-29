#!/usr/bin/env python3
"""
Sort X - file sorter & deduplicator

Sorts the files in a folder into category folders (Images, Videos, Documents, ...),
optionally into Year/Month subfolders, and finds exact duplicate files by content.

Duplicates are never deleted: they are moved into a "Duplicates" folder so you can
review them, and every run writes a log that can be undone.

Usage:
    python3 file_sorter.py ~/Downloads                 # sort into ~/Downloads/Sorted
    python3 file_sorter.py ~/Downloads --dry-run       # preview only, change nothing
    python3 file_sorter.py ~/Pictures --by-date --copy # copy into Year/Month folders
    python3 file_sorter.py --undo ~/Downloads/Sorted/sort_log_20261029_141500.json
    python3 file_sorter.py                             # open the window

Works with the Python standard library. If Pillow is installed, photo dates are read
from EXIF data when sorting by date.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

try:
    from PIL import Image  # optional, for EXIF photo dates

    HAVE_PIL = True
except Exception:  # pragma: no cover
    HAVE_PIL = False


# --------------------------------------------------------------------------- #
# Categories
# --------------------------------------------------------------------------- #

CATEGORIES: Dict[str, set] = {
    "Images": {
        "jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp", "heic", "heif",
        "svg", "ico", "avif", "psd", "raw", "cr2", "cr3", "nef", "arw", "dng",
        "orf", "rw2", "raf", "srw",
    },
    "Videos": {
        "mp4", "mov", "avi", "mkv", "wmv", "flv", "webm", "m4v", "3gp", "mpg",
        "mpeg", "mts", "m2ts",
    },
    "Audio": {
        "mp3", "wav", "aac", "flac", "ogg", "m4a", "wma", "aif", "aiff", "opus",
        "mid", "midi",
    },
    "Documents": {
        "pdf", "doc", "docx", "txt", "rtf", "odt", "md", "pages", "tex", "epub",
        "mobi",
    },
    "Spreadsheets": {"xls", "xlsx", "xlsm", "csv", "tsv", "ods", "numbers"},
    "Presentations": {"ppt", "pptx", "odp", "key"},
    "Archives": {"zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz"},
    "Installers": {"dmg", "pkg", "iso", "exe", "msi", "apk", "deb", "rpm"},
    "Code": {
        "py", "js", "ts", "jsx", "tsx", "html", "htm", "css", "json", "xml", "yml",
        "yaml", "java", "c", "cpp", "h", "hpp", "cs", "go", "rs", "rb", "php", "sh",
        "swift", "kt", "sql", "ipynb", "r", "m",
    },
    "Fonts": {"ttf", "otf", "woff", "woff2"},
}
OTHER = "Other"
DUPLICATES_DIR = "Duplicates"

EXT_TO_CATEGORY = {ext: cat for cat, exts in CATEGORIES.items() for ext in exts}

# System junk that is never sorted.
IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", "Icon\r"}

# Names that look like copies ("photo copy.jpg", "photo (1).jpg", "photo 2.jpg").
COPY_NAME_RE = re.compile(r"(\bcopy\b|\(\d+\)$|[ _-]\d{1,2}$)", re.IGNORECASE)

LOG_PREFIX = "sort_log_"


def category_for(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    if ext in EXT_TO_CATEGORY:
        return EXT_TO_CATEGORY[ext]
    mime, _ = mimetypes.guess_type(path.name)
    if mime:
        top = mime.split("/")[0]
        return {"image": "Images", "video": "Videos", "audio": "Audio",
                "text": "Documents"}.get(top, OTHER)
    return OTHER


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def file_date(path: Path) -> datetime:
    """Best guess at when a file was created: EXIF for photos, else file times."""
    if HAVE_PIL and category_for(path) == "Images":
        try:
            with Image.open(path) as img:
                exif = img.getexif()
                raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)  # DateTimeOriginal / DateTime
                if raw:
                    return datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S")
        except Exception:
            pass
    st = path.stat()
    stamp = st.st_mtime
    birth = getattr(st, "st_birthtime", None)  # macOS
    if birth:
        stamp = min(stamp, birth)
    return datetime.fromtimestamp(stamp)


def quick_hash(path: Path, chunk: int = 64 * 1024) -> str:
    """Hash of the first and last 64 KB. Cheap pre-filter before a full hash."""
    h = hashlib.blake2b(digest_size=16)
    size = path.stat().st_size
    with path.open("rb") as f:
        h.update(f.read(chunk))
        if size > chunk * 2:
            f.seek(-chunk, os.SEEK_END)
            h.update(f.read(chunk))
    return h.hexdigest()


def full_hash(path: Path, chunk: int = 1024 * 1024,
              check: Optional[Callable[[], None]] = None) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            if check:
                check()  # lets a stop request interrupt huge files
            h.update(block)
    return h.hexdigest()


def unique_path(target: Path, taken: set) -> Path:
    """Return a path that doesn't exist yet, adding ' (1)', ' (2)' if needed."""
    if not target.exists() and target not in taken:
        return target
    stem, suffix = target.stem, target.suffix
    n = 1
    while True:
        candidate = target.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists() and candidate not in taken:
            return candidate
        n += 1


def _trash_dirs_for(path: Path) -> List[Path]:
    """Where macOS puts a file from this location when it's moved to the Trash."""
    dirs = [Path.home() / ".Trash"]
    parts = path.resolve().parts
    if len(parts) > 2 and parts[1] == "Volumes":       # external drive
        dirs.insert(0, Path("/", "Volumes", parts[2], ".Trashes", str(os.getuid())))
    return dirs


def move_to_trash(path: Path) -> Optional[Path]:
    """Send a file to the Trash, where Finder's Put Back can restore it. Never deletes.
    Returns where the file ended up in the Trash when that can be confirmed, so it can
    be restored from inside the app; otherwise None (Put Back in Finder still works)."""
    st = path.stat()
    test_dir = os.environ.get("SORTX_TEST_TRASH")  # used by tests instead of the real Trash
    if test_dir:
        Path(test_dir).mkdir(parents=True, exist_ok=True)
        target = unique_path(Path(test_dir) / path.name, set())
        shutil.move(str(path), str(target))
        return target
    if sys.platform != "darwin":
        raise OSError("Moving to the Trash is only supported on macOS.")
    if Path("/usr/bin/trash").exists():  # built into macOS 15 and later
        proc = subprocess.run(["/usr/bin/trash", str(path)], capture_output=True, text=True)
    else:
        proc = subprocess.run(
            ["osascript", "-e", "on run argv", "-e",
             'tell application "Finder" to delete (POSIX file (item 1 of argv) as alias)',
             "-e", "end run", str(path)], capture_output=True, text=True)
    if proc.returncode != 0 or path.exists():
        raise OSError(f"Could not move {path.name} to the Trash.")
    # Same disk = same file id, so we can be sure it's our file. (The Trash folder itself
    # can't be listed, but a known name inside it can be checked.)
    for d in _trash_dirs_for(path):
        candidate = d / path.name
        try:
            cst = candidate.stat()
            if cst.st_ino == st.st_ino and cst.st_dev == st.st_dev:
                return candidate
        except OSError:
            pass
    return None


def restore_from_trash(trashed: Path, original: Path) -> Path:
    """Move a file we sent to the Trash back to where it was. Returns where it went."""
    trash_roots = [Path.home() / ".Trash"]
    test_dir = os.environ.get("SORTX_TEST_TRASH")
    if test_dir:
        trash_roots.append(Path(test_dir))
    in_trash = any(is_inside(trashed, r) for r in trash_roots) or "/.Trashes/" in str(trashed)
    if not in_trash:
        raise ValueError("That file isn't in the Trash.")
    if not trashed.exists():
        raise FileNotFoundError(f"{original.name} is no longer in the Trash (it may have been emptied).")
    original.parent.mkdir(parents=True, exist_ok=True)
    target = unique_path(original, set())
    shutil.move(str(trashed), str(target))
    return target


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #

@dataclass
class Options:
    source: Path
    dest: Optional[Path] = None          # default: <source>/Sorted
    copy: bool = False                   # copy instead of move
    by_date: bool = False                # add Year/Month subfolders
    dedupe: bool = True                  # look for duplicate files
    recursive: bool = True               # include subfolders
    include_hidden: bool = False
    remove_empty_dirs: bool = True       # after moving, remove folders left empty
    dry_run: bool = False
    keep_dupes: set = field(default_factory=set)    # duplicate paths to sort like normal files
    remove_dupes: set = field(default_factory=set)  # duplicate paths to send to the Trash


@dataclass
class Result:
    planned: List[dict] = field(default_factory=list)   # {"src","dst","kind","category"}
    duplicate_groups: List[List[Path]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    bytes_saved: int = 0
    log_file: Optional[Path] = None
    report_file: Optional[Path] = None
    cancelled: bool = False      # the user stopped the run part-way
    planned_total: int = 0       # how many files were planned before stopping

    def summary(self) -> str:
        counts: Dict[str, int] = defaultdict(int)
        dupes = removed = 0
        for item in self.planned:
            if item["kind"] == "duplicate":
                dupes += 1
            elif item["kind"] == "remove":
                removed += 1
            else:
                counts[item["category"]] += 1
        lines = ["", "Summary", "-------"]
        for cat in sorted(counts):
            lines.append(f"  {cat:<14} {counts[cat]:>6} file(s)")
        lines.append(f"  {'Duplicates':<14} {dupes:>6} file(s)  "
                     f"({human_size(self.bytes_saved)} reclaimable)")
        if removed:
            lines.append(f"  {'To Trash':<14} {removed:>6} file(s)")
        if self.errors:
            lines.append(f"  {'Errors':<14} {len(self.errors):>6}")
        if self.cancelled:
            lines.append(f"\nStopped early: {len(self.planned)} of {self.planned_total} file(s) done.")
        if self.log_file:
            lines.append(f"\nUndo log: {self.log_file}")
        if self.report_file:
            lines.append(f"Duplicate report: {self.report_file}")
        return "\n".join(lines)


Logger = Callable[[str], None]
Progress = Callable[[str, int, int], None]  # (phase, done, total)


class Cancelled(Exception):
    """Raised when the user stops a scan or preview."""


class FileSorter:
    def __init__(self, opts: Options, log: Logger = print, progress: Optional[Progress] = None,
                 stop_event: Optional[threading.Event] = None):
        self.opts = opts
        self.log = log
        self.progress = progress or (lambda phase, done, total: None)
        self.stop_event = stop_event or threading.Event()
        self.source = opts.source.expanduser().resolve()
        self.dest = (opts.dest.expanduser().resolve() if opts.dest
                     else self.source / "Sorted")

    # -- stopping ----------------------------------------------------------- #
    def stop(self) -> None:
        """Ask a running scan/sort to stop. Sorting stops after the current file."""
        self.stop_event.set()

    def _check_stop(self) -> None:
        if self.stop_event.is_set():
            raise Cancelled()

    # -- scanning ----------------------------------------------------------- #
    def _walk(self, root: Path, skip: Optional[Path] = None) -> List[Path]:
        files: List[Path] = []
        if not root.exists():
            return files
        for dirpath, dirnames, filenames in os.walk(root):
            self._check_stop()
            d = Path(dirpath)
            # never descend into the output folder, hidden folders, or app bundles
            dirnames[:] = [
                n for n in dirnames
                if not (skip and is_inside(d / n, skip))
                and (self.opts.include_hidden or not n.startswith("."))
                and not n.endswith((".app", ".photoslibrary", ".bundle"))
            ]
            if not self.opts.recursive and d != root:
                dirnames[:] = []
                continue
            for name in filenames:
                if name in IGNORED_NAMES or name.startswith(LOG_PREFIX):
                    continue
                if not self.opts.include_hidden and name.startswith("."):
                    continue
                p = d / name
                if p.is_symlink() or not p.is_file():
                    continue
                files.append(p)
            if not self.opts.recursive:
                dirnames[:] = []
        return files

    # -- duplicate detection ----------------------------------------------- #
    def _find_duplicates(self, new_files: List[Path], existing: List[Path]) -> Dict[Path, Path]:
        """Return {duplicate_path: kept_path}. Files already in dest are preferred keepers."""
        existing_set = set(existing)
        by_size: Dict[int, List[Path]] = defaultdict(list)
        for p in new_files + existing:
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size > 0:  # empty files are never treated as duplicates
                by_size[size].append(p)

        candidates = [g for g in by_size.values() if len(g) > 1]
        total = sum(len(g) for g in candidates)
        self.log(f"Checking {total} file(s) with matching sizes for duplicates...")

        def group_by(paths: List[Path], fn) -> List[List[Path]]:
            buckets: Dict[str, List[Path]] = defaultdict(list)
            for p in paths:
                try:
                    buckets[fn(p)].append(p)
                except OSError as e:
                    self.result.errors.append(f"Could not read {p}: {e}")
            return [b for b in buckets.values() if len(b) > 1]

        dup_map: Dict[Path, Path] = {}
        checked = 0
        self.progress("Checking for duplicates", 0, total)
        hash_all = lambda p: full_hash(p, check=self._check_stop)  # noqa: E731
        for group in candidates:
            self._check_stop()
            checked += len(group)
            self.progress("Checking for duplicates", checked, total)
            for qgroup in group_by(group, quick_hash):
                for fgroup in group_by(qgroup, hash_all):
                    keeper = min(fgroup, key=lambda p: self._keeper_rank(p, existing_set))
                    others = [p for p in fgroup if p != keeper and p not in existing_set]
                    if not others:
                        continue
                    self.result.duplicate_groups.append([keeper] + others)
                    for p in others:
                        dup_map[p] = keeper
                        self.result.bytes_saved += p.stat().st_size
        return dup_map

    @staticmethod
    def _keeper_rank(p: Path, existing: set):
        """Lower sorts first = better original to keep."""
        st = p.stat()
        return (
            0 if p in existing else 1,                  # already sorted wins
            1 if COPY_NAME_RE.search(p.stem) else 0,    # avoid "photo (1).jpg"
            st.st_mtime,                                # older file wins
            len(str(p)),                                # shorter path wins
        )

    # -- planning ----------------------------------------------------------- #
    def _target_for(self, p: Path, category: str, base: Path) -> Path:
        folder = base / category
        if self.opts.by_date:
            try:
                d = file_date(p)
                folder = folder / f"{d:%Y}" / f"{d:%m-%B}"
            except Exception:
                folder = folder / "Unknown date"
        return folder / p.name

    def plan(self) -> Result:
        self.result = Result()
        if not self.source.is_dir():
            raise NotADirectoryError(f"Not a folder: {self.source}")

        self.log(f"Scanning {self.source} ...")
        self.progress("Scanning", 0, 0)
        files = self._walk(self.source, skip=self.dest)
        self.log(f"Found {len(files)} file(s) to sort.")

        dup_map: Dict[Path, Path] = {}
        existing_set: set = set()
        if self.opts.dedupe:
            existing = [p for p in self._walk(self.dest)
                        if not is_inside(p, self.dest / DUPLICATES_DIR)]
            if existing:
                self.log(f"Also comparing against {len(existing)} already-sorted file(s).")
            dup_map = self._find_duplicates(files, existing)
            existing_set = set(existing)
            self.log(f"Found {len(dup_map)} duplicate file(s).")

        taken: set = set()
        already = 0
        for p in sorted(files):
            self._check_stop()
            category = category_for(p)
            if self.opts.copy and dup_map.get(p) in existing_set:
                # copy mode: this file's content is already in the sorted folder
                # (e.g. from an earlier or stopped run), so there's nothing to copy
                already += 1
                continue
            chosen = None
            if p in dup_map and str(p) in self.opts.remove_dupes:
                chosen, kind, target = "remove", "remove", None
            elif p in dup_map and str(p) in self.opts.keep_dupes:
                chosen, kind = "keep", "file"   # keep both: sort it like any other file
                target = self._target_for(p, category, self.dest)
            elif p in dup_map:
                target = self._target_for(p, category, self.dest / DUPLICATES_DIR)
                kind = "duplicate"
            else:
                target = self._target_for(p, category, self.dest)
                kind = "file"
            if target is not None:
                if target == p:
                    continue
                target = unique_path(target, taken)
                taken.add(target)
            self.result.planned.append({
                "src": str(p), "dst": str(target) if target else None, "kind": kind,
                "category": category, "size": p.stat().st_size,
                "original": str(dup_map[p]) if p in dup_map else None,
                "chosen": chosen,
            })
        if already:
            self.log(f"Skipped {already} file(s) already copied to the sorted folder.")
            # these aren't duplicates to set aside, so leave them out of the report
            skipped = {p for p in files if self.opts.copy and dup_map.get(p) in existing_set}
            self.result.duplicate_groups = [
                [g[0]] + [m for m in g[1:] if m not in skipped] for g in self.result.duplicate_groups]
            self.result.duplicate_groups = [g for g in self.result.duplicate_groups if len(g) > 1]
        self.result.bytes_saved = sum(i["size"] for i in self.result.planned
                                      if i["kind"] in ("duplicate", "remove"))
        return self.result

    # -- execution ---------------------------------------------------------- #
    def run(self) -> Result:
        result = self.plan()
        action = "copy" if self.opts.copy else "move"

        if self.opts.dry_run:
            self.log("\nDRY RUN - nothing will be changed.\n")
            for item in result.planned:
                tag = {"duplicate": "DUP ", "remove": "DEL "}.get(item["kind"], "    ")
                to = self._rel(item["dst"]) if item["dst"] else "Trash"
                self.log(f"{tag}{self._rel(item['src'])}  ->  {to}")
            self.log(result.summary())
            return result

        if not result.planned:
            self.log("Nothing to do - the folder is already sorted.")
            return result

        self.dest.mkdir(parents=True, exist_ok=True)
        base_stamp = stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        n = 1
        while any(self.dest.glob(f"{LOG_PREFIX}{stamp}.*json")):  # two runs in the same second
            n += 1
            stamp = f"{base_stamp}_{n:02d}"
        log_path = self.dest / f"{LOG_PREFIX}{stamp}.json"
        done: List[dict] = []

        verb_ing = "Copying" if self.opts.copy else "Moving"
        self.log(f"\n{verb_ing} {len(result.planned)} file(s)...")
        completed: List[dict] = []
        try:
            for i, item in enumerate(result.planned, 1):
                if self.stop_event.is_set():
                    result.cancelled = True
                    self.log(f"\nStopped by request after {len(done)} of {len(result.planned)} file(s).")
                    break
                src = Path(item["src"])
                if item["kind"] == "remove":
                    try:
                        where = move_to_trash(src)
                        done.append({"src": str(src), "dst": None, "action": "trash",
                                     "trashed_to": str(where) if where else None})
                        completed.append(item)
                    except Exception as e:
                        result.errors.append(f"{src}: {e}")
                        self.log(f"  ! {src.name}: {e}")
                    self.progress(f"{verb_ing} files", i, len(result.planned))
                    continue
                dst = Path(item["dst"])
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if dst.exists():  # appeared since planning
                        dst = unique_path(dst, set())
                    if self.opts.copy:
                        shutil.copy2(src, dst)
                    else:
                        shutil.move(str(src), str(dst))
                    done.append({"src": str(src), "dst": str(dst), "action": action})
                    completed.append(dict(item, dst=str(dst)))
                except Exception as e:
                    result.errors.append(f"{src}: {e}")
                    self.log(f"  ! {src.name}: {e}")
                self.progress(f"{verb_ing} files", i, len(result.planned))
                if i % 200 == 0:
                    self.log(f"  {i}/{len(result.planned)}")
        finally:
            # always write the log, even if interrupted, so undo still works
            with log_path.open("w") as f:
                json.dump({"created": stamp, "source": str(self.source),
                           "dest": str(self.dest), "operations": done}, f, indent=2)
            result.log_file = log_path

        result.planned_total = len(result.planned)
        if result.cancelled:
            # only report what actually happened
            result.planned = completed
            handled = {i["src"] for i in completed if i["original"]}
            result.duplicate_groups = [g for g in result.duplicate_groups
                                       if any(str(p) in handled for p in g[1:])]
            result.bytes_saved = sum(i["size"] for i in completed if i["kind"] in ("duplicate", "remove"))

        if result.duplicate_groups:
            result.report_file = self._write_dup_report(result, stamp)

        if not self.opts.copy and self.opts.remove_empty_dirs:
            self._remove_empty_dirs(self.source)

        self.log(result.summary())
        return result

    def _write_dup_report(self, result: Result, stamp: str) -> Path:
        moved = {i["src"]: i["dst"] for i in result.planned}
        kinds = {i["src"]: i["kind"] for i in result.planned}
        status_for = {"duplicate": "duplicate (set aside)", "remove": "duplicate (moved to Trash)",
                      "file": "duplicate (kept and sorted)"}
        report = self.dest / DUPLICATES_DIR / f"duplicates_report_{stamp}.csv"
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["group", "status", "original_location", "now_at", "size"])
            for n, group in enumerate(result.duplicate_groups, 1):
                for j, p in enumerate(group):
                    to = moved.get(str(p), str(p))
                    now_at = Path(to) if to else None
                    size = human_size(now_at.stat().st_size) if now_at and now_at.exists() else ""
                    status = "original (kept)" if j == 0 else status_for.get(kinds.get(str(p)), "duplicate")
                    w.writerow([n, status, str(p), str(now_at) if now_at else "Trash", size])
        return report

    def _remove_empty_dirs(self, root: Path) -> None:
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            d = Path(dirpath)
            if d == root or is_inside(d, self.dest):
                continue
            try:
                leftovers = [n for n in os.listdir(d) if n != ".DS_Store"]
                if not leftovers:
                    (d / ".DS_Store").unlink(missing_ok=True)
                    d.rmdir()
            except OSError:
                pass

    def _rel(self, p: str) -> str:
        try:
            return str(Path(p).relative_to(self.source))
        except ValueError:
            return p


# --------------------------------------------------------------------------- #
# Undo
# --------------------------------------------------------------------------- #

def find_latest_log(dest: Path) -> Optional[Path]:
    logs = sorted(p for p in dest.glob(f"{LOG_PREFIX}*.json")
                  if not p.name.endswith(".undone.json"))
    return logs[-1] if logs else None


def undo(log_path: Path, log: Logger = print) -> int:
    """Reverse a run: moved files go back, copied files are removed."""
    data = json.loads(log_path.read_text())
    ops = data.get("operations", [])
    restored = trashed = 0
    for op in reversed(ops):
        if op["action"] == "trash":
            where = op.get("trashed_to")
            if where and Path(where).exists() and not Path(op["src"]).exists():
                try:
                    restore_from_trash(Path(where), Path(op["src"]))
                    restored += 1
                    continue
                except Exception as e:
                    log(f"  ! could not restore {Path(op['src']).name} from the Trash: {e}")
            trashed += 1
            continue
        src, dst = Path(op["src"]), Path(op["dst"])
        if not dst.exists():
            log(f"  skip (missing): {dst}")
            continue
        try:
            if op["action"] == "move":
                if src.exists():
                    log(f"  skip (something already at original spot): {src}")
                    continue
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(src))
            else:  # copy: the original is untouched, remove the copy
                dst.unlink()
            restored += 1
        except Exception as e:
            log(f"  ! {dst}: {e}")

    # remove this run's duplicate report, then tidy up now-empty folders
    dest = Path(data.get("dest", log_path.parent))
    report = dest / DUPLICATES_DIR / f"duplicates_report_{data.get('created', '')}.csv"
    if report.exists():
        report.unlink()
    for dirpath, _, _ in os.walk(dest, topdown=False):
        d = Path(dirpath)
        try:
            if d != dest and not [n for n in os.listdir(d) if n != ".DS_Store"]:
                (d / ".DS_Store").unlink(missing_ok=True)
                d.rmdir()
        except OSError:
            pass

    log_path.rename(log_path.with_suffix(".undone.json"))
    log(f"Undo finished: {restored} of {len(ops)} file(s) restored.")
    if trashed:
        log(f"{trashed} removed duplicate(s) are in the Trash. To get one back, open the Trash, "
            "select it and choose File > Put Back.")
    return restored



# --------------------------------------------------------------------------- #
# Deciding about duplicates after sorting
# --------------------------------------------------------------------------- #

def resolve_duplicates(paths: List[str], action: str, log_path: Path) -> Dict[str, Optional[str]]:
    """For duplicates already set aside in Sorted/Duplicates:
    'remove' sends them to the Trash, 'keep' moves them into their normal folder.
    Updates the run's undo log so Undo still works. Returns {old path: new path or None}."""
    if action not in ("remove", "keep"):
        raise ValueError("Unknown action.")
    data = json.loads(log_path.read_text())
    dest = Path(data["dest"])
    dup_root = dest / DUPLICATES_DIR
    ops_by_dst = {op.get("dst"): op for op in data.get("operations", [])}
    results: Dict[str, Optional[str]] = {}
    for raw in paths:
        path = Path(raw)
        if not is_inside(path, dup_root) or path == dup_root:
            raise ValueError(f"Not in the Duplicates folder: {path}")
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        op = ops_by_dst.get(str(path))
        if action == "remove":
            where = move_to_trash(path)
            if op and op["action"] == "move":  # the original itself is now in the Trash
                op["action"], op["trashed_from"] = "trash", op["dst"]
                op["dst"], op["trashed_to"] = None, str(where) if where else None
            results[raw] = None
        else:
            target = unique_path(dest / path.relative_to(dup_root), set())
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target))
            if op:
                op["dst"] = str(target)
            results[raw] = str(target)
    log_path.write_text(json.dumps(data, indent=2))
    # tidy up folders inside Duplicates that are now empty
    for dirpath, _, _ in os.walk(dup_root, topdown=False):
        d = Path(dirpath)
        try:
            if d != dup_root and not [n for n in os.listdir(d) if n != ".DS_Store"]:
                (d / ".DS_Store").unlink(missing_ok=True)
                d.rmdir()
        except OSError:
            pass
    return results

# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #

def launch_gui() -> None:
    import queue
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter.scrolledtext import ScrolledText

    root = tk.Tk()
    root.title("Sort X")
    root.geometry("820x600")
    root.minsize(640, 460)

    msgs: "queue.Queue[str]" = queue.Queue()
    src_var, dst_var = tk.StringVar(), tk.StringVar()
    copy_var = tk.BooleanVar(value=False)
    date_var = tk.BooleanVar(value=False)
    dedupe_var = tk.BooleanVar(value=True)
    rec_var = tk.BooleanVar(value=True)
    busy = {"on": False}

    pad = {"padx": 8, "pady": 4}
    frm = ttk.Frame(root, padding=10)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(1, weight=1)

    def pick_src():
        d = filedialog.askdirectory(title="Folder to sort")
        if d:
            src_var.set(d)
            if not dst_var.get():
                dst_var.set(str(Path(d) / "Sorted"))

    def pick_dst():
        d = filedialog.askdirectory(title="Where to put sorted files")
        if d:
            dst_var.set(d)

    ttk.Label(frm, text="Folder to sort:").grid(row=0, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=src_var).grid(row=0, column=1, sticky="ew", **pad)
    ttk.Button(frm, text="Browse...", command=pick_src).grid(row=0, column=2, **pad)

    ttk.Label(frm, text="Put sorted files in:").grid(row=1, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=dst_var).grid(row=1, column=1, sticky="ew", **pad)
    ttk.Button(frm, text="Browse...", command=pick_dst).grid(row=1, column=2, **pad)

    opts = ttk.Frame(frm)
    opts.grid(row=2, column=0, columnspan=3, sticky="w", **pad)
    ttk.Checkbutton(opts, text="Find duplicates", variable=dedupe_var).pack(side="left", padx=6)
    ttk.Checkbutton(opts, text="Include subfolders", variable=rec_var).pack(side="left", padx=6)
    ttk.Checkbutton(opts, text="Year/Month folders", variable=date_var).pack(side="left", padx=6)
    ttk.Checkbutton(opts, text="Copy (keep originals)", variable=copy_var).pack(side="left", padx=6)

    btns = ttk.Frame(frm)
    btns.grid(row=3, column=0, columnspan=3, sticky="w", **pad)

    out = ScrolledText(frm, height=20, font=("Menlo", 11))
    out.grid(row=4, column=0, columnspan=3, sticky="nsew", **pad)
    frm.rowconfigure(4, weight=1)

    status = tk.StringVar(value="Choose a folder, then click Preview.")
    ttk.Label(frm, textvariable=status).grid(row=5, column=0, columnspan=3, sticky="w", **pad)

    def make_opts(dry: bool) -> Optional[Options]:
        if not src_var.get() or not Path(src_var.get()).expanduser().is_dir():
            messagebox.showwarning("No folder", "Please choose a folder to sort.")
            return None
        return Options(
            source=Path(src_var.get()),
            dest=Path(dst_var.get()) if dst_var.get() else None,
            copy=copy_var.get(), by_date=date_var.get(), dedupe=dedupe_var.get(),
            recursive=rec_var.get(), dry_run=dry,
        )

    def worker(fn):
        def wrapped():
            try:
                fn()
            except Exception as e:
                msgs.put(f"\nError: {e}")
            finally:
                msgs.put("__DONE__")
        busy["on"] = True
        status.set("Working...")
        for b in btns.winfo_children():
            b.state(["disabled"])
        threading.Thread(target=wrapped, daemon=True).start()

    def preview():
        o = make_opts(True)
        if o:
            out.delete("1.0", "end")
            worker(lambda: FileSorter(o, msgs.put).run())

    def run():
        o = make_opts(False)
        if not o:
            return
        verb = "copied" if o.copy else "moved"
        if not messagebox.askyesno("Sort files?",
                                   f"Files will be {verb} into:\n{FileSorter(o).dest}\n\n"
                                   "Duplicates go to a 'Duplicates' folder (nothing is deleted).\n"
                                   "You can undo this afterwards. Continue?"):
            return
        out.delete("1.0", "end")
        worker(lambda: FileSorter(o, msgs.put).run())

    def do_undo():
        dest = Path(dst_var.get()) if dst_var.get() else (
            Path(src_var.get()) / "Sorted" if src_var.get() else None)
        log_file = find_latest_log(dest) if dest and dest.exists() else None
        if not log_file:
            log_file = filedialog.askopenfilename(
                title="Choose a sort log to undo", filetypes=[("Sort log", "sort_log_*.json")])
            if not log_file:
                return
            log_file = Path(log_file)
        if messagebox.askyesno("Undo?", f"Undo the run recorded in:\n{log_file.name}?"):
            out.delete("1.0", "end")
            worker(lambda: undo(log_file, msgs.put))

    ttk.Button(btns, text="Preview", command=preview).pack(side="left", padx=4)
    ttk.Button(btns, text="Sort now", command=run).pack(side="left", padx=4)
    ttk.Button(btns, text="Undo last run", command=do_undo).pack(side="left", padx=4)

    def poll():
        try:
            while True:
                m = msgs.get_nowait()
                if m == "__DONE__":
                    busy["on"] = False
                    status.set("Done.")
                    for b in btns.winfo_children():
                        b.state(["!disabled"])
                else:
                    out.insert("end", m + "\n")
                    out.see("end")
        except queue.Empty:
            pass
        root.after(100, poll)

    poll()
    root.mainloop()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Sort X: sort files into category folders and set aside duplicates.")
    ap.add_argument("source", nargs="?", help="folder to sort")
    ap.add_argument("-o", "--dest", help="output folder (default: <source>/Sorted)")
    ap.add_argument("--copy", action="store_true", help="copy files instead of moving them")
    ap.add_argument("--by-date", action="store_true", help="add Year/Month subfolders")
    ap.add_argument("--no-dedupe", action="store_true", help="skip duplicate detection")
    ap.add_argument("--no-recursive", action="store_true", help="ignore subfolders")
    ap.add_argument("--include-hidden", action="store_true", help="include hidden files")
    ap.add_argument("--keep-empty-folders", action="store_true",
                    help="don't remove folders left empty after moving")
    ap.add_argument("-n", "--dry-run", action="store_true", help="preview, change nothing")
    ap.add_argument("--undo", metavar="LOG", nargs="?", const="latest",
                    help="undo a run (pass a log file, or a source folder for its latest log)")
    ap.add_argument("--gui", action="store_true", help="open the window (default with no folder)")
    ap.add_argument("--tk", action="store_true", help="open the older basic window instead")
    args = ap.parse_args(argv)

    if args.tk:
        launch_gui()
        return 0
    if args.gui or (args.source is None and args.undo is None):
        import gui_server  # modern window, lives next to this file
        gui_server.serve()
        return 0

    if args.undo:
        if args.undo != "latest":
            log_path = Path(args.undo).expanduser()
        else:
            if not args.source:
                ap.error("--undo needs a log file, or a source folder before it")
            dest = Path(args.dest).expanduser() if args.dest else Path(args.source).expanduser() / "Sorted"
            log_path = find_latest_log(dest)
        if log_path and log_path.is_dir():
            log_path = find_latest_log(log_path) or find_latest_log(log_path / "Sorted")
        if not log_path or not log_path.exists():
            print("No sort log found to undo.", file=sys.stderr)
            return 1
        undo(log_path)
        return 0

    opts = Options(
        source=Path(args.source),
        dest=Path(args.dest) if args.dest else None,
        copy=args.copy,
        by_date=args.by_date,
        dedupe=not args.no_dedupe,
        recursive=not args.no_recursive,
        include_hidden=args.include_hidden,
        remove_empty_dirs=not args.keep_empty_folders,
        dry_run=args.dry_run,
    )
    try:
        result = FileSorter(opts).run()
    except (NotADirectoryError, PermissionError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
