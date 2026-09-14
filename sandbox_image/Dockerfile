# The AiS sandbox image.
#
# Deliberately minimal: a Python interpreter, pytest, and an unprivileged user.
# Nothing else. The less that is installed here, the less an edit under review
# can reach for, and the smaller the gap between "the tests passed" and "the
# tests passed for a reason to do with the code".
#
# No AiS source is baked in. The runner, the patch engine and the tracer are
# streamed into the container per run, so rebuilding the image is not needed
# when they change.

ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

# Pinned so a verification run is reproducible: the tooling that judges an edit
# should not change underneath the evaluation.
ARG PYTEST_VERSION=8.3.4

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install --no-cache-dir "pytest==${PYTEST_VERSION}"

# Runs as an unprivileged user; the container also drops every Linux capability
# and gets no network interface at all (see ais/sandbox/docker_backend.py).
RUN groupadd --gid 1000 ais \
 && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin ais \
 && mkdir -p /workspace /out /ais \
 && chown -R ais:ais /workspace /out /ais

USER ais:ais
WORKDIR /workspace

# Overridden by the backend; present so the image is runnable on its own.
CMD ["python", "/ais/runner.py", "--root", "/"]
