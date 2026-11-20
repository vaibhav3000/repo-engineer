# Developer entry points. Works from Git Bash on Windows and from Linux.
#
# Every Python command runs through `uv run --locked`, so the same commands
# work whether the venv keeps its scripts in .venv/Scripts or .venv/bin, and
# a stale uv.lock fails instead of drifting. Recipes use relative paths only:
# the checkout may live under a path with spaces.
#
# Docker: set REPO_ENGINEER_DOCKER_CMD (or DOCKER) when the engine is reached
# another way, for example "wsl -d Ubuntu -e docker". A shell function named
# docker is not visible to make.

UV ?= uv
RUN = $(UV) run --locked
PY = $(RUN) python
DOCKER ?= $(or $(REPO_ENGINEER_DOCKER_CMD),docker)
REPORT = Autonomous_Repository_Engineer
REPORT_BUILD = build

.PHONY: help setup lint typecheck test docs docs-check bench bench-check jail jail-check \
	report report-build sandbox-image docker text-check check

help:
	@echo "setup         install every dependency from uv.lock"
	@echo "lint          ruff check and ruff format --check"
	@echo "typecheck     pyright"
	@echo "test          the test suite (Docker tests skip without a Linux daemon)"
	@echo "docs          regenerate published numbers and the demo data from results/"
	@echo "docs-check    fail if published numbers or demo data are stale"
	@echo "bench         regenerate the scripted benchmark results"
	@echo "bench-check   run the benchmark twice (auto and local sandbox), results/ must not change"
	@echo "jail          regenerate the attack evidence (local, and docker when available)"
	@echo "jail-check    regenerate the evidence, results/ must not change"
	@echo "report        rebuild the report PDF and copy it into reports/"
	@echo "sandbox-image build the Docker sandbox image"
	@echo "docker        build the benchmark and test images"
	@echo "check         the full gate"

setup:
	$(UV) sync --locked --all-extras

lint:
	$(RUN) ruff check .
	$(RUN) ruff format --check .

typecheck:
	$(RUN) pyright

test:
	$(PY) -m pytest -q

docs:
	$(PY) scripts/sync_docs.py
	$(PY) demo-site/build.py

docs-check:
	$(PY) scripts/sync_docs.py --check
	$(PY) demo-site/build.py --check

text-check:
	$(PY) scripts/check_text.py

bench:
	$(PY) scripts/run_benchmark.py --sandbox auto

bench-check:
	$(PY) scripts/run_benchmark.py --sandbox auto
	$(PY) scripts/run_benchmark.py --sandbox local
	git diff --exit-code --stat results/

jail:
	$(PY) scripts/run_jail_attacks.py --backend local
	@if $(PY) -c "import sys; from repo_engineer.sandbox import DockerSandbox; sys.exit(0 if DockerSandbox().available() else 1)"; then \
		$(PY) scripts/run_jail_attacks.py --backend docker; \
	else \
		echo "no Linux Docker daemon answers: skipping the docker backend"; \
	fi

jail-check: jail
	git diff --exit-code --stat results/

# The PDF is not byte-reproducible, so the gate builds it into reports/build
# (ignored) and only `make report` copies it over the committed one.
report-build:
	cd reports && latexmk -pdf -interaction=nonstopmode -halt-on-error -outdir=$(REPORT_BUILD) $(REPORT).tex

report: report-build
	cp reports/$(REPORT_BUILD)/$(REPORT).pdf reports/$(REPORT).pdf

sandbox-image:
	$(PY) -m repo_engineer sandbox build

docker:
	$(DOCKER) build -t repo-engineer .
	$(DOCKER) build --target test -t repo-engineer-test .

check: lint typecheck docs-check text-check test bench-check jail-check report-build
	git status --porcelain
	@test -z "$$(git status --porcelain)" || (echo "the gate left the tree dirty"; exit 1)
	@echo "gate passed"
