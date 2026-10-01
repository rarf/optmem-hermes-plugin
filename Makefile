# OptMem Hermes Plugin - Makefile
# Common development tasks

.PHONY: test test-cov lint fmt dev-install install build clean check test-windows help

# Run tests (Linux/macOS/Windows)
test:
	python -m pytest tests/ -q

# Run tests with coverage
test-cov:
	python -m pytest tests/ --cov=optmem --cov-report=term-missing

# Lint with ruff
lint:
	ruff check optmem/ tests/

# Format with ruff
fmt:
	ruff check --fix optmem/ tests/
	ruff format optmem/ tests/

# Install in editable mode with dev deps
dev-install:
	python -m pip install -e '.[dev]'

# Install in editable mode (production deps only)
install:
	python -m pip install -e .

# Build distribution packages
build:
	python -m build

# Clean build artifacts
clean:
	rm -rf build/ dist/ *.egg-info/ .pytest_cache/ .ruff_cache/ optmem/__pycache__/ tests/__pycache__/

# Full check before commit
check: lint test

# Run Windows-specific test (locking)
test-windows:
	python -m pytest tests/ -q -k "lock or windows"

# Show help
help:
	@printf '%s\n' 'Targets: test test-cov lint fmt dev-install install build clean check test-windows help'
