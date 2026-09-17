#!/bin/sh
set -eu

data_dir="$HOME/picopg/data"
backup_root="$HOME/backups/picopg"
destination="$backup_root/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_root"

systemctl --user stop picopg.service
restart=1
trap 'if [ "$restart" -eq 1 ]; then systemctl --user start picopg.service; fi' EXIT

PYTHONPATH="$HOME/picopg/apps/server" /usr/bin/python3 - "$data_dir" "$destination" <<'PY'
import sys

import maintenance

maintenance.create_backup(sys.argv[1], sys.argv[2], service_stopped=True)
maintenance.validate_backup(sys.argv[2])
print(sys.argv[2])
PY
