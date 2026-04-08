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
