#!/bin/sh
set -eu

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
app_dir=$data_home/malcip
desktop_dir=$data_home/applications
mkdir -p "$app_dir" "$desktop_dir"
install -m 755 "$source_dir/malcip.py" "$source_dir/run.sh" "$app_dir"
install -m 644 "$source_dir/README.md" "$app_dir/README.md"
python3 - "$source_dir/malcip.desktop.in" "$desktop_dir/malcip.desktop" "$app_dir/run.sh" <<'PY'
from pathlib import Path
import sys

template, output, executable = map(Path, sys.argv[1:])
quoted = '"' + str(executable).replace('\\', '\\\\').replace('"', '\\"') + '"'
output.write_text(template.read_text().replace('@EXEC@', quoted))
PY
printf 'Installed MALCIP in %s\n' "$app_dir"
