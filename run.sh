#!/bin/sh
set -eu
app_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# XWayland lets Qt place these small panels precisely on KDE and Hyprland.
if [ "${XDG_SESSION_TYPE-}" = wayland ] && [ -n "${DISPLAY-}" ]; then
    export QT_QPA_PLATFORM=xcb
fi
exec "${MALCIP_PYTHON:-python3}" "$app_dir/malcip.py" "$@"
