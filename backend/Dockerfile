# The broker image. Unlike the Hermes era there is no second image: the agent
# runs in this process, so the four campus MCP servers have to be installed
# here, in the environment that will actually spawn them.
#
# They are installed but carry no credentials. Each student's servers are
# launched as subprocesses with only that student's METU credentials in their
# environment (app/campus/mcp_config.py), so one image serves N isolated
# credential sets and none of them is in a layer.

ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.5.11

# --- Stage 1: one isolated virtualenv per MCP server -----------------------
#
# Separate venvs rather than one shared site-packages because the four upstream
# projects don't co-resolve: metu-webmail-mcp needs `fastmcp`, the others pin
# `mcp` directly, and their pydantic/httpx ranges overlap only by luck.
# Isolating them means upgrading one server can never break another — and
# keeps them off the broker's own dependency tree, which now includes agno.
#
# The interpreter is a uv-managed standalone build installed *under /opt/mcp*
# rather than the builder's own /usr/local python, because the whole tree is
# copied into the runtime image in stage 2. Everything the venvs point at has
# to travel with them, at the same absolute path they were built at.
FROM ${UV_IMAGE} AS uv

FROM python:3.12-slim AS mcp-builder

COPY --from=uv /uv /usr/local/bin/uv

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV UV_PYTHON_INSTALL_DIR=/opt/mcp/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

RUN uv python install 3.11

# Pinned to exact commits, not branches. These servers run as subprocesses of
# the broker with a student's METU credentials in their environment, so
# "whatever is on main today" is not an acceptable input to a build that serves
# students: a push to any of the four repos would otherwise change what runs in
# front of them with no deploy, no review, and no way to notice.
#
# What this buys is reproducibility, not review. These are the commits that
# were HEAD on 2026-08-30 and nobody has audited them. Pinning is the thing
# that makes an audit worth doing later — reviewing a moving branch proves
# nothing about what will run tomorrow.
#
# To bump one: `git ls-remote <repo> <branch>`, put the SHA here, rebuild. A
# branch name still works as a build arg for local iteration; the manifest
# below records whatever it actually resolved to, so an image built that way
# still reports the truth about itself.
ARG SAIS_REPO=https://github.com/atesahmet0/metu-sais-mcp.git
ARG SAIS_REF=33c228842fcbe44e3459145055ff28a5e13bae64
ARG COURSE_INFO_REPO=https://github.com/atesahmet0/metu-course-info-mcp.git
ARG COURSE_INFO_REF=be7a4a93425812b538205ddb6cfbce8b494b6de4
ARG WEBMAIL_REPO=https://github.com/atesahmet0/metu-webmail-mcp.git
ARG WEBMAIL_REF=af0fe0503806db5d704e041866853dcbbe98e25f
ARG ODTUCLASS_REPO=https://github.com/erkinemreta1/metu-odtuclass-mcp.git
ARG ODTUCLASS_REF=626d9e088b1f568ee64e34176246c6d8102b4415

# The manifest is written before .git is removed, and records what `rev-parse`
# resolved rather than the ref that was requested. That distinction is the
# whole point: an image built with a branch arg still reports the exact commit
# it contains, so "which commit is in production" is answerable from a running
# container (GET /health) instead of from build logs nobody kept.
RUN set -eux; \
    for entry in \
        "sais|${SAIS_REPO}|${SAIS_REF}" \
        "course-info|${COURSE_INFO_REPO}|${COURSE_INFO_REF}" \
        "webmail|${WEBMAIL_REPO}|${WEBMAIL_REF}" \
        "odtuclass|${ODTUCLASS_REPO}|${ODTUCLASS_REF}" \
    ; do \
        slug="${entry%%|*}"; rest="${entry#*|}"; \
        repo="${rest%%|*}"; ref="${rest##*|}"; \
        git clone "${repo}" "/opt/mcp/${slug}"; \
        git -C "/opt/mcp/${slug}" checkout --quiet "${ref}"; \
        printf '%s %s %s\n' "${slug}" \
            "$(git -C "/opt/mcp/${slug}" rev-parse HEAD)" "${repo}" \
            >> /opt/mcp/MANIFEST; \
        rm -rf "/opt/mcp/${slug}/.git"; \
        uv venv --python 3.11 "/opt/mcp/${slug}/.venv"; \
    done

# Two corrections to the pinned source, applied before its wheel is built.
# Upstream skips a meeting table when it is nested in the same row as the
# section button; and it never opens the page behind that section button, which
# is the only place METU states who may register for a section — the departments
# admitted and their surname, CGPA and year ranges. Adding the latter touches
# three modules, so the whole patch directory is copied rather than one file.
#
# Two things this must not do quietly. It must not silently no-op: `COPY` to a
# path creates missing parents, so if upstream ever moves the module the patch
# would land somewhere harmless and the build would still succeed with the bug
# in it — hence the `test -f`. And it must not make the manifest lie: the block
# above exists so a running container can answer "which commit is serving
# this?", and an image carrying an undeclared source change no longer matches
# the commit it names, so the patch gets its own manifest line (slug, content
# hash, origin) and is reported by GET /health alongside the pins.
COPY vendor_patches/course_info/ /tmp/course_info_patch/
RUN set -eux; \
    dir=/opt/mcp/course-info/src/metu_course_info_mcp; \
    for name in sais_client.py models.py server.py; do \
        test -f "${dir}/${name}"; \
        cp "/tmp/course_info_patch/${name}" "${dir}/${name}"; \
        printf 'course-info-patch %s vendor_patches/course_info/%s\n' \
            "$(sha256sum "${dir}/${name}" | cut -d" " -f1)" "${name}" >> /opt/mcp/MANIFEST; \
    done; \
    rm -rf /tmp/course_info_patch

# Expose existing client thread headers so replies send exactly the approved
# recipient, subject and body, without upstream implicit quoting or recipients.
COPY vendor_patches/webmail/patch_server.py /tmp/patch_webmail.py
RUN python /tmp/patch_webmail.py /opt/mcp/webmail/src/metu_webmail_mcp/server.py \
    && printf 'webmail-patch %s vendor_patches/webmail/patch_server.py\n' \
        "$(sha256sum /tmp/patch_webmail.py | cut -d" " -f1)" >> /opt/mcp/MANIFEST \
    && rm /tmp/patch_webmail.py

# Installed as a built wheel, not `-e`, so nothing at runtime depends on the
# source tree layout surviving. metu-odtuclass-mcp ships loose modules and a
# requirements.txt rather than an installable package, so it gets its
# dependencies installed and is launched by script path instead — see
# app/campus/catalog.py.
RUN set -eux; \
    for slug in sais course-info webmail; do \
        VIRTUAL_ENV="/opt/mcp/${slug}/.venv" uv pip install "/opt/mcp/${slug}"; \
    done; \
    VIRTUAL_ENV=/opt/mcp/odtuclass/.venv uv pip install -r /opt/mcp/odtuclass/requirements.txt

# --- Stage 2: the broker ---------------------------------------------------
FROM python:3.12-slim

COPY --from=mcp-builder /opt/mcp /opt/mcp

# Import each server with the interpreter that will actually launch it, in the
# image it will actually run in. This is the step that catches a runtime image
# whose libc the standalone interpreter can't run against — better a failed
# build than four campus tools that silently never start.
RUN set -eux; \
    /opt/mcp/sais/.venv/bin/python -c "import sais_mcp.server"; \
    /opt/mcp/course-info/.venv/bin/python -c "import metu_course_info_mcp.__main__"; \
    /opt/mcp/webmail/.venv/bin/python -c "import metu_webmail_mcp.server"; \
    cd /opt/mcp/odtuclass && /opt/mcp/odtuclass/.venv/bin/python -c "import odtuclass_mcp"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app app
COPY vendor_patches/course_info vendor_patches/course_info
COPY alembic alembic
COPY alembic.ini .

# Per-student MCP working directories (CAMPUS_STATE_ROOT). Owned by the
# unprivileged user because the campus subprocesses run as that user and cache
# session tokens here.
RUN useradd --create-home --uid 10001 devrimo \
    && mkdir -p /var/lib/devrimo/campus \
    && chown -R devrimo:devrimo /var/lib/devrimo

# The broker no longer needs the Docker socket, so it no longer needs to be
# root: dropping to an unprivileged user is now just a chown away, where under
# the container runtime it was impossible.
USER devrimo

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
