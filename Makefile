# The checks a change must pass: `make check` runs the lot; CI runs the same, plus `coverage`,
# `perf` on Linux and `audit`.
PY ?= .venv/bin
.PHONY: check lint format types test coverage perf browser audit install

check: lint types test

lint:
	$(PY)/ruff check src tests contrib
	$(PY)/ruff format --check src tests contrib

format:
	$(PY)/ruff check --fix src tests contrib
	$(PY)/ruff format src tests contrib

types:
	$(PY)/mypy

test:
	$(PY)/pytest -q

coverage:  # the whole package, then the gate CI applies: 85% on the core modules
	$(PY)/pytest -q --cov=bodh --cov-report=term-missing
	$(PY)/coverage report --fail-under=85 --include="src/bodh/store.py,src/bodh/cutting.py,src/bodh/feedback.py,src/bodh/judgment.py,src/bodh/run.py,src/bodh/config.py"

perf:  # the 50,000-article timings (a fixture is built in a temporary folder; ten seconds)
	BODH_PERF=1 $(PY)/pytest -q tests/test_perf.py -s

browser:  # the page in Chromium: the visual baselines, the keyboard order, the live layer (needs playwright)
	BODH_BROWSER=1 $(PY)/pytest -q tests/visual

audit:
	$(PY)/pip-audit --progress-spinner off

install:
	./install.sh
