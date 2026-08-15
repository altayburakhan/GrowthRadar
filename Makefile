.PHONY: install install-dev lint format typecheck test run web shell start

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt
	.venv/bin/pip install -e .

install-dev: install
	.venv/bin/pip install -r requirements-dev.txt
	.venv/bin/patchright install chromium

lint:
	.venv/bin/ruff check src tests

format:
	.venv/bin/black src tests
	.venv/bin/ruff check --fix src tests

typecheck:
	.venv/bin/mypy src

test:
	.venv/bin/pytest

run:
	.venv/bin/python -m growthradar "$(URL)"

web:
	.venv/bin/uvicorn growthradar.web:app --port 8000

shell:
	bash -c "source .venv/bin/activate && exec bash"

start: web
