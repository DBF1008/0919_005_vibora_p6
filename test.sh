#!/usr/bin/env bash
#
# Runs every unit test script for the template engine refactoring:
#   1. Transactional template loading (engine.py)
#   2. Cython compiler source maps (compilers/cython.py)
#   3. Incremental template reload (loader.py)
#
# Each script is self-contained and can also be run manually, e.g.:
#   python3 tests/templates/transactional_loading.py
#
set -u
cd "$(dirname "$0")"

SCRIPTS=(
    "tests/templates/transactional_loading.py"
    "tests/templates/cython_source_map.py"
    "tests/templates/incremental_loader.py"
)

failed=0
for script in "${SCRIPTS[@]}"; do
    echo "======================================================================"
    echo "Running: ${script}"
    echo "======================================================================"
    if python3 "${script}"; then
        echo "PASS: ${script}"
    else
        echo "FAIL: ${script}"
        failed=1
    fi
    echo
done

if [ "${failed}" -ne 0 ]; then
    echo "Some test scripts failed."
    exit 1
fi
echo "All test scripts passed."
