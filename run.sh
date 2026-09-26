#!/bin/sh
set -eu
app_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if "${MALCIP_PYTHON:-python3}" "$app_dir/ctl.py" "${1:-toggle}"; then
    exit 0
else
    result=$?
    [ "$result" -eq 2 ] || exit "$result"
fi
# XWayland lets Qt place these small panels precisely on KDE and Hyprland.
if [ "${XDG_SESSION_TYPE-}" = wayland ] && [ -n "${DISPLAY-}" ]; then
    export QT_QPA_PLATFORM=xcb
fi
exec "${MALCIP_PYTHON:-python3}" "$app_dir/malcip.py" "$@"
