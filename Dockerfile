FROM python:3.11-slim

WORKDIR /workspace
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY tests/ ./tests/
COPY scripts/ ./scripts/

RUN pip install --no-cache-dir -e ".[dev]"

CMD ["pytest", "tests/", "-q"]
