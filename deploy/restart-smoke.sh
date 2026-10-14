#!/bin/sh
set -eu

: "${PICOPG_SMOKE_BASE_URL:?set PICOPG_SMOKE_BASE_URL}"
: "${PICOPG_SMOKE_ACCESS_CODE:?set PICOPG_SMOKE_ACCESS_CODE}"
: "${PICOPG_REPLICA_TOKEN:?set PICOPG_REPLICA_TOKEN}"

systemctl --user restart picopg

attempt=0
until curl --fail --silent --show-error "${PICOPG_SMOKE_BASE_URL%/}/healthz" >/dev/null; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 20 ]; then
        echo "PicoPG did not become healthy after restart" >&2
        exit 1
    fi
    sleep 1
done

exec python3 deploy/smoke.py \
    --base-url "$PICOPG_SMOKE_BASE_URL" \
    --access-code "$PICOPG_SMOKE_ACCESS_CODE" \
    --operator-token "$PICOPG_REPLICA_TOKEN"
