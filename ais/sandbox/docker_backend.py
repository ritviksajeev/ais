"""The Docker sandbox: the isolation boundary AiS actually relies on.

One container per edit request, with no network, capped CPU, memory and process
count, every Linux capability dropped, and a filesystem that is destroyed when
the container is removed. Nothing is bind-mounted from the host: the bundle is
streamed *into* the container as a tar and the results are streamed back out,
so there is no host path for a sandboxed process to reach through.

AiS deliberately does not implement its own isolation. Docker is the boundary;
this module only configures it and reads the result.
"""

from __future__ import annotations

import io
import tarfile
import time
from pathlib import Path

from ais.config import Settings
from ais.models import SandboxResult
from ais.sandbox.base import Bundle, SandboxBackend, SandboxUnavailable, assemble_result

try:
    import docker
    from docker.errors import APIError, BuildError, DockerException, ImageNotFound, NotFound
except ImportError:  # pragma: no cover - the SDK is a declared dependency
    docker = None
    APIError = BuildError = DockerException = ImageNotFound = NotFound = Exception

#: uid/gid of the unprivileged user baked into the sandbox image.
SANDBOX_UID = 1000
SANDBOX_GID = 1000


class DockerSandbox(SandboxBackend):
    """Runs a bundle inside a disposable, network-less container."""

    name = "docker"
    isolated = True

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = None
        self._image_ready = False

    # -- availability ------------------------------------------------------

    def client(self):
        if self._client is None:
            if docker is None:
                raise SandboxUnavailable("the docker SDK is not installed")
            try:
                self._client = docker.from_env()
                self._client.ping()
            except DockerException as exc:
                raise SandboxUnavailable(f"cannot reach the Docker daemon: {exc}") from exc
        return self._client

    def available(self) -> bool:
        try:
            self.client()
            return True
        except SandboxUnavailable:
            return False

    def ensure_image(self, force: bool = False) -> str:
        """Build the sandbox image if it is not already present."""
        tag = self.settings.sandbox_image
        client = self.client()
        if not force:
            try:
                client.images.get(tag)
                self._image_ready = True
                return tag
            except ImageNotFound:
                pass

        context = self.settings.paths.template_project.parent / "sandbox_image"
        if not (context / "Dockerfile").is_file():
            raise SandboxUnavailable(f"no Dockerfile found in {context}")
        try:
            client.images.build(
                path=str(context),
                tag=tag,
                rm=True,
                buildargs={"BASE_IMAGE": self.settings.base_image},
            )
        except (BuildError, APIError) as exc:
            raise SandboxUnavailable(
                f"could not build the sandbox image {tag!r}: {exc}. "
                f"The base image {self.settings.base_image!r} must be pullable."
            ) from exc
        self._image_ready = True
        return tag

    # -- execution ---------------------------------------------------------

    def run(self, request_id: str, bundle: Bundle, settings: Settings) -> SandboxResult:
        limits = settings.limits
        image = self.ensure_image()
        client = self.client()

        container = client.containers.create(
            image=image,
            command=[settings.python_executable, "/ais/runner.py", "--root", "/"],
            # The whole point: no interface, no DNS, no loopback to the host.
            network_mode="none",
            # Memory and swap set to the same value, so the sandbox cannot buy
            # itself more room by swapping past the cgroup limit.
            mem_limit=f"{limits.memory_mb}m",
            memswap_limit=f"{limits.memory_mb}m",
            nano_cpus=int(limits.cpus * 1_000_000_000),
            pids_limit=limits.pids,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            user=f"{SANDBOX_UID}:{SANDBOX_GID}",
            working_dir="/workspace",
            environment={"PYTHONDONTWRITEBYTECODE": "1"},
            detach=True,
            # No volumes, no binds, no mounts: nothing of the host is reachable.
            network_disabled=True,
        )

        started = time.monotonic()
        timed_out = False
        oom_killed = False
        infrastructure_error = None

        try:
            container.put_archive("/", _tar_bundle(bundle.root))
            container.start()
            try:
                container.wait(timeout=limits.wall_clock_s)
            except Exception:
                # The host-side ceiling. The runner has its own, lower timeout;
                # reaching this one means even the runner stopped responding.
                timed_out = True
                _force_kill(container)

            duration = time.monotonic() - started
            container.reload()
            oom_killed = bool(container.attrs.get("State", {}).get("OOMKilled", False))
            _extract_out(container, bundle.out)
        except (APIError, NotFound, DockerException, OSError) as exc:
            duration = time.monotonic() - started
            infrastructure_error = f"docker backend failure: {type(exc).__name__}: {exc}"
        finally:
            _remove(container)

        return assemble_result(
            request_id,
            self,
            bundle.out,
            settings,
            duration,
            timed_out=timed_out,
            oom_killed=oom_killed,
            infrastructure_error=infrastructure_error,
        )

    def describe(self) -> str:
        return f"docker ({self.settings.sandbox_image}, --network none)"


# --------------------------------------------------------------------------
# tar plumbing
# --------------------------------------------------------------------------


def _tar_bundle(root: Path) -> bytes:
    """Tar the bundle for streaming into the container's root directory.

    Ownership is rewritten to the sandbox's unprivileged uid so the container
    does not have to run as root to write to its own workspace.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for child in sorted(root.iterdir()):
            archive.add(str(child), arcname=child.name, filter=_own_as_sandbox_user)
    buffer.seek(0)
    return buffer.read()


def _own_as_sandbox_user(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid, info.gid = SANDBOX_UID, SANDBOX_GID
    info.uname, info.gname = "ais", "ais"
    return info


def _extract_out(container, destination: Path) -> None:
    """Stream ``/out`` back from the container onto the host."""
    try:
        stream, _ = container.get_archive("/out")
    except (NotFound, APIError):
        return  # the container died before writing anything

    buffer = io.BytesIO()
    for chunk in stream:
        buffer.write(chunk)
    buffer.seek(0)

    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=buffer, mode="r") as archive:
        for member in archive.getmembers():
            # Strip the leading "out/" the daemon adds, and refuse anything that
            # would land outside the destination.
            relative = Path(member.name).relative_to("out") if member.name != "out" else None
            if relative is None or member.isdir():
                continue
            target = (destination / relative).resolve()
            if destination.resolve() not in target.parents:
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(extracted.read())


def _force_kill(container) -> None:
    try:
        container.kill()
    except (APIError, NotFound):
        pass


def _remove(container) -> None:
    """Destroy the container and its filesystem. This is what makes it ephemeral."""
    try:
        container.remove(force=True, v=True)
    except (APIError, NotFound, DockerException):
        pass
