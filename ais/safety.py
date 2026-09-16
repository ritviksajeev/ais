"""Refusing to run an edit the sandbox cannot contain.

The local backend is documented as not being an isolation boundary, but a
document is not a control. Without this, ``--backend local`` happily executes a
scenario whose whole purpose is to delete files outside its workspace, and it
really does delete them -- on Windows ``~/.ssh/known_hosts`` is a real path with
a real file behind it.

So the pipeline matches the risk of a request against the strength of the
containment it has available, and refuses the pairing it cannot survive. This is
deliberately a *pre-flight* check on the proposed content rather than a verdict:
it runs before any sandbox exists, it is not a detection, and nothing it decides
is reported as a finding about the edit.
"""

from __future__ import annotations

from ais.models import EditRequest
from ais.verifier import static_scan

#: Constructs that cause irreversible damage when run without containment.
#: Narrower than the Verifier's full denylist: ``eval`` and friends are worth
#: flagging in a report but do not, by themselves, delete a file or open a
#: socket, and refusing to run them would make the local backend useless for the
#: scenarios it is genuinely fine for.
DESTRUCTIVE_CALLS = frozenset(
    {
        "os.system", "os.popen", "os.execv", "os.execve",
        "os.remove", "os.unlink", "os.rmdir", "os.chmod", "os.setuid",
        "shutil.rmtree",
        "subprocess.run", "subprocess.call", "subprocess.Popen", "subprocess.check_output",
        "socket.socket", "socket.create_connection",
        "urllib.request.urlopen", "requests.get", "requests.post",
        "ctypes.CDLL", "ctypes.cdll.LoadLibrary",
    }
)

DESTRUCTIVE_IMPORTS = frozenset(
    {"socket", "subprocess", "ctypes", "requests", "urllib.request", "http.client",
     "ftplib", "smtplib", "telnetlib", "pty"}
)


def uncontained_risks(request: EditRequest) -> list[str]:
    """Constructs in ``request`` that must not run without a real sandbox.

    Scans the *whole* proposed file rather than only the added lines. The
    Verifier restricts itself to added lines so a finding is about the edit
    rather than its neighbours; here the question is simply what will execute
    if this content is imported, and inherited code executes too.
    """
    risks: list[str] = []
    for path, content in sorted(request.proposed.items()):
        every_line = set(range(1, content.count("\n") + 2))
        for finding in static_scan.scan(path, content, every_line):
            name = finding.construct.removesuffix("()").removeprefix("import ")
            if name in DESTRUCTIVE_CALLS or name in DESTRUCTIVE_IMPORTS:
                risks.append(f"{finding.path}:{finding.line} {finding.construct} — {finding.why}")
    return risks


def refusal_reason(request: EditRequest, isolated: bool) -> str | None:
    """Why this request must not run on the available backend, if it must not."""
    if isolated:
        return None
    risks = uncontained_risks(request)
    if not risks:
        return None
    listed = "\n    ".join(risks)
    return (
        f"refused: {request.request_id} would run uncontained.\n"
        f"  The backend in use is not an isolation boundary, and this edit contains "
        f"operations that damage the host for real when nothing stops them:\n"
        f"    {listed}\n"
        f"  Start Docker and re-run, or pass --allow-uncontained if you genuinely "
        f"intend to execute this on this machine."
    )
