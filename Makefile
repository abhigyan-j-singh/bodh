# The checks a change must pass: `make check` runs the lot; CI runs the same.
PY ?= .venv/bin
.PHONY: check lint format types test coverage install

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

coverage:
	$(PY)/pytest -q --cov=bodh --cov-report=term-missing

install:
	./install.sh
