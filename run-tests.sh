#!/bin/sh

set -u
cd "$(dirname "$0")"

log=$(mktemp "${TMPDIR:-/tmp}/picopg-tests.XXXXXX")
trap 'rm -f "$log"' EXIT HUP INT TERM

failed=0
for test in $(find apps deploy tests -name '*.test.py' | sort); do
    printf '%-45s' "$test"
    if python3 "$test" >"$log" 2>&1; then
        echo ok
    else
        echo FAIL
        cat "$log"
        failed=1
    fi
done
exit "$failed"
