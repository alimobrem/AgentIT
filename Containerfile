FROM registry.access.redhat.com/ubi9/python-312:latest

WORKDIR /opt/app-root/src

COPY pyproject.toml ./
COPY src/ src/

RUN pip install --no-cache-dir .

EXPOSE 8080

USER 1001

ENTRYPOINT ["python", "-m", "agentit", "portal", "--port", "8080"]
