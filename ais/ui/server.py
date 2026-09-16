"""A local web server for the review UI.

Standard library only, on purpose. AiS already asks a user to install Docker;
asking them to install a web framework and run a build step to see what the
tool does would defeat the point of building a UI in the first place. The
whole front end is three static files served from disk.

Two things about the binding are deliberate. It listens on ``127.0.0.1`` and
never on ``0.0.0.0``: this page approves changes to real files, so it must not
be reachable from the network. And every request carries a single-use random
token, generated per run, because "localhost only" still means "every process
on this machine" -- including whatever agent is being mediated.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ais.models import Decision
from ais.ui.state import UiState

STATIC = Path(__file__).resolve().parent / "static"

#: Content types for the files this package ships, pinned rather than asked of
#: the host. ``mimetypes.guess_type`` consults the Windows registry, which
#: reports ``application/javascript`` where Linux reports ``text/javascript``
#: and which a user can break outright -- and a browser refuses a stylesheet or
#: a module served under the wrong type, so a machine-specific answer here is a
#: blank page on somebody else's machine.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
}

#: Refuse a request body larger than this. The only POST bodies are small JSON
#: decisions, so anything bigger is a mistake or an attempt to exhaust memory.
MAX_BODY = 64 * 1024


class ReviewServer:
    """The HTTP surface over a :class:`UiState`."""

    def __init__(self, state: UiState, host: str = "127.0.0.1", port: int = 0) -> None:
        self.state = state
        self.token = secrets.token_urlsafe(24)
        self._httpd = ThreadingHTTPServer((host, port), _make_handler(self))
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None
        self.stopped = threading.Event()

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="ais-ui", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self.stopped.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def _make_handler(server: ReviewServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "AiS"
        sys_version = ""

        # -- plumbing --------------------------------------------------------

        def log_message(self, *_args) -> None:
            """Silence. The terminal is showing the run, not an access log."""

        def _authorised(self, query: dict) -> bool:
            supplied = self.headers.get("X-AiS-Token") or (query.get("t") or [""])[0]
            return secrets.compare_digest(supplied, server.token)

        def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # Nothing here should be cached: the page is a live view of a run.
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

        def _deny(self) -> None:
            self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain; charset=utf-8")

        # -- routes ----------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if not self._authorised(query):
                return self._deny()

            if parsed.path in ("/", "/index.html"):
                return self._static("index.html")
            if parsed.path.startswith("/static/"):
                return self._static(parsed.path[len("/static/") :])
            if parsed.path == "/api/state":
                since = _int(query.get("since", ["0"])[0])
                payload = server.state.snapshot(since=since)
                payload["token"] = server.token
                return self._json(payload)
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if not self._authorised(query):
                return self._deny()

            body = self._body()
            if body is None:
                return self._json({"error": "bad request body"}, HTTPStatus.BAD_REQUEST)

            if parsed.path == "/api/decision":
                return self._decide(body)
            if parsed.path == "/api/abort":
                server.state.abort()
                return self._json({"ok": True})
            if parsed.path == "/api/close":
                server.state.close()
                return self._json({"ok": True})
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")

        # -- handlers --------------------------------------------------------

        def _decide(self, body: dict) -> None:
            request_id = str(body.get("request_id", ""))
            if "approved" not in body or not request_id:
                return self._json({"error": "request_id and approved required"}, HTTPStatus.BAD_REQUEST)
            reason = str(body.get("reason", "")).strip()
            approved = bool(body["approved"])
            if not reason:
                reason = "approved in the review UI" if approved else "rejected in the review UI"
            decision = Decision(
                request_id=request_id,
                approved=approved,
                reviewer="reviewer:web",
                reason=reason[:2000],
            )
            if not server.state.submit_decision(decision):
                # Double-click, a stale tab, or a decision for a request that is
                # no longer the one on screen. Not an error worth failing on.
                return self._json({"ok": False, "reason": "no matching request is awaiting a decision"})
            self._json({"ok": True})

        def _body(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return None
            if length <= 0 or length > MAX_BODY:
                return None
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None

        def _static(self, name: str) -> None:
            target = (STATIC / name).resolve()
            # Confine to the static directory: the URL must not be able to name
            # a file elsewhere on disk.
            if not target.is_file() or STATIC.resolve() not in target.parents:
                return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")
            content_type = CONTENT_TYPES.get(target.suffix.lower())
            if content_type is None:
                guessed = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                content_type = guessed + ("; charset=utf-8" if guessed.startswith("text/") else "")
            body = target.read_bytes()
            if target.name == "index.html":
                # Every request needs the token, including the page's own
                # stylesheet and script -- a browser does not carry the query
                # string of the page over to its subresources.
                body = body.replace(b"{{TOKEN}}", server.token.encode("ascii"))
            self._send(HTTPStatus.OK, body, content_type)

    return Handler


def _int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
