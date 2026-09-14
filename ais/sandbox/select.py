"""Choosing a sandbox backend, and being loud about a downgrade."""

from __future__ import annotations

from ais.config import Settings
from ais.sandbox.base import SandboxBackend, SandboxUnavailable
from ais.sandbox.docker_backend import DockerSandbox
from ais.sandbox.local_backend import LocalSandbox


def select_backend(settings: Settings) -> tuple[SandboxBackend, str | None]:
    """Return the backend to use and, if it is a downgrade, why.

    ``docker`` is the real boundary and the only backend whose findings mean the
    behaviour was *contained*. ``auto`` prefers it and falls back to the local
    runner only when the daemon is unreachable, returning a warning the caller
    is expected to show the user rather than swallow.
    """
    choice = settings.backend.lower()

    if choice == "local":
        return LocalSandbox(settings), (
            "backend forced to 'local': this is NOT an isolation boundary, "
            "findings describe observed behaviour rather than contained behaviour"
        )

    docker_backend = DockerSandbox(settings)
    if choice == "docker":
        if not docker_backend.available():
            raise SandboxUnavailable(
                "backend 'docker' was requested but the Docker daemon is unreachable. "
                "Start Docker, or run with --backend local to exercise the pipeline "
                "without a real isolation boundary."
            )
        return docker_backend, None

    if choice != "auto":
        raise SandboxUnavailable(f"unknown backend {settings.backend!r}: use docker, local or auto")

    if docker_backend.available():
        return docker_backend, None
    return LocalSandbox(settings), (
        "Docker is unreachable, so the pipeline fell back to the local subprocess "
        "runner. This is NOT an isolation boundary: the edits below were observed, "
        "not contained. Re-run with Docker available for meaningful isolation."
    )
