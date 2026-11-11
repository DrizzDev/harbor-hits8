#!/bin/bash
# Oracle: writes the self-test's gold end state onto the phone. Must score reward 1.
set -euo pipefail
python3 "$(dirname "$0")/oracle.py"
