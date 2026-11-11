#!/bin/bash
# Grades the phone's final state with the task's original grader. No reward is written when the state can't
# be read (INDETERMINATE): a harness fault is not a model failure.
set -u
LOGS="${VERIFIER_LOGS:-/logs/verifier}"; mkdir -p "$LOGS"
python3 "$(dirname "$0")/verify.py" --logs "$LOGS" --from-dir "${CAPTURE_DIR:-/state/capture}"
