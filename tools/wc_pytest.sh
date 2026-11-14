#!/usr/bin/env bash
# wc_pytest.sh — batch pytest for Wayfarer's Cycle workers.
#
# Why this exists: workers were running one `env -u WC_PG_DB python -m pytest`
# per test file. Each invocation is a fresh Python process that imports the
# whole app. At ~50 workers this produced ~250 python processes and saturated
# the disk. This runs N files in ONE process.
#
# Usage:
#   tools/wc_pytest.sh server/tests/test_a.py server/tests/test_b.py
#   tools/wc_pytest.sh server/tests/            # whole directory
#   WC_EXTRA="-k foo" tools/wc_pytest.sh server/tests/test_a.py
#   tools/wc_pytest.sh --keep-db-env server/tests/test_ledger_pg.py
#
# By default this strips ALL WC_PG_* vars. Stripping only WC_PG_DB is not
# enough — the suite's skip check watches other WC_PG_* names, so a partial
# strip silently runs database tests against a live database.
#
# --keep-db-env is the opt-out. It keeps the caller's WC_PG_* settings so a
# PostgreSQL-backed test can actually run. It is opt-in and never automatic:
# with no flag the strip below happens exactly as it always has.
#
# RUNAWAY PROTECTION (added 2026-10-05)
# ------------------------------------
# A test run consumed 15.7 GB of RAM over 37 minutes and had to be killed by
# hand. The cause was an exact-integer power with a billion-digit exponent
# (2 ** 10 ** 400) fed in as a "corrupt value" to a guard sweep. Nothing in
# pytest stopped it; the process simply grew until the machine was out of
# memory, which takes down every other window on the host.
#
# Every run is therefore wrapped in tools/run_test_capped.py, which applies a
# hard per-process memory cap and a wall-clock timeout via a Windows Job Object.
# The kernel enforces both, so a runaway cannot ignore them. Measured: the same
# 2 ** 10 ** 400 that reached 15.7 GB now dies at ~0.7 GB in 7 seconds.
#
# Override with WC_MEM_GB and WC_TIMEOUT, or bypass with WC_NO_CAP=1.

set -uo pipefail

REPO="${WC_REPO:-G:/Projects/wayfarers-cycle}"
PY="${WC_PY:-C:/Users/Boss/AppData/Local/hermes/installs/3556b0859749568e/environments/7bb77ee6a9b64c07b94c41b54e08e540/venv/Scripts/python.exe}"

if [ "$#" -eq 0 ]; then
  echo "usage: wc_pytest.sh [--keep-db-env] <file-or-dir> [more...]" >&2
  exit 2
fi

# Pull --keep-db-env out of the argument list before anything else sees it.
# It is ours, not pytest's; leaving it in place would make pytest reject it.
KEEP_DB_ENV=0
ARGS=()
for arg in "$@"; do
  if [ "$arg" = "--keep-db-env" ]; then
    KEEP_DB_ENV=1
  else
    ARGS+=("$arg")
  fi
done

if [ "${#ARGS[@]}" -eq 0 ]; then
  echo "usage: wc_pytest.sh [--keep-db-env] <file-or-dir> [more...]" >&2
  exit 2
fi

cd "$REPO" || { echo "cannot cd to $REPO" >&2; exit 2; }

if [ "$KEEP_DB_ENV" -eq 1 ]; then
  # Caller asked for the database settings to survive. Do not touch them, in
  # this shell or in the environment handed to pytest.
  if [ "${WC_NO_CAP:-0}" = "1" ]; then
    exec env PYTHONDONTWRITEBYTECODE=1 \
      "$PY" -m pytest "${ARGS[@]}" \
      -q -p no:randomly -p no:cacheprovider --no-header --import-mode=importlib \
      ${WC_EXTRA:-}
  fi
  # Run the capper on the SAME interpreter, so the memory limit applies to the
  # process that actually does the work rather than to a wrapper shell.
  exec "$PY" "$REPO/tools/run_test_capped.py" \
    --mem-gb "${WC_MEM_GB:-2}" --timeout "${WC_TIMEOUT:-600}" -q -- \
    "$PY" -m pytest "${ARGS[@]}" \
    -q -p no:randomly -p no:cacheprovider --no-header --import-mode=importlib \
    ${WC_EXTRA:-}
fi

# Clear every database variable, not just WC_PG_DB.
for v in WC_PG_DB WC_PG_HOST WC_PG_USER WC_PG_PASSWORD WC_PG_PORT; do
  unset "$v" 2>/dev/null || true
done

if [ "${WC_NO_CAP:-0}" = "1" ]; then
  exec env -u WC_PG_DB -u WC_PG_HOST -u WC_PG_USER -u WC_PG_PASSWORD -u WC_PG_PORT \
    PYTHONDONTWRITEBYTECODE=1 \
    "$PY" -m pytest "${ARGS[@]}" \
    -q -p no:randomly -p no:cacheprovider --no-header --import-mode=importlib \
    ${WC_EXTRA:-}
fi

exec env -u WC_PG_DB -u WC_PG_HOST -u WC_PG_USER -u WC_PG_PASSWORD -u WC_PG_PORT \
  "$PY" "$REPO/tools/run_test_capped.py" \
  --mem-gb "${WC_MEM_GB:-2}" --timeout "${WC_TIMEOUT:-600}" -q -- \
  "$PY" -m pytest "${ARGS[@]}" \
  -q -p no:randomly -p no:cacheprovider --no-header --import-mode=importlib \
  ${WC_EXTRA:-}
