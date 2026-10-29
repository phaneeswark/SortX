"""
Modern window for Sort X.

Runs a small web server on this Mac only (127.0.0.1) and opens the interface in a
Chrome app window, or in the default browser if Chrome isn't installed. Folder
choosing uses the normal macOS folder dialog. The server stops by itself shortly
after the window is closed.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import file_sorter as fs
import image_dupes as imgd

HERE = Path(__file__).resolve().parent
UI_DIR = HERE / "ui"
PAGES = {"/": "index.html", "/index.html": "index.html", "/x": "x.html", "/image": "image.html"}
STATIC = {"/common.css": ("common.css", "text/css; charset=utf-8"),
          "/common.js": ("common.js", "text/javascript; charset=utf-8")}
IMAGE_ROOTS: set = set()   # folders .Image has scanned; only pictures inside them are served or changed
TOKEN = secrets.token_urlsafe(18)  # stops other web pages from using this server

MAX_ITEMS = 5000    # file rows sent to the page
MAX_GROUPS = 1000   # duplicate groups sent to the page
MAX_PICTURES = 50000  # pictures listed in .Image's "All pictures" tab
IDLE_TIMEOUT = 120  # seconds without a ping before the server quits
SETTINGS_FILE = Path(os.environ.get("SORTX_SETTINGS") or
                     Path.home() / "Library" / "Application Support" / "Sort X" / "settings.json")


# --------------------------------------------------------------------------- #
# Settings (last folder, options, theme) - kept in a file so they survive restarts
# --------------------------------------------------------------------------- #

def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    """Merge these keys into the saved settings (each page saves only its own keys)."""
    if not isinstance(data, dict):
        raise ValueError("Settings must be an object.")
    merged = load_settings()
    merged.update(data)
    text = json.dumps(merged, indent=2)
    if len(text) > 64_000:
        raise ValueError("Settings are too large.")
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(text)


# --------------------------------------------------------------------------- #
# Background job (one at a time)
# --------------------------------------------------------------------------- #

class Job:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.id = 0
        self.state = "idle"        # idle | running | done | cancelled | error
        self.stop_event = threading.Event()
        self.kind = ""
        self.phase = ""
        self.done = 0
        self.total = 0
        self.log: list = []
        self.result: Optional[dict] = None
        self.error = ""

    def start(self, kind: str, fn) -> int:
        with self.lock:
            if self.state == "running":
                raise RuntimeError("Something is already running.")
            self.id += 1
            self.state, self.kind = "running", kind
            self.phase, self.done, self.total = "Starting", 0, 0
            self.log, self.result, self.error = [], None, ""
            self.stop_event = threading.Event()
            job_id = self.id

        def runner():
            try:
                res = fn()
                with self.lock:
                    self.result, self.state = res, "done"
            except fs.Cancelled:
                with self.lock:
                    self.state = "cancelled"
            except Exception as e:
                traceback.print_exc()
                with self.lock:
                    self.error, self.state = str(e), "error"

        threading.Thread(target=runner, daemon=True).start()
        return job_id

    def request_stop(self) -> bool:
        with self.lock:
            if self.state != "running":
                return False
            self.stop_event.set()
            self.phase = "Stopping"
            return True

    def add_log(self, msg: str) -> None:
        with self.lock:
            self.log.append(msg)

    def set_progress(self, phase: str, done: int, total: int) -> None:
        with self.lock:
            self.phase, self.done, self.total = phase, done, total

    def snapshot(self, since: int) -> dict:
        with self.lock:
            return {
                "id": self.id, "state": self.state, "kind": self.kind,
                "phase": self.phase, "done": self.done, "total": self.total,
                "log": self.log[since:], "log_len": len(self.log),
                "result": self.result if self.state == "done" else None,
                "stopping": self.stop_event.is_set(),
                "error": self.error,
            }


JOB = Job()
LAST_PING = {"t": time.time(), "bye": 0.0}


# --------------------------------------------------------------------------- #
# Turning results into something the page can show
# --------------------------------------------------------------------------- #

def rel(path: str, base: Path) -> str:
    try:
        return str(Path(path).relative_to(base))
    except ValueError:
        return path


def dup_state(item: Optional[dict], dry_run: bool) -> str:
    """What is happening to a duplicate: duplicate (set aside), kept, removed, or unsorted."""
    if item is None:
        return "unsorted"            # a stopped run never got to it
    if item["kind"] == "remove":
        return "removed"
    if item["kind"] == "file":
        return "kept"
    return "duplicate"


def result_to_dict(sorter: fs.FileSorter, result: fs.Result, opts: fs.Options) -> dict:
    cats: dict = {}
    dupes = {"count": 0, "bytes": 0}     # set aside in the Duplicates folder
    removed = {"count": 0, "bytes": 0}   # sent to the Trash
    kept = 0                             # duplicates the user chose to keep
    items = []
    for it in result.planned:
        size = it.get("size", 0)
        if it["kind"] == "duplicate":
            dupes["count"] += 1
            dupes["bytes"] += size
        elif it["kind"] == "remove":
            removed["count"] += 1
            removed["bytes"] += size
        else:
            c = cats.setdefault(it["category"], {"count": 0, "bytes": 0})
            c["count"] += 1
            c["bytes"] += size
            kept += 1 if it.get("chosen") == "keep" else 0
        if len(items) < MAX_ITEMS:
            items.append({
                "name": Path(it["src"]).name,
                "from": rel(str(Path(it["src"]).parent), sorter.source),
                "to": rel(it["dst"], sorter.dest) if it["dst"] else "Trash",
                "src": it["src"], "dst": it["dst"],
                "kind": it["kind"], "category": it["category"], "size": size,
                "original": it.get("original"),
                "dup_state": dup_state(it, opts.dry_run) if it.get("original") else None,
            })

    # For each set of identical files: where every copy is now, and where it goes
    # (preview) or went (after sorting).
    by_src = {it["src"]: it for it in result.planned}
    groups = []
    for g in result.duplicate_groups[:MAX_GROUPS]:
        members = []
        for i, p in enumerate(g):
            src = str(p)
            item = by_src.get(src)
            if opts.dry_run:
                here, to = src, (item or {}).get("dst")   # to=None: already in the sorted folder
            else:
                here, to = ((item or {}).get("dst") if item else src), None
            try:
                size = Path(here).stat().st_size if here else (item or {}).get("size", 0)
            except OSError:
                size = 0
            members.append({
                "path": here, "original": src, "to": to, "size": size,
                "state": "original" if i == 0 else dup_state(item, opts.dry_run),
                "category": fs.category_for(p), "already_sorted": fs.is_inside(p, sorter.dest),
            })
        groups.append(members)

    return {
        "dry_run": opts.dry_run, "copy": opts.copy,
        "cancelled": result.cancelled, "planned_total": result.planned_total or len(result.planned),
        "source": str(sorter.source), "dest": str(sorter.dest),
        "categories": cats, "duplicates": dupes, "removed": removed, "kept_dupes": kept,
        "total": len(result.planned), "items": items,
        "items_truncated": len(result.planned) > MAX_ITEMS,
        "groups": groups, "groups_total": len(result.duplicate_groups),
        "errors": result.errors[:200],
        "log_file": str(result.log_file) if result.log_file else None,
        "report_file": str(result.report_file) if result.report_file else None,
    }


def options_from(body: dict, dry_run: bool) -> fs.Options:
    src = (body.get("source") or "").strip()
    if not src:
        raise ValueError("Choose a folder to sort first.")
    source = Path(src).expanduser()
    if not source.is_dir():
        raise ValueError(f"That folder doesn't exist: {source}")
    dest = (body.get("dest") or "").strip()
    return fs.Options(
        source=source,
        dest=Path(dest).expanduser() if dest else None,
        copy=bool(body.get("copy")),
        by_date=bool(body.get("by_date")),
        dedupe=bool(body.get("dedupe", True)),
        recursive=bool(body.get("recursive", True)),
        dry_run=dry_run,
        # per-duplicate choices from the review screen (only applied when sorting)
        keep_dupes=set() if dry_run else {str(x) for x in body.get("keep") or []},
        remove_dupes=set() if dry_run else {str(x) for x in body.get("remove") or []},
    )


def default_dest(source: str) -> str:
    return str(Path(source).expanduser() / "Sorted") if source else ""


# --------------------------------------------------------------------------- #
# macOS helpers
# --------------------------------------------------------------------------- #

def choose_folder(prompt: str, start: str = "") -> Optional[str]:
    script = ["activate"]
    loc = ""
    if start and Path(start).expanduser().is_dir():
        safe_start = str(Path(start).expanduser()).replace("\\", "\\\\").replace('"', '\\"')
        loc = f' default location (POSIX file "{safe_start}")'
    safe_prompt = prompt.replace('"', "'")
    script.append(f'POSIX path of (choose folder with prompt "{safe_prompt}"{loc})')
    args = ["osascript"]
    for line in script:
        args += ["-e", line]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:  # cancelled
        return None
    path = proc.stdout.strip()
    return path.rstrip("/") or "/"


def reveal(path: str, select: bool) -> None:
    p = Path(path)
    if not p.exists():
        # fall back to the nearest folder that still exists
        while not p.exists() and p != p.parent:
            p = p.parent
        select = False
    subprocess.run(["open", "-R", str(p)] if select else ["open", str(p)])


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "SortX"

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    # -- plumbing ---------------------------------------------------------- #
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data).encode(), "application/json")

    def _authorized(self) -> bool:
        if self.headers.get("X-Token") == TOKEN:
            return True
        q = parse_qs(urlparse(self.path).query)
        return q.get("t", [""])[0] == TOKEN

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    # -- routes ------------------------------------------------------------ #
    def do_GET(self):
        url = urlparse(self.path)
        if url.path in PAGES:
            # hand the saved settings to the page up front so the theme applies instantly
            blob = json.dumps(load_settings()).replace("<", "\\u003c")
            html = (UI_DIR / PAGES[url.path]).read_text().replace("/*__SETTINGS__*/null", blob)
            self._send(200, html.encode(), "text/html; charset=utf-8")
            return
        if url.path in STATIC:
            name, ctype = STATIC[url.path]
            self._send(200, (UI_DIR / name).read_bytes(), ctype)
            return
        if not self._authorized():
            self._json({"error": "forbidden"}, 403)
            return
        if url.path == "/api/thumb":
            self._thumb(parse_qs(url.query))
            return
        if url.path == "/api/job":
            since = int(parse_qs(url.query).get("since", ["0"])[0] or 0)
            self._json(JOB.snapshot(since))
            return
        self._json({"error": "not found"}, 404)

    def _thumb(self, q: dict) -> None:
        try:
            path = Path(q.get("path", [""])[0])
            side = max(64, min(2400, int(q.get("s", ["400"])[0])))
            check_picture(path)
            data, ctype = imgd.preview(path, side)
        except (ValueError, OSError) as e:
            self._json({"error": str(e)}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if not self._authorized():
            self._json({"error": "forbidden"}, 403)
            return
        route = urlparse(self.path).path
        body = self._body()
        try:
            handler = ROUTES.get(route)
            if not handler:
                self._json({"error": "not found"}, 404)
                return
            self._json(handler(body))
        except (ValueError, RuntimeError, OSError) as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": f"Unexpected error: {e}"}, 500)


def api_ping(_body):
    LAST_PING["t"] = time.time()
    LAST_PING["bye"] = 0.0
    return {"ok": True, "home": str(Path.home()), "have_pil": fs.HAVE_PIL}


def api_bye(_body):
    LAST_PING["bye"] = time.time()
    return {"ok": True}


def api_pick(body):
    prompt = {"dest": "Choose where to put the sorted files",
              "image": "Choose a folder of pictures"}.get(body.get("which"), "Choose the folder to sort")
    return {"path": choose_folder(prompt, body.get("start", ""))}


def api_start(body):
    mode = body.get("mode")
    if mode in ("preview", "run"):
        opts = options_from(body, dry_run=(mode == "preview"))

        def work():
            sorter = fs.FileSorter(opts, log=JOB.add_log, progress=JOB.set_progress,
                                   stop_event=JOB.stop_event)
            res = sorter.run()
            return result_to_dict(sorter, res, opts)

        return {"job": JOB.start(mode, work)}

    if mode == "undo":
        log_file = Path(body.get("log_file") or "")
        if not log_file.is_file():
            raise ValueError("No sort log found to undo.")

        def work():
            JOB.set_progress("Putting files back", 0, 0)
            data = json.loads(log_file.read_text())
            n = fs.undo(log_file, log=JOB.add_log)
            ops = data.get("operations", [])
            return {"restored": n, "total": len(ops),
                    "trashed": sum(1 for op in ops if op.get("action") == "trash"
                                   and not Path(op["src"]).exists()),
                    "source": data.get("source"), "dest": data.get("dest")}

        return {"job": JOB.start("undo", work)}

    if mode == "image-scan":
        src = Path((body.get("source") or "").strip()).expanduser()
        if not str(src) or not src.is_dir():
            raise ValueError("Choose a folder of pictures first.")
        recursive = bool(body.get("recursive", True))
        similar = bool(body.get("similar", True))
        sensitivity = str(body.get("sensitivity") or "balanced")

        def work():
            res = imgd.ImageScanner(src, recursive=recursive, similar=similar, sensitivity=sensitivity,
                                    progress=JOB.set_progress, log=JOB.add_log,
                                    stop_event=JOB.stop_event).scan()
            res["similar"] = res["similar"][:MAX_GROUPS]
            IMAGE_ROOTS.add(res["source"])
            res["pictures_total"] = len(res["pictures"])
            res["pictures"] = res["pictures"][:MAX_PICTURES]
            return res

        return {"job": JOB.start("image-scan", work)}

    raise ValueError("Unknown action.")


def check_picture(path: Path) -> None:
    """Only pictures inside a folder .Image has scanned may be shown or changed."""
    if not imgd.is_image(path):
        raise ValueError("Not a picture.")
    if not any(fs.is_inside(path, Path(root)) for root in IMAGE_ROOTS):
        raise ValueError("That picture isn't in a scanned folder.")


def api_image_action(body):
    """.Image: remove pictures (to the Trash) or restore them from the Trash."""
    action = body.get("action")
    results, errors = {}, []
    if action == "remove":
        for raw in [str(x) for x in body.get("paths") or []][:5000]:
            path = Path(raw)
            try:
                check_picture(path)
                if not path.is_file():
                    raise FileNotFoundError(f"{path.name} isn't there any more.")
                where = fs.move_to_trash(path)
                results[raw] = str(where) if where else None
            except (ValueError, OSError) as e:
                errors.append(str(e))
        if not results and errors:
            raise ValueError(errors[0])
        return {"results": results, "errors": errors}
    if action == "restore":
        for item in body.get("items") or []:
            original, trashed = Path(str(item.get("path") or "")), item.get("trashed")
            check_picture(original)
            if not trashed:
                raise ValueError("Use Put Back in Finder's Trash to restore this one.")
            results[str(original)] = str(fs.restore_from_trash(Path(trashed), original))
        return {"results": results, "errors": errors}
    raise ValueError("Unknown action.")


def api_dup_action(body):
    """After sorting: remove (to Trash) or keep duplicates that were set aside."""
    if JOB.state == "running":
        raise RuntimeError("Wait for the current task to finish.")
    log_file = Path(body.get("log_file") or "")
    if not log_file.is_file():
        raise ValueError("This sort's log wasn't found, so this can't be changed here.")
    paths = [str(x) for x in body.get("paths") or []]
    if not paths:
        raise ValueError("No files given.")
    return {"results": fs.resolve_duplicates(paths, body.get("action", ""), log_file)}


def api_settings(body):
    save_settings(body.get("settings"))
    return {"ok": True}


def api_stop(_body):
    return {"stopping": JOB.request_stop()}


def api_undo_info(body):
    src = (body.get("source") or "").strip()
    dest = (body.get("dest") or "").strip() or default_dest(src)
    if not dest or not Path(dest).expanduser().is_dir():
        return {"log_file": None}
    log = fs.find_latest_log(Path(dest).expanduser())
    if not log:
        return {"log_file": None}
    try:
        data = json.loads(log.read_text())
        count = len(data.get("operations", []))
        created = data.get("created", "")
    except Exception:
        count, created = 0, ""
    return {"log_file": str(log), "count": count, "created": created}


def api_reveal(body):
    path = body.get("path") or ""
    if not path:
        raise ValueError("No path given.")
    reveal(path, select=bool(body.get("select", True)))
    return {"ok": True}


ROUTES = {
    "/api/ping": api_ping,
    "/api/bye": api_bye,
    "/api/pick": api_pick,
    "/api/start": api_start,
    "/api/stop": api_stop,
    "/api/settings": api_settings,
    "/api/dup-action": api_dup_action,
    "/api/image-action": api_image_action,
    "/api/undo-info": api_undo_info,
    "/api/reveal": api_reveal,
}


# --------------------------------------------------------------------------- #
# Start-up
# --------------------------------------------------------------------------- #

CHROMIUM_APPS = ["Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"]


def open_window(url: str) -> None:
    """Prefer a tab-less Chrome 'app' window; fall back to the default browser."""
    if sys.platform == "darwin":
        for app in CHROMIUM_APPS:
            if Path(f"/Applications/{app}.app").exists():
                subprocess.Popen(["open", "-na", app, "--args", f"--app={url}",
                                  "--window-size=1180,860"])
                return
    webbrowser.open(url)


def watchdog(server: ThreadingHTTPServer) -> None:
    while True:
        time.sleep(2)
        now = time.time()
        if JOB.state == "running":
            continue
        closed = LAST_PING["bye"] and now - LAST_PING["bye"] > 4
        idle = now - LAST_PING["t"] > IDLE_TIMEOUT
        if closed or idle:
            print("Window closed - stopping Sort X.")
            server.shutdown()
            return


def serve(open_browser: bool = True, port: int = 0) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_port}/?t={TOKEN}"
    print("Sort X is running.")
    print(f"If no window opened, paste this into your browser:\n  {url}")
    print("It stops by itself when you close the window.")
    LAST_PING["t"] = time.time()
    threading.Thread(target=watchdog, args=(server,), daemon=True).start()
    if open_browser:
        open_window(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    port = 0
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    serve(open_browser="--no-browser" not in sys.argv, port=port)
