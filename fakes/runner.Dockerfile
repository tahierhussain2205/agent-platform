# The sandbox runner (B-008): runs sandbox commands with no network, as a non root user.
# Compose and tests only; never deployed. Python, git and pytest are what the fixture repositories need.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "pytest==8.3.5" \
    && mkdir -p /sandboxes /runner && chown 10001 /sandboxes
COPY fakes/sandbox_runner.py /runner/sandbox_runner.py
ENV SANDBOX_ROOT=/sandboxes PYTHONUNBUFFERED=1
USER 10001
CMD ["python", "/runner/sandbox_runner.py"]
