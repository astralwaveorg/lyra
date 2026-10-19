PYTHON ?= ./.venv/bin/python
PIP ?= ./.venv/bin/pip

.PHONY: install test lint typecheck web-build docs dev run clean

install:
	python3 -m venv .venv
	$(PIP) install -e ".[dev]"
	cd web && npm ci

test:
	$(PYTHON) -m pytest -q
	cd web && npm run test

lint:
	$(PYTHON) -m ruff check .

typecheck:
	cd web && npm run typecheck

web-build:
	cd web && npm run build

docs:
	$(PIP) install -r docs/requirements.txt
	mkdocs build --strict

run:
	$(PYTHON) -m lyra serve

clean:
	rm -rf .pytest_cache .ruff_cache site
	cd web && rm -rf dist
