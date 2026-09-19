#!/usr/bin/env bash
#
# Runs the template engine unit tests:
#   - transactional template loading (tests/templates/transactions.py)
#   - cython compiler source maps   (tests/templates/cython_source_map.py)
#   - incremental template loader   (tests/templates/loader.py)
#   - pre-existing template suites  (render/exceptions/nodes)
#
# The full test suite (test.py) requires the compiled C extensions:
# install cython + setuptools and run `python3 build.py` first.
set -euo pipefail
cd "$(dirname "$0")"
python3 run_template_tests.py
