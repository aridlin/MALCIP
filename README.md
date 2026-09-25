# MALCIP

**Make Arch Look Cool In Public** is a small desktop overlay for KDE Plasma and Hyprland. It uses Qt through XWayland, NumPy for the fluid simulation, and no Electron runtime.

![MALCIP control bar](preview-bar.png)
![MALCIP fluid popup](preview-fluid.png)

The top center control bar uses 50 px cells, 5 px gaps, and a gliding selection frame inspired by Workspace Field. Its icons are drawn as geometry rather than emoji. Popups are transparent, click-through, and arranged in a stack at the side of the primary monitor. Closing one moves the others into place.

- **Fluid** is a particle/grid FLIP simulation rendered as a continuous water surface at physical display resolution. The pointer acts as a small solid object when it enters the water. A volume correction prevents the particle pool from gradually collapsing onto the floor. Ordered dither is limited to the surface edge.
- **System** shows live CPU, memory, and root disk usage.

## Install

On Arch Linux, install the runtime dependencies:

```sh
sudo pacman -S python python-pyqt6 python-numpy python-psutil python-xlib
```

Then run:

```sh
./install.sh
~/.local/share/malcip/run.sh toggle
```

`install.sh` writes only to the current user's XDG data directory. To use another Python environment, set `MALCIP_PYTHON` to its interpreter. On a Wayland session with `DISPLAY` available, the launcher selects Qt's X11 backend so XWayland can position the popups and pass clicks through them.

## Controls

Bind `~/.local/share/malcip/run.sh toggle` to a shortcut in KDE or Hyprland. The shortcut opens or hides **only the bar**; running popups remain visible.

While the bar is open:

| Keys | Action |
| --- | --- |
| Arrow keys or A/D | Move the gliding selection frame |
| Enter or E | Activate the selected cell |
| 1/F, 2/S | Toggle Fluid or System directly |
| Shift+Enter | Open config |
| R | Reset fluid |
| H | Toggle surface dither |
| Escape | Hide the bar |

The cells also respond to mouse clicks. Config controls particle count, FLIP/PIC blend, and dither. A `MALCIP_SCREEN` environment variable can select a display by its Qt screen name.

For Hyprland, one possible binding is:

```ini
bind = SUPER SHIFT, P, exec, ~/.local/share/malcip/run.sh toggle
```

The overlay windows request XWayland's skip-taskbar and skip-pager states, so KDE does not list them as separate Python tasks.

## Current scope

This is an early FLIP-focused MALCIP release. A globe and other desktop effects are planned after the fluid behavior is confirmed. At 4,000 particles and 1.5× display scaling, a local 90-frame measurement found a 12.4 ms median and 19.9 ms 95th-percentile simulation plus rendering time while the pointer intersected the water. Qt painting and compositor latency add to that.

MIT licensed. See [LICENSE](LICENSE).
