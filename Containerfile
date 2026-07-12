FROM registry.access.redhat.com/ubi9/python-312:latest

USER 0
RUN curl -sL https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz \
    | tar -xz -C /usr/local/bin oc kubectl && chmod +x /usr/local/bin/oc /usr/local/bin/kubectl
USER 1001

RUN git config --global user.email "agentit@agentit.local" && \
    git config --global user.name "AgentIT"

WORKDIR /opt/app-root/src

COPY pyproject.toml ./
RUN mkdir -p src/agentit && touch src/agentit/__init__.py && \
    pip install --no-cache-dir . && \
    rm -rf src/agentit

COPY src/ src/
RUN pip install --no-cache-dir --no-deps .

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python -c "import httpx; httpx.get('http://localhost:8080/healthz', timeout=2).raise_for_status()" || exit 1

ENTRYPOINT ["python", "-m", "agentit", "portal", "--port", "8080"]
