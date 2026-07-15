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

# OpenShift runs this image under an arbitrary, per-namespace UID that never
# matches the build-time file owner of the .git directory copied in below --
# git's dubious-ownership check would otherwise refuse every git command
# against it. The credential helper supplies GITHUB_TOKEN (read from the
# environment at call time -- the same Secret already wired into the
# capability-scout/portal Deployments) for HTTPS push auth; no token is ever
# baked into the image itself.
RUN git config --global --add safe.directory /opt/app-root/src && \
    git config --global credential.helper '!f() { echo username=x-access-token; echo password=$GITHUB_TOKEN; }; f'

WORKDIR /opt/app-root/src

COPY pyproject.toml ./
RUN mkdir -p src/agentit && touch src/agentit/__init__.py && \
    pip install --no-cache-dir . && \
    rm -rf src/agentit

COPY src/ src/
RUN pip install --no-cache-dir --no-deps --force-reinstall .
COPY skills/ skills/
COPY checks/ checks/

# Real git history + origin remote so capability_scout.py / self-fix
# --create-pr can `git checkout -b` / commit / push against AgentIT's own
# repo from inside the running container -- without this there is no .git
# at all and every git_pr.py call fails immediately with "not a git
# repository". Safe to ship: the Tekton git-clone task that populates this
# build's workspace clones a public HTTPS URL with no embedded credentials.
COPY .git ./.git

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python -c "import httpx; httpx.get('http://localhost:8080/healthz', timeout=2).raise_for_status()" || exit 1

ENTRYPOINT ["python", "-m", "agentit", "portal", "--port", "8080"]
