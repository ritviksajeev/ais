"""Runtime observation hook, installed inside the sandbox as ``sitecustomize``.

This file is copied into every sandbox and put on the test process's
``PYTHONPATH``, so CPython imports it before any project code runs. It registers
a PEP 578 audit hook that records the operations AiS cares about -- network
calls, filesystem writes, process spawns, dynamic loading -- to a JSON-lines
trace the Verifier reads afterwards.

Three properties matter here:

*Attempt-level.* Audit events fire when an operation is *requested*, not when it
succeeds. A container with ``--network none`` makes a connect fail, and a file
outside the workspace may not exist -- the hook still sees the attempt, which is
the behaviour a reviewer needs to know about.

*Unremovable.* ``sys.addaudithook`` has no inverse. Once installed, code running
in the sandbox cannot uninstall it from Python.

*Crash-durable.* The trace is line-buffered and appended, so a run killed by a
timeout or an OOM still leaves behind everything observed up to that moment.

It is stdlib-only and imports nothing from ``ais`` -- the sandbox image contains
no AiS package.
"""

import json
import os
import site
import sys
import threading
import time

TRACE_PATH = os.environ.get("AIS_TRACE_PATH")
WORKSPACE = os.path.realpath(os.environ.get("AIS_WORKSPACE", "/workspace"))
ALLOWLIST = tuple(
    os.path.realpath(p)
    for p in os.environ.get("AIS_WRITE_ALLOWLIST", "/workspace:/tmp:/var/tmp").split(":")
    if p
)
MAX_EVENTS = int(os.environ.get("AIS_MAX_TRACE_EVENTS", "5000"))

#: How many times one identical (event, subject) pair is recorded before it is
#: suppressed. A loop hammering the same call is evidence once, noise after that.
REPEAT_CAP = 3

#: How far up the stack to look when attributing an event to its cause.
MAX_STACK_DEPTH = 40

NETWORK_EVENTS = {
    "socket.connect": "network",
    "socket.bind": "network",
    "socket.getaddrinfo": "network",
    "socket.gethostbyname": "network",
    "socket.sendto": "network",
    "urllib.Request": "network",
    "http.client.connect": "network",
    "http.client.send": "network",
    "ftplib.connect": "network",
    "smtplib.connect": "network",
    "smtplib.send": "network",
}

FILESYSTEM_EVENTS = {
    "open": "filesystem",
    "os.remove": "filesystem",
    "os.rename": "filesystem",
    "os.rmdir": "filesystem",
    "os.mkdir": "filesystem",
    "os.chmod": "filesystem",
    "os.chown": "filesystem",
    "os.symlink": "filesystem",
    "os.link": "filesystem",
    "os.truncate": "filesystem",
    "os.listdir": "filesystem",
    "shutil.copyfile": "filesystem",
    "shutil.move": "filesystem",
}

PROCESS_EVENTS = {
    "subprocess.Popen": "process",
    "os.system": "process",
    "os.exec": "process",
    "os.posix_spawn": "process",
    "os.spawn": "process",
    "os.fork": "process",
    "os.forkpty": "process",
    "os.kill": "process",
    "pty.spawn": "process",
}

DYNAMIC_EVENTS = {
    "ctypes.dlopen": "dynamic",
    "ctypes.dlsym": "dynamic",
    "ctypes.call_function": "dynamic",
    "cpython.run_file": "dynamic",
}

#: Modules whose import alone is worth noting next to a diff.
#: Kept narrow on purpose. Modules like ``shutil``, ``base64``, ``codecs`` and
#: ``pickle`` are pulled in by pytest and the standard library on every single
#: run, so watching them produces a finding on every scenario and therefore
#: distinguishes nothing.
WATCHED_IMPORTS = frozenset(
    {
        "socket", "ssl", "ctypes", "subprocess", "requests", "urllib.request",
        "http.client", "httpx", "ftplib", "smtplib", "telnetlib", "paramiko", "pty",
    }
)

CATEGORIES = {}
CATEGORIES.update(NETWORK_EVENTS)
CATEGORIES.update(FILESYSTEM_EVENTS)
CATEGORIES.update(PROCESS_EVENTS)
CATEGORIES.update(DYNAMIC_EVENTS)
CATEGORIES["import"] = "import"

def _site_directories():
    """Every site-packages directory this interpreter knows about.

    The per-user site directory is not under ``sys.prefix``, and pytest scans
    it for plugin entry points at startup. Defined above
    ``BORING_READ_PREFIXES`` because that constant calls it at import time.
    """
    paths = []
    try:
        user_site = site.getusersitepackages()
        if isinstance(user_site, str):
            paths.append(user_site)
        paths.extend(site.getsitepackages())
    except Exception:
        pass
    return [os.path.dirname(p) for p in paths] + paths


#: Reads under these prefixes are the interpreter doing its job, or the sandbox
#: running its own machinery -- not the edit under test doing something
#: interesting. Writes here are still recorded: an edit that writes into the
#: runner's control directory is very much worth seeing.
BORING_READ_PREFIXES = tuple(
    os.path.realpath(p)
    for p in (
        sys.prefix,
        sys.base_prefix,
        getattr(sys, "real_prefix", sys.prefix),
        "/usr/lib/python3",
        "/usr/local/lib/python3",
        os.path.dirname(os.__file__),
        # The per-user site directory is NOT under sys.prefix. pytest scans it
        # for plugin entry points on startup, so leaving it out makes every run
        # look like it read a few dozen files it had no business reading.
        *_site_directories(),
        # Kernel and device pseudo-filesystems: pytest touches /dev/null on
        # every run, and treating that as reconnaissance would bury the signal.
        "/dev",
        "/proc",
        "/sys",
        # The sandbox's own control and output directories, passed in by the
        # runner so this works under any bundle root.
        *(p for p in os.environ.get("AIS_CONTROL_PATHS", "").split(os.pathsep) if p),
    )
)

_state = threading.local()
_start = time.time()
_counts = {}
_seq = [0]

_trace = None
if TRACE_PATH:
    try:
        # Opened before the hook is installed, so the trace file's own open()
        # never appears in the trace it is about to receive.
        _trace = open(TRACE_PATH, "a", buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        _trace = None


def _text(value, limit=200):
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        text = str(value)
    except Exception:
        return "<unrepresentable>"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _resolve(value):
    """Best-effort absolute path for a filesystem audit argument."""
    if isinstance(value, int):
        return None  # an already-open file descriptor, not a path
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        if hasattr(value, "__fspath__"):
            value = os.fspath(value)
        if not isinstance(value, str):
            return None
        return os.path.realpath(value)
    except Exception:
        return None


def _under(path, prefixes):
    return any(path == p or path.startswith(p + os.sep) for p in prefixes)


def _classify_filesystem(event, args):
    """Return ``(path, is_write, keep)`` for a filesystem event."""
    path = _resolve(args[0]) if args else None
    if path is None:
        return None, False, False

    if event == "open":
        mode = _text(args[1]) if len(args) > 1 else "r"
        is_write = any(flag in mode for flag in "wxa+")
    else:
        is_write = event != "os.listdir"

    if not is_write and _under(path, BORING_READ_PREFIXES):
        return path, is_write, False  # interpreter reading its own stdlib
    if not is_write and path.endswith((".pyc", ".pth", ".dist-info")):
        return path, is_write, False
    if not is_write and _under(path, (WORKSPACE,)):
        return path, is_write, False  # reading its own source is the normal case
    return path, is_write, True


def _origin():
    """Attribute an event to the code responsible for it.

    Walks the stack for the nearest frame belonging to the workspace. This is
    what separates "the edit under review opened a socket" from "pytest's
    capture machinery opened /dev/null" -- without it, every run reports the
    test runner's own housekeeping and the interesting events are lost in it.

    Returns ``(label, from_workspace)``. ``from_workspace`` is true when *any*
    frame on the stack is workspace code, so a standard-library helper called by
    the edit is still attributed to the edit.
    """
    try:
        frame = sys._getframe(2)  # skip _origin and _hook
    except ValueError:
        return "?", False

    nearest = None
    depth = 0
    while frame is not None and depth < MAX_STACK_DEPTH:
        filename = frame.f_code.co_filename
        if filename == WORKSPACE or filename.startswith(WORKSPACE + os.sep):
            relative = filename[len(WORKSPACE) + 1 :] or os.path.basename(filename)
            return f"{relative}:{frame.f_lineno}", True
        if nearest is None and not filename.startswith("<"):
            nearest = f"{os.path.basename(filename)}:{frame.f_lineno}"
        frame = frame.f_back
        depth += 1
    return nearest or "?", False


def _subject(event, args, path):
    """The identity used for repeat suppression."""
    if path is not None:
        return path
    return _text(args[0], 80) if args else ""


def _hook(event, args):
    if _trace is None or event not in CATEGORIES:
        return
    if getattr(_state, "busy", False):
        return  # the hook's own I/O must not re-enter the hook
    _state.busy = True
    try:
        category = CATEGORIES[event]
        path = None
        is_write = False

        if category == "filesystem":
            path, is_write, keep = _classify_filesystem(event, args)
            if not keep:
                return
        elif category == "import":
            name = _text(args[0], 80) if args else ""
            if name not in WATCHED_IMPORTS:
                return

        subject = _subject(event, args, path)
        key = (event, subject)
        seen = _counts.get(key, 0) + 1
        _counts[key] = seen
        if seen > REPEAT_CAP or _seq[0] >= MAX_EVENTS:
            return

        origin, from_workspace = _origin()
        _seq[0] += 1
        record = {
            "seq": _seq[0],
            "elapsed_ms": int((time.time() - _start) * 1000),
            "event": event,
            "category": category,
            "args": [_text(a) for a in args[:4]],
            "origin": origin,
            "from_workspace": from_workspace,
        }
        if path is not None:
            record["path"] = path
            record["write"] = is_write
            record["escapes_workspace"] = not _under(path, (WORKSPACE,))
            record["allowed"] = _under(path, ALLOWLIST)
        _trace.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:
        pass  # a tracer that crashes the run it observes is worse than useless
    finally:
        _state.busy = False


if _trace is not None:
    sys.addaudithook(_hook)
    # A marker proving the hook is live. CPython swallows any exception raised
    # while importing sitecustomize, so without this a tracer that failed to
    # load would produce an empty trace -- indistinguishable from a run that
    # genuinely did nothing interesting. The Verifier refuses to call a run
    # clean unless it sees this line.
    _trace.write(
        json.dumps(
            {"seq": 0, "elapsed_ms": 0, "event": "tracer.installed", "category": "meta", "args": []},
            separators=(",", ":"),
        )
        + "\n"
    )
