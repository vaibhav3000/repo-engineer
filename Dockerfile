# A clean Linux environment for the deterministic benchmark and the test suite.
#
# This image is NOT the agent sandbox. Inside it, run_tests still executes
# agent-written test code with the rights of the container user, and the
# container has whatever network access `docker run` gives it. Use
# `--network none` (as the README does); per-call OS isolation of run_tests is
# planned work, not something this image provides.
#
# The base image is pinned by digest (python:3.13-slim, a version CI tests).

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --create-home --uid 10001 app
WORKDIR /home/app/repo-engineer

COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN pip install --root-user-action=ignore . && rm -rf build src/*.egg-info
COPY scripts/ ./scripts/
COPY results/ ./results/
RUN chown -R app:app /home/app/repo-engineer

USER app
CMD ["python", "scripts/run_benchmark.py"]

# Test stage: the runtime image plus the test suite.
FROM runtime AS test
COPY --chown=app:app tests/ ./tests/
CMD ["python", "-m", "pytest", "-q"]
