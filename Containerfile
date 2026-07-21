FROM registry.access.redhat.com/ubi9/python-312:latest

USER 0
RUN curl -sfL https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz \
    | tar -xz -C /usr/local/bin oc kubectl && chmod +x /usr/local/bin/oc /usr/local/bin/kubectl

# gh CLI -- capability_scout.py's git_pr.py shells out to `gh pr create`/`gh
# pr list` to open and throttle self-improvement PRs; git alone (already in
# the base image) can only branch/commit/push, not open a PR. Official RPM
# install method for DNF4-based RHEL/UBI images:
# https://github.com/cli/cli/blob/trunk/docs/install_linux.md#dnf4
RUN dnf install -y 'dnf-command(config-manager)' && \
    dnf config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo && \
    dnf install -y gh && \
    dnf clean all
USER 1001

RUN git config --global user.email "agentit@agentit.local" && \
    git config --global user.name "AgentIT"

# Credential helper supplies GITHUB_TOKEN (read from the environment at call
# time -- the same Secret already wired into the capability-scout/portal
# Deployments) for HTTPS push auth; no token is ever baked into the image.
# safe.directory is set --system (as root, below) so arbitrary OpenShift
# UIDs see it; --global here only covers USER 1001 local runs.
RUN git config --global credential.helper '!f() { echo username=x-access-token; echo password=$GITHUB_TOKEN; }; f'

WORKDIR /opt/app-root/src

COPY pyproject.toml ./
# capability_scout.py's `tests-pass` safety gate runs
# `python -m pytest tests/ ...` against this image's own tree (repo_dir
# defaults to Path.cwd(), the running container's filesystem -- see
# watchers/capability_scout.py). The base install below used to install
# only the runtime deps, so pytest itself was never importable in this
# image and that gate failed every single cycle with "No module named
# pytest" (misreported as an opaque "pytest exited 1", since run_test_suite
# only captured stdout, where nothing was written -- see git_pr.py's
# sibling fix in capability_scout.py for the stderr side of this). Install
# the 'dev' extra (pytest, pytest-asyncio, httpx) here too, same as CI.
RUN mkdir -p src/agentit && touch src/agentit/__init__.py && \
    pip install --no-cache-dir ".[dev]" && \
    rm -rf src/agentit

COPY src/ src/
RUN pip install --no-cache-dir --no-deps --force-reinstall .
COPY skills/ skills/
# checks/ is intentionally NOT copied here -- Phase 4 of
# docs/extension-model-unification-plan-2026-07-18.md ported every
# checks/*.yaml file to a mode: detect skill under skills/, so checks/ now
# has zero files in it. Git doesn't track empty directories, so checks/
# doesn't exist at all in a checkout of this commit -- `COPY checks/
# checks/` fails the build outright ("/checks": not found) rather than
# copying nothing. check_engine.py's load_checks()/run_checks*() still
# exist (detect_check_definitions() depends on the rule-running half) and
# already handle a missing checks_dir gracefully (`if not
# checks_dir.is_dir(): return []`), so nothing at runtime expects this
# directory to exist. Re-add this COPY line if checks/ ever gains a real
# file again (e.g. a legacy check that hasn't been ported yet, or a new
# one added directly as YAML).
# See the pytest/dev-extra comment above -- the tests-pass gate also needs
# the actual test files to run, which this image never shipped. Most of
# tests/test_helm_templates.py (plus a chart-consistency check in
# test_helpers.py, and this Containerfile's own regression coverage in
# test_capability_scout.py) reads chart/templates/*.yaml and this
# Containerfile straight off disk relative to the repo root -- neither was
# ever copied in either, so those tests failed with a bare FileNotFoundError
# every real cycle, regardless of the actual proposal being tested.
COPY tests/ tests/
COPY chart/ chart/
# capability_scout.scan_doc_gaps() greps Path("docs")/*.md for "Known gap" /
# "Deliberately deferred" / etc. (see gather_evidence). Without shipping
# docs/, docs_dir.is_dir() is False every cycle and doc_gaps is always [] --
# the highest-precision evidence signal is silently zeroed. Same WORKDIR
# (/opt/app-root/src) the portal and capability-scout share.
COPY docs/ docs/
COPY Containerfile ./Containerfile

# Real git history + origin remote so capability_scout.py / self-fix
# --create-pr can `git checkout -b` / commit / push against AgentIT's own
# repo from inside the running container -- without this there is no .git
# at all and every git_pr.py call fails immediately with "not a git
# repository". Safe to ship: the Tekton git-clone task that populates this
# build's workspace clones a public HTTPS URL with no embedded credentials.
COPY .git ./.git
# The directories COPY just created (.git and its subdirs) land owned by
# root with mode 755 -- group has read+execute but not write. OpenShift
# runs this container under an arbitrary per-namespace UID that's never
# actually `1001`, but always shares gid 0 (root) -- the standard
# OpenShift-friendly pattern is exactly this: any UID, but group-writable
# so gid-0 membership is what actually grants access. Individual git files
# (HEAD, config, index, ...) already come out group-writable; only the
# *directories* were missing g+w, which blocks git from creating new lock
# files/refs (e.g. `.git/HEAD.lock`) inside them -- confirmed live: a real
# capability-scout PR attempt failed with "Unable to create
# '.git/HEAD.lock': Permission denied" for exactly this reason. Must run
# as root (USER 0) since only the owner or root can chmod files this
# user (1001) doesn't own.
USER 0
# OpenShift runs under an arbitrary per-namespace UID that never matches the
# build-time owner of .git. Put safe.directory in /etc/gitconfig (--system)
# so every UID sees it; --global alone lives under USER 1001's home and is
# invisible to Tekton/OpenShift smoke and scout pods (dubious ownership).
# Group-writable dirs OpenShift arbitrary UIDs (gid 0) need to mutate at
# runtime: .git for branch/commit/push, plus the L3 source-mode allowlist
# paths capability-scout writes before opening a PR. Without g+w on
# tests/skills/checks/src/docs, source-mode cycles fail at write_text with
# PermissionError even after gates pass (confirmed live on fa7db61).
# chmod g+w on directories *and* files under the L3 allowlist. Directory g+w
# alone lets scout create new files; file g+w is required to overwrite an
# existing root-owned COPY artifact (e.g. regenerating a test module).
RUN git config --system --add safe.directory /opt/app-root/src && \
    find .git -type d -exec chmod g+w {} + && \
    for d in tests skills checks src docs; do \
      if [ -d "$d" ]; then chmod -R g+w "$d"; fi; \
    done && \
    chmod g+w /opt/app-root/src
USER 1001

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python -c "import httpx; httpx.get('http://localhost:8080/healthz', timeout=2).raise_for_status()" || exit 1

ENTRYPOINT ["python", "-m", "agentit", "portal", "--port", "8080"]
