#!/usr/bin/env python3
"""Browser treemap for directories named in a JSON config.

The desktop disktree app is a native window. This serves the same job over
HTTP: which directories fill a tree, without crossing onto another
filesystem and without following symlinks. Sizes are allocated blocks
(st_blocks * 512), and a hard link is counted once per tree.

Which directories to measure, and which to skip, come from the file in
DISKTREE_CONFIG. With no file, the only directory is /. Nothing here names
a machine's disks.
"""

from __future__ import annotations

import heapq
import json
import os
import sqlite3
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("DISKTREE_PORT", "8766"))
STATE = os.environ.get("DISKTREE_STATE", "/var/lib/disktree-web")
TOP_N = 24


def absolute_paths(items: object, what: str) -> list[str]:
    if items is None:
        return []
    if not isinstance(items, list):
        raise SystemExit(f"{what} must be a list of absolute paths")
    out: list[str] = []
    for item in items:
        if not isinstance(item, str) or "\x00" in item or not item.startswith("/"):
            raise SystemExit(f"{what} entries must be absolute paths, got {item!r}")
        out.append(os.path.normpath(item))
    return out


def load_settings() -> tuple[list[str], list[str], list[str]]:
    """Paths to measure, paths to skip, and paths to walk slowly.

    DISKTREE_CONFIG is a JSON object written by the NixOS module:
    {"paths": ["/"], "exclude": [], "pacedPaths": []}.
    """
    config_path = os.environ.get("DISKTREE_CONFIG")
    data: dict = {}
    if config_path:
        with open(config_path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if not isinstance(loaded, dict):
            raise SystemExit("DISKTREE_CONFIG must be a JSON object")
        data = loaded
    paths = absolute_paths(data.get("paths", ["/"]), "paths") or ["/"]
    exclude = absolute_paths(data.get("exclude", []), "exclude")
    paced = absolute_paths(data.get("pacedPaths", []), "pacedPaths")
    return paths, exclude, paced


MOUNTS, EXCLUDE, PACED = load_settings()

db_lock = threading.Lock()
queue_lock = threading.Lock()
wake = threading.Event()
queues: dict[str, list[str]] = {m: [] for m in MOUNTS}
focus: list[tuple[str, str]] = []
boost: list[tuple[str, str]] = []
rr = 0
current_path: str | None = None
seen_inodes: dict[str, set[tuple[int, int]]] = {m: set() for m in MOUNTS}
stop = threading.Event()


def is_under(path: str, roots: list[str]) -> bool:
    return any(path == root or path.startswith(root + os.sep) for root in roots)


def is_excluded(mount: str, path: str) -> bool:
    # The directory being measured is never skipped because of an exclude
    # entry that is the directory itself. Children still are.
    if path == mount:
        return False
    return is_under(path, EXCLUDE)


def owning_mount(path: str) -> str | None:
    match: str | None = None
    for mount in MOUNTS:
        if path == mount or path.startswith(mount + os.sep) or (mount == "/" and path.startswith("/")):
            if match is None or len(mount) > len(match):
                match = mount
    return match


def label_of(mount: str) -> str:
    if mount == "/":
        return "root"
    return mount.rstrip("/").rsplit("/", 1)[-1] or mount


def parent_of(mount: str, path: str) -> str | None:
    if path == mount:
        return None
    parent = os.path.dirname(path)
    return parent or None


def inside(mount: str, path: str) -> bool:
    path = os.path.normpath(path)
    mount = os.path.normpath(mount)
    if mount not in MOUNTS or path != os.path.normpath(path):
        return False
    if "\x00" in path:
        return False
    if mount == "/":
        return path.startswith("/")
    return path == mount or path.startswith(mount + os.sep)


def connect() -> sqlite3.Connection:
    os.makedirs(STATE, exist_ok=True)
    conn = sqlite3.connect(os.path.join(STATE, "tree.sqlite"), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS node (
            mount TEXT NOT NULL,
            path TEXT NOT NULL,
            parent TEXT,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            bytes_here INTEGER NOT NULL DEFAULT 0,
            nfiles INTEGER NOT NULL DEFAULT 0,
            nfiles_rec INTEGER NOT NULL DEFAULT 0,
            recursive_bytes INTEGER NOT NULL DEFAULT 0,
            pending INTEGER NOT NULL DEFAULT 0,
            listed INTEGER NOT NULL DEFAULT 0,
            complete INTEGER NOT NULL DEFAULT 0,
            top_files TEXT NOT NULL DEFAULT '[]',
            error TEXT,
            PRIMARY KEY (mount, path)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS node_parent ON node(mount, parent)")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    # v2 ignores other filesystems inside a walk. v1 had counted their df
    # usage inside the parent, which made `/` look like the whole array.
    fingerprint = json.dumps({"paths": MOUNTS, "exclude": EXCLUDE}, sort_keys=True)
    saved = conn.execute("SELECT value FROM meta WHERE key = 'fingerprint'").fetchone()
    if row is None or row["value"] != "2" or (saved is not None and saved["value"] != fingerprint):
        conn.execute("DELETE FROM node")
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema', '2')")
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('fingerprint', ?)", (fingerprint,))
    conn.commit()
    return conn


CONN = connect()


def ensure_root(mount: str) -> None:
    name = label_of(mount) if mount != "/" else "/"
    CONN.execute(
        """
        INSERT OR IGNORE INTO node
            (mount, path, parent, name, kind, listed, complete)
        VALUES (?, ?, NULL, ?, 'dir', 0, 0)
        """,
        (mount, mount, name),
    )


def add_delta(mount: str, path: str, dbytes: int, dfiles: int) -> None:
    while path is not None:
        CONN.execute(
            """
            UPDATE node
               SET recursive_bytes = recursive_bytes + ?,
                   nfiles_rec = nfiles_rec + ?
             WHERE mount = ? AND path = ?
            """,
            (dbytes, dfiles, mount, path),
        )
        row = CONN.execute(
            "SELECT parent FROM node WHERE mount = ? AND path = ?",
            (mount, path),
        ).fetchone()
        path = row["parent"] if row else None


def mark_complete(mount: str, path: str) -> None:
    CONN.execute(
        "UPDATE node SET complete = 1, pending = 0 WHERE mount = ? AND path = ?",
        (mount, path),
    )
    row = CONN.execute(
        "SELECT parent FROM node WHERE mount = ? AND path = ?",
        (mount, path),
    ).fetchone()
    parent = row["parent"] if row else None
    if parent is None:
        return
    CONN.execute(
        "UPDATE node SET pending = pending - 1 WHERE mount = ? AND path = ? AND pending > 0",
        (mount, parent),
    )
    parent_row = CONN.execute(
        "SELECT pending, listed, complete FROM node WHERE mount = ? AND path = ?",
        (mount, parent),
    ).fetchone()
    if parent_row and parent_row["listed"] and parent_row["pending"] <= 0 and not parent_row["complete"]:
        mark_complete(mount, parent)


def statvfs_of(path: str) -> dict | None:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    fr = st.f_frsize or st.f_bsize or 1
    total = st.f_blocks * fr
    used = (st.f_blocks - st.f_bfree) * fr
    avail = st.f_bavail * fr
    return {"total": total, "used": used, "avail": avail}


def file_size(mount: str, st: os.stat_result) -> int:
    size = st.st_blocks * 512
    if st.st_nlink > 1:
        key = (st.st_dev, st.st_ino)
        seen = seen_inodes[mount]
        if key in seen:
            return 0
        seen.add(key)
    return size


def consider_top(heap: list, name: str, size: int) -> None:
    if size <= 0:
        return
    item = (size, name)
    if len(heap) < TOP_N:
        heapq.heappush(heap, item)
    elif size > heap[0][0]:
        heapq.heapreplace(heap, item)


def enqueue(mount: str, path: str) -> None:
    with queue_lock:
        if any(mount == bmount and (path == bpath or path.startswith(bpath + os.sep)) for bmount, bpath in boost):
            focus.append((mount, path))
        else:
            queues[mount].append(path)
    wake.set()


def take_job() -> tuple[str, str] | None:
    global rr
    with queue_lock:
        while focus:
            mount, path = focus.pop(0)
            return mount, path
        for _ in range(len(MOUNTS)):
            mount = MOUNTS[rr % len(MOUNTS)]
            rr += 1
            if queues[mount]:
                return mount, queues[mount].pop(0)
    return None


def listed(mount: str, path: str) -> bool:
    row = CONN.execute(
        "SELECT listed FROM node WHERE mount = ? AND path = ?",
        (mount, path),
    ).fetchone()
    return bool(row and row["listed"])


def scan_dir(mount: str, path: str, mount_dev: int) -> None:
    global current_path
    with db_lock:
        if listed(mount, path):
            return
        current_path = path
    try:
        entries = list(os.scandir(path))
        self_stat = os.lstat(path)
    except OSError as exc:
        with db_lock:
            CONN.execute(
                """
                UPDATE node
                   SET listed = 1, complete = 0, error = ?, kind = 'dir'
                 WHERE mount = ? AND path = ?
                """,
                (exc.strerror or str(exc), mount, path),
            )
            mark_complete(mount, path)
            CONN.commit()
            current_path = None
        return

    dir_inode = self_stat.st_blocks * 512
    files_bytes = 0
    nfiles = 0
    heap: list[tuple[int, str]] = []
    subdirs: list[tuple[str, str]] = []
    extras: list[tuple] = []
    extra_bytes = 0

    for ent in entries:
        try:
            if ent.is_symlink():
                st = ent.stat(follow_symlinks=False)
                size = file_size(mount, st)
                files_bytes += size
                nfiles += 1
                consider_top(heap, ent.name, size)
                continue
            if ent.is_dir(follow_symlinks=False):
                st = ent.stat(follow_symlinks=False)
                child = os.path.join(path, ent.name) if path != "/" else "/" + ent.name
                if st.st_dev != mount_dev:
                    # Another filesystem. It is its own entry in `paths` when
                    # someone wants it measured, and counting it here would
                    # hide everything else on this filesystem.
                    continue
                if is_excluded(mount, child):
                    extras.append((child, ent.name, "excluded", 0, "excluded from the scan"))
                    continue
                subdirs.append((child, ent.name))
                continue
            st = ent.stat(follow_symlinks=False)
            size = file_size(mount, st)
            files_bytes += size
            nfiles += 1
            consider_top(heap, ent.name, size)
        except OSError as exc:
            nfiles += 1
            extras.append((os.path.join(path, ent.name) if path != "/" else "/" + ent.name, ent.name, "error", 0, exc.strerror or str(exc)))

    top = sorted(heap, reverse=True)
    top_json = json.dumps(top, ensure_ascii=False)
    children = [(child, name) for child, name in subdirs]

    with db_lock:
        if listed(mount, path):
            current_path = None
            return
        for child, name in children:
            CONN.execute(
                """
                INSERT OR IGNORE INTO node
                    (mount, path, parent, name, kind)
                VALUES (?, ?, ?, ?, 'dir')
                """,
                (mount, child, path, name),
            )
        for child, name, kind, used, err in extras:
            CONN.execute(
                """
                INSERT OR REPLACE INTO node
                    (mount, path, parent, name, kind, recursive_bytes, listed, complete, error)
                VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?)
                """,
                (mount, child, path, name, kind, used, err if kind == "error" else None),
            )
        CONN.execute(
            """
            UPDATE node
               SET bytes_here = ?,
                   nfiles = ?,
                   pending = ?,
                   listed = 1,
                   top_files = ?,
                   error = NULL
             WHERE mount = ? AND path = ?
            """,
            (files_bytes, nfiles, len(children), top_json, mount, path),
        )
        add_delta(mount, path, files_bytes + extra_bytes + dir_inode, nfiles)
        if not children:
            mark_complete(mount, path)
        CONN.commit()
        current_path = None

    for child, _name in children:
        enqueue(mount, child)
    if is_under(path, PACED):
        time.sleep(0.05)


def scanner() -> None:
    devs: dict[str, int | None] = {}
    with db_lock:
        for mount in MOUNTS:
            if not os.path.isdir(mount):
                continue
            try:
                devs[mount] = os.lstat(mount).st_dev
            except OSError:
                devs[mount] = None
                continue
            ensure_root(mount)
            row = CONN.execute(
                "SELECT listed FROM node WHERE mount = ? AND path = ?",
                (mount, mount),
            ).fetchone()
            if not row or not row["listed"]:
                enqueue(mount, mount)
            else:
                pending = CONN.execute(
                    """
                    SELECT path FROM node
                     WHERE mount = ? AND kind = 'dir' AND listed = 0
                     ORDER BY length(path), path
                    """,
                    (mount,),
                ).fetchall()
                for row in pending:
                    enqueue(mount, row["path"])
        CONN.commit()

    while not stop.is_set():
        for mount in MOUNTS:
            if devs.get(mount) is not None or not os.path.isdir(mount):
                continue
            try:
                devs[mount] = os.lstat(mount).st_dev
            except OSError:
                continue
            with db_lock:
                ensure_root(mount)
                CONN.commit()
            enqueue(mount, mount)
        job = take_job()
        if job is None:
            wake.wait(timeout=2)
            wake.clear()
            continue
        mount, path = job
        dev = devs.get(mount)
        if dev is None:
            continue
        try:
            scan_dir(mount, path, dev)
        except Exception as exc:  # keep the walk alive if one directory is hostile
            with db_lock:
                CONN.execute(
                    "UPDATE node SET listed = 1, error = ? WHERE mount = ? AND path = ?",
                    (str(exc), mount, path),
                )
                mark_complete(mount, path)
                CONN.commit()


def mount_status() -> list[dict]:
    out = []
    with db_lock:
        for mount in MOUNTS:
            usage = statvfs_of(mount) if os.path.isdir(mount) else None
            row = CONN.execute(
                "SELECT recursive_bytes, nfiles_rec, complete, listed, error FROM node WHERE mount = ? AND path = ?",
                (mount, mount),
            ).fetchone()
            counts = CONN.execute(
                "SELECT COALESCE(SUM(listed), 0) AS done, COUNT(*) AS known FROM node WHERE mount = ? AND kind = 'dir'",
                (mount,),
            ).fetchone()
            out.append(
                {
                    "mount": mount,
                    "label": label_of(mount),
                    "present": os.path.isdir(mount),
                    "total": usage["total"] if usage else None,
                    "used": usage["used"] if usage else None,
                    "avail": usage["avail"] if usage else None,
                    "measured": row["recursive_bytes"] if row else 0,
                    "files": row["nfiles_rec"] if row else 0,
                    "complete": bool(row and row["complete"]),
                    "listed": bool(row and row["listed"]),
                    "dirs_done": counts["done"] if counts else 0,
                    "dirs_known": counts["known"] if counts else 0,
                    "error": row["error"] if row else None,
                    "current": current_path if current_path and owning_mount(current_path) == mount else None,
                }
            )
    return out


def tree_payload(mount: str, path: str) -> dict | None:
    with db_lock:
        row = CONN.execute(
            "SELECT * FROM node WHERE mount = ? AND path = ?",
            (mount, path),
        ).fetchone()
        if row is None:
            return None
        kids = CONN.execute(
            """
            SELECT name, path, kind, recursive_bytes, nfiles_rec, complete, listed, error
              FROM node
             WHERE mount = ? AND parent = ?
             ORDER BY recursive_bytes DESC, name
            """,
            (mount, path),
        ).fetchall()
    usage = statvfs_of(mount)
    entries = []
    for kid in kids:
        entries.append(
            {
                "name": kid["name"],
                "path": kid["path"],
                "kind": kid["kind"],
                "bytes": kid["recursive_bytes"],
                "files": kid["nfiles_rec"],
                "complete": bool(kid["complete"]),
                "listed": bool(kid["listed"]),
                "error": kid["error"],
            }
        )
    try:
        top = json.loads(row["top_files"] or "[]")
    except json.JSONDecodeError:
        top = []
    shown = 0
    for size, name in top:
        shown += size
        entries.append(
            {
                "name": name,
                "path": os.path.join(path, name) if path != "/" else "/" + name,
                "kind": "file",
                "bytes": size,
                "files": 1,
                "complete": True,
                "listed": True,
                "error": None,
            }
        )
    other = row["bytes_here"] - shown
    other_n = row["nfiles"] - len(top)
    if other > 0 and other_n > 0:
        entries.append(
            {
                "name": f"{other_n} smaller files",
                "path": "",
                "kind": "files",
                "bytes": other,
                "files": other_n,
                "complete": True,
                "listed": True,
                "error": None,
            }
        )
    entries.sort(key=lambda e: e["bytes"], reverse=True)
    crumbs = []
    cursor = path
    while True:
        crumbs.append({"name": os.path.basename(cursor) or label_of(mount), "path": cursor})
        if cursor == mount:
            break
        parent = parent_of(mount, cursor)
        if parent is None or not inside(mount, parent):
            break
        cursor = parent
    crumbs.reverse()
    return {
        "mount": mount,
        "label": label_of(mount),
        "path": path,
        "name": row["name"],
        "bytes": row["recursive_bytes"],
        "bytes_here": row["bytes_here"],
        "files": row["nfiles_rec"],
        "complete": bool(row["complete"]),
        "listed": bool(row["listed"]),
        "error": row["error"],
        "total": usage["total"] if usage else None,
        "used": usage["used"] if usage else None,
        "avail": usage["avail"] if usage else None,
        "crumbs": crumbs,
        "entries": entries,
    }


def focus_path(mount: str, path: str) -> str:
    """Measure `path` next, after any unmeasured parent. A child is never
    walked before its parent has been listed, or the parent's total would
    miss it or count it twice.
    """
    if not inside(mount, path) or not os.path.isdir(path):
        return "not a directory on that mount"
    try:
        st = os.lstat(path)
        root_dev = os.lstat(mount).st_dev
    except OSError as exc:
        return exc.strerror or str(exc)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return "not a directory on that mount"
    if st.st_dev != root_dev:
        return "that path is another filesystem"
    if is_excluded(mount, path):
        return "that directory is excluded"
    chain = []
    cursor: str | None = path
    while cursor is not None:
        chain.append(cursor)
        if cursor == mount:
            break
        cursor = parent_of(mount, cursor)
    chain.reverse()
    first: str | None = None
    with queue_lock:
        if (mount, path) not in boost:
            boost.append((mount, path))
    with db_lock:
        for index, item in enumerate(chain):
            parent = None if index == 0 else chain[index - 1]
            CONN.execute(
                """
                INSERT OR IGNORE INTO node (mount, path, parent, name, kind)
                VALUES (?, ?, ?, ?, 'dir')
                """,
                (mount, item, parent, os.path.basename(item) or label_of(mount)),
            )
            row = CONN.execute(
                "SELECT listed FROM node WHERE mount = ? AND path = ?",
                (mount, item),
            ).fetchone()
            if first is None and row and not row["listed"]:
                first = item
        CONN.commit()
    if first is not None:
        with queue_lock:
            focus.insert(0, (mount, first))
        wake.set()
    return "ok"


def rescan(mount: str) -> None:
    with db_lock:
        CONN.execute("DELETE FROM node WHERE mount = ?", (mount,))
        ensure_root(mount)
        CONN.commit()
    seen_inodes[mount] = set()
    with queue_lock:
        queues[mount].clear()
        boost[:] = [item for item in boost if item[0] != mount]
        focus[:] = [item for item in focus if item[0] != mount]
    enqueue(mount, mount)


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DiskTree</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #14161a;
    --panel: #1c1f26;
    --line: #2c313c;
    --text: #e7e4dc;
    --muted: #9a958a;
    --amber: #e0a45a;
    --gap: 10px;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font: 15px/1.4 "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  }
  header {
    display: flex;
    gap: 12px;
    align-items: stretch;
    padding: 14px 16px 8px;
    flex-wrap: wrap;
  }
  button.mount, button.action {
    background: var(--panel);
    color: var(--text);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 8px 12px;
    font: inherit;
    cursor: pointer;
    text-align: left;
  }
  button.mount.active { border-color: var(--amber); }
  button.mount b { display: block; font-size: 16px; }
  button.mount span, .meta { color: var(--muted); font-family: ui-sans-serif, system-ui, sans-serif; font-size: 12px; }
  .bar { height: 6px; background: #2a2e36; border-radius: 99px; margin-top: 6px; overflow: hidden; }
  .bar i { display: block; height: 100%; background: var(--amber); }
  main { display: grid; grid-template-columns: 1fr 320px; gap: var(--gap); padding: 8px 16px 16px; }
  #map { position: relative; height: calc(100vh - 150px); min-height: 420px; background: #101216; border-radius: 10px; overflow: hidden; }
  .tile {
    position: absolute;
    overflow: hidden;
    border: 1px solid rgba(0,0,0,.45);
    color: #f4f1ea;
    cursor: pointer;
  }
  .tile.incomplete { background-image: repeating-linear-gradient(-45deg, transparent, transparent 6px, rgba(0,0,0,.18) 6px, rgba(0,0,0,.18) 7px); }
  .tile span { display: block; padding: 4px 6px 0; font-family: ui-sans-serif, system-ui, sans-serif; font-size: 12px; white-space: nowrap; text-overflow: ellipsis; overflow: hidden; text-shadow: 0 1px 2px rgba(0,0,0,.7); }
  .tile em { display: block; padding: 0 6px; font-style: normal; font-family: ui-sans-serif, system-ui, sans-serif; font-size: 11px; opacity: .85; }
  aside { background: var(--panel); border-radius: 10px; padding: 12px 14px; overflow: auto; height: calc(100vh - 150px); min-height: 420px; }
  nav { font-family: ui-sans-serif, system-ui, sans-serif; font-size: 13px; margin-bottom: 8px; }
  nav button { background: none; border: 0; color: var(--amber); font: inherit; cursor: pointer; padding: 0; }
  h1 { font-size: 22px; margin: 0 0 4px; font-weight: 600; }
  .actions { display: flex; gap: 8px; margin: 10px 0; }
  table { width: 100%; border-collapse: collapse; font-family: ui-sans-serif, system-ui, sans-serif; font-size: 13px; }
  td { padding: 4px 0; border-top: 1px solid var(--line); vertical-align: baseline; }
  td.num { text-align: right; white-space: nowrap; color: var(--muted); padding-left: 8px; }
  tr.click td:first-child { cursor: pointer; }
  footer { padding: 0 16px 14px; color: var(--muted); font-family: ui-sans-serif, system-ui, sans-serif; font-size: 12px; }
  @media (max-width: 800px) {
    main { grid-template-columns: 1fr; }
    #map, aside { height: auto; min-height: 280px; }
    #map { height: 70vh; }
  }
</style>
</head>
<body>
<header id="mounts"></header>
<main>
  <div id="map"></div>
  <aside id="side"></aside>
</main>
<footer id="foot"></footer>
<script>
const $ = (s) => document.querySelector(s);
let mount = "/";
let path = "/";
let timer = null;

function fmt(n) {
  if (n == null) return "—";
  const u = ["B","KB","MB","GB","TB","PB"];
  let i = 0, x = Number(n);
  while (x >= 1024 && i < u.length - 1) { x /= 1024; i++; }
  return (x >= 10 || i === 0 ? x.toFixed(0) : x.toFixed(1)) + " " + u[i];
}
function pct(n, d) { return d ? Math.round(100 * n / d) : 0; }

function color(entry) {
  if (entry.kind === "mount") return "#3f6b58";
  if (entry.kind === "excluded") return "#3a3d44";
  if (entry.kind === "error") return "#6b3f3f";
  if (entry.kind === "files") return "#3a4150";
  const name = entry.name.toLowerCase();
  const ext = name.includes(".") ? name.slice(name.lastIndexOf(".") + 1) : "";
  const fileColors = {
    mkv:"#3d6f9a", mp4:"#3d6f9a", avi:"#3d6f9a", webm:"#3d6f9a", mov:"#3d6f9a", iso:"#3d7a78",
    mp3:"#6d5b8a", flac:"#6d5b8a", m4a:"#6d5b8a", wav:"#6d5b8a",
    jpg:"#5d7a45", jpeg:"#5d7a45", png:"#5d7a45", gif:"#5d7a45", heic:"#5d7a45",
    zip:"#8a6240", "7z":"#8a6240", rar:"#8a6240", gz:"#8a6240", tar:"#8a6240",
    pdf:"#8a7040"
  };
  if (entry.kind === "file" && fileColors[ext]) return fileColors[ext];
  let h = 0;
  for (const c of name) h = (h * 33 + c.charCodeAt(0)) >>> 0;
  return `hsl(${h % 360} 28% 36%)`;
}

async function loadMounts() {
  const rows = await (await fetch("/api/mounts")).json();
  const box = $("#mounts");
  box.replaceChildren();
  for (const row of rows) {
    const b = document.createElement("button");
    b.className = "mount" + (row.mount === mount ? " active" : "");
    const used = row.used, total = row.total;
    b.innerHTML = "";
    const name = document.createElement("b");
    name.textContent = row.label;
    const sub = document.createElement("span");
    sub.textContent = row.present
      ? `${fmt(used)} used · ${fmt(row.avail)} free`
      : "not mounted";
    const bar = document.createElement("div");
    bar.className = "bar";
    const i = document.createElement("i");
    i.style.width = (total ? pct(used, total) : 0) + "%";
    bar.append(i);
    b.append(name, sub, bar);
    b.onclick = () => { mount = row.mount; path = row.mount; refresh(); };
    box.append(b);
  }
  const scanning = rows.some(r => r.present && !r.complete);
  const foot = $("#foot");
  const bits = rows.filter(r => r.present).map(r => {
    const cur = r.current ? ` · now ${r.current}` : "";
    return `${r.label}: ${r.dirs_done}/${r.dirs_known} folders${r.complete ? " · done" : ""}${cur}`;
  });
  foot.textContent = bits.join("   ");
  if (timer) clearTimeout(timer);
  timer = setTimeout(refresh, scanning ? 2000 : 15000);
}

function layout(entries, w, h) {
  const items = entries.filter(e => e.bytes > 0).map(e => ({...e}));
  const rects = [];
  function worst(row, side) {
    const sum = row.reduce((s, e) => s + e.bytes, 0);
    const max = Math.max(...row.map(e => e.bytes));
    const min = Math.min(...row.map(e => e.bytes));
    return Math.max((side * side * max) / (sum * sum), (sum * sum) / (side * side * min));
  }
  function squarify(rest, x, y, w, h) {
    if (!rest.length || w < 1 || h < 1) return;
    const side = Math.min(w, h);
    let row = [rest[0]];
    let i = 1;
    while (i < rest.length && worst(row, side) >= worst(row.concat([rest[i]]), side)) {
      row.push(rest[i]);
      i++;
    }
    const restSum = rest.reduce((s, e) => s + e.bytes, 0);
    const rowSum = row.reduce((s, e) => s + e.bytes, 0);
    const slice = rest.slice(i);
    if (w >= h) {
      const rw = w * (rowSum / restSum);
      let yoff = y;
      for (const e of row) {
        const rh = h * (e.bytes / rowSum);
        rects.push({...e, x, y: yoff, w: rw, h: rh});
        yoff += rh;
      }
      squarify(slice, x + rw, y, w - rw, h);
    } else {
      const rh = h * (rowSum / restSum);
      let xoff = x;
      for (const e of row) {
        const rw = w * (e.bytes / rowSum);
        rects.push({...e, x: xoff, y, w: rw, h: rh});
        xoff += rw;
      }
      squarify(slice, x, y + rh, w, h - rh);
    }
  }
  if (items.length) squarify(items, 0, 0, w, h);
  return rects;
}

async function refresh() {
  await loadMounts();
  const q = new URLSearchParams({mount, path});
  const res = await fetch("/api/tree?" + q);
  if (!res.ok) {
    path = mount;
    return;
  }
  const data = await res.json();
  path = data.path;
  draw(data);
  side(data);
}

function draw(data) {
  const map = $("#map");
  const w = map.clientWidth, h = map.clientHeight;
  map.replaceChildren();
  const rects = layout(data.entries, w, h);
  for (const r of rects) {
    const d = document.createElement("div");
    d.className = "tile" + (r.complete ? "" : " incomplete");
    d.style.left = r.x + "px";
    d.style.top = r.y + "px";
    d.style.width = Math.max(0, r.w - 1) + "px";
    d.style.height = Math.max(0, r.h - 1) + "px";
    d.style.background = color(r);
    if (r.w > 48 && r.h > 28) {
      const s = document.createElement("span");
      s.textContent = r.name;
      d.append(s);
    }
    if (r.w > 70 && r.h > 46) {
      const e = document.createElement("em");
      e.textContent = fmt(r.bytes) + (r.complete ? "" : " so far");
      d.append(e);
    }
    d.title = `${r.name} — ${fmt(r.bytes)}`;
    d.onclick = () => openEntry(r);
    map.append(d);
  }
  if (!rects.length) {
    const p = document.createElement("p");
    p.className = "meta";
    p.style.padding = "16px";
    p.textContent = data.listed ? "This folder is empty." : "Measuring this folder…";
    map.append(p);
  }
}

function openEntry(entry) {
  if (entry.kind === "dir") { path = entry.path; refresh(); }
  else if (entry.kind === "mount") { mount = entry.path; path = entry.path; refresh(); }
}

function side(data) {
  const box = $("#side");
  box.replaceChildren();
  const nav = document.createElement("nav");
  data.crumbs.forEach((c, i) => {
    if (i) nav.append(" / ");
    const b = document.createElement("button");
    b.textContent = c.name;
    b.onclick = () => { path = c.path; refresh(); };
    nav.append(b);
  });
  const h = document.createElement("h1");
  h.textContent = data.crumbs.length ? data.crumbs[data.crumbs.length - 1].name : data.label;
  const meta = document.createElement("div");
  meta.className = "meta";
  const share = data.used ? ` · ${pct(data.bytes, data.used)}% of used space` : "";
  meta.textContent = `${fmt(data.bytes)}${data.complete ? "" : " measured so far"}${share} · ${data.files.toLocaleString()} files`;
  if (data.avail != null) {
    const free = document.createElement("div");
    free.className = "meta";
    free.textContent = `${fmt(data.avail)} free on ${data.label} · ${fmt(data.used)} used of ${fmt(data.total)}`;
    meta.append(free);
  }
  const actions = document.createElement("div");
  actions.className = "actions";
  const next = document.createElement("button");
  next.className = "action";
  next.textContent = "Measure this next";
  next.onclick = async () => {
    await fetch("/api/focus", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({mount, path})});
    refresh();
  };
  const again = document.createElement("button");
  again.className = "action";
  again.textContent = "Measure again";
  again.onclick = async () => {
    await fetch("/api/rescan", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({mount})});
    path = mount;
    refresh();
  };
  actions.append(next, again);
  const table = document.createElement("table");
  for (const e of data.entries) {
    const tr = document.createElement("tr");
    if (e.kind === "dir" || e.kind === "mount") tr.className = "click";
    const name = document.createElement("td");
    name.textContent = e.name + (e.complete ? "" : " …");
    const num = document.createElement("td");
    num.className = "num";
    num.textContent = fmt(e.bytes);
    tr.append(name, num);
    tr.onclick = () => openEntry(e);
    table.append(tr);
  }
  box.append(nav, h, meta, actions, table);
}

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Backspace" || ev.key === "Escape") {
    const parent = path === mount ? mount : path.replace(/\/[^/]+$/, "") || "/";
    if (mount !== "/" && parent.length < mount.length) return;
    path = parent || mount;
    refresh();
  }
});
window.addEventListener("resize", refresh);
refresh();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: object) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 8192:
            raise ValueError("body too large")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode() or "{}")
        if not isinstance(data, dict):
            raise ValueError("expected an object")
        return data

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/mounts":
            self._json(200, mount_status())
            return
        if parsed.path == "/api/tree":
            qs = parse_qs(parsed.query)
            mount = (qs.get("mount") or ["/"])[0]
            path = (qs.get("path") or [mount])[0]
            path = os.path.normpath(path)
            if mount not in MOUNTS or not inside(mount, path):
                self._json(400, {"error": "path is outside the mount"})
                return
            payload = tree_payload(mount, path)
            if payload is None:
                self._json(404, {"error": "not measured yet"})
                return
            self._json(200, payload)
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            data = self._read_json()
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
            return
        mount = data.get("mount")
        path = os.path.normpath(data.get("path") or mount or "")
        if mount not in MOUNTS:
            self._json(400, {"error": "unknown mount"})
            return
        if parsed.path == "/api/focus":
            if not inside(mount, path):
                self._json(400, {"error": "path is outside the mount"})
                return
            self._json(200, {"status": focus_path(mount, path)})
            return
        if parsed.path == "/api/rescan":
            rescan(mount)
            self._json(200, {"status": "ok"})
            return
        self._json(404, {"error": "not found"})


def main() -> None:
    for mount in MOUNTS:
        with db_lock:
            if os.path.isdir(mount):
                ensure_root(mount)
                CONN.commit()
    thread = threading.Thread(target=scanner, name="scanner", daemon=True)
    thread.start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"disktree-web listening on 127.0.0.1:{PORT} mounts={MOUNTS}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
