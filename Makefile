.PHONY: install run-web run-cli test clean

install:
	pip install -e .

run-web:
	python3 web/run_web.py

run-cli:
	python3 -m cli.main monitor

test:
	python3 -m pytest tests/

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
