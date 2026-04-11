#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export PYTHONPATH="${ROOT_DIR}/src"

echo "Running full test suite..."
python3 -m unittest discover -s tests -v

echo
echo "Running end-to-end matrix..."
python3 -m unittest tests.test_e2e -v

echo
echo "Running transcript ingestion tests..."
python3 -m pytest -v \
    tests/test_transcripts.py \
    tests/test_adapters.py \
    tests/test_adapters_claude_code.py \
    tests/test_adapters_codex.py \
    tests/test_adapters_generic.py \
    tests/test_capture.py
