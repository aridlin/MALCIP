#!/usr/bin/env python3
"""MALCIP: small ambient desktop simulations and live instruments."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
import time

import numpy as np
import psutil
from PyQt6.QtCore import QEasingCurve, QEvent, QObject, QPoint, QPointF, QRect, QRectF, QPropertyAnimation, Qt, QTimer
from PyQt6.QtGui import QColor, QCursor, QFont, QImage, QPainter, QPainterPath, QPen, QPolygonF
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QLabel, QPushButton,
    QSlider, QVBoxLayout, QWidget,
)


APP = "MALCIP"
ACCENT = QColor("#a8ffb7")
BG = QColor("#09130f")
BORDER = QColor("#3c8060")
CONFIG_PATH = Path.home() / ".config/malcip/config.json"
SOCKET_PATH = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/malcip-{os.getuid()}")) / "malcip.sock"


def load_config() -> dict:
    defaults = {"particle_count": 2400, "flip_blend": 0.94, "halftone": True}
    try:
        values = json.loads(CONFIG_PATH.read_text())
        return {**defaults, **values}
    except (OSError, ValueError, TypeError):
        return defaults


def save_config(values: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(values, indent=2) + "\n")
    temp.replace(CONFIG_PATH)


class FlipFluid:
    """2D particle/grid FLIP: P2G, pressure projection, then FLIP/PIC G2P."""

    def __init__(self, count=2400, blend=0.94, width=112, height=65):
        self.w, self.h = width, height
        self.blend = blend
        self.rng = np.random.default_rng(2026)
        self.x = self.rng.uniform(7, width-7, count)
        self.y = self.rng.uniform(height-23, height-6, count)
        self.vx = self.rng.normal(0, 0.06, count)
        self.vy = self.rng.normal(0, 0.06, count)
        self.u = np.zeros((height, width), dtype=np.float32)
        self.v = np.zeros_like(self.u)
        self.pressure = np.zeros_like(self.u)
        self.tick = 0
        self.obstacle = None

    def resize_particles(self, count: int) -> None:
        if count == len(self.x):
            return
        self.__init__(count, self.blend, self.w, self.h)

    def add_impulse(self, px: float, py: float, dx: float, dy: float) -> None:
        delta = (self.x - px) ** 2 + (self.y - py) ** 2
        weight = np.exp(-delta / 90.0)
        self.vx += weight * dx * 0.18
        self.vy += weight * dy * 0.18

    def set_obstacle(self, px: float, py: float, vx=0.0, vy=0.0) -> None:
        self.obstacle = (px, py, 6.0, vx, vy)

    def sample(self, grid: np.ndarray) -> np.ndarray:
        x0 = np.clip(self.x.astype(np.int32), 0, self.w - 2)
        y0 = np.clip(self.y.astype(np.int32), 0, self.h - 2)
        fx = self.x - x0
        fy = self.y - y0
        return (grid[y0, x0] * (1-fx) * (1-fy)
                + grid[y0, x0+1] * fx * (1-fy)
                + grid[y0+1, x0] * (1-fx) * fy
                + grid[y0+1, x0+1] * fx * fy)

    def step(self) -> None:
        self.tick += 1
        # A moving pressure pulse makes an idle pool ripple without a fountain.
        phase = self.tick * 0.055
        wave_x = self.w * (0.5 + 0.28*math.sin(phase*0.4))
        wave = np.exp(-((self.x-wave_x)**2/230 + (self.y-(self.h-8))**2/180))
        self.vy -= wave * (0.15 + 0.05*math.sin(phase))
        self.vx += wave * 0.035 * math.cos(phase)
        self.vy += 0.12
        self.vx *= 0.997
        self.vy *= 0.997

        x0 = np.clip(self.x.astype(np.int32), 0, self.w - 2)
        y0 = np.clip(self.y.astype(np.int32), 0, self.h - 2)
        fx, fy = self.x - x0, self.y - y0
        weights = ((1-fx)*(1-fy), fx*(1-fy), (1-fx)*fy, fx*fy)
        mass = np.zeros_like(self.u)
        self.u.fill(0)
        self.v.fill(0)
        for (ox, oy), weight in zip(((0,0),(1,0),(0,1),(1,1)), weights):
            indices = (y0+oy, x0+ox)
            np.add.at(mass, indices, weight)
            np.add.at(self.u, indices, self.vx * weight)
            np.add.at(self.v, indices, self.vy * weight)
        wet = mass > 1e-6
        self.u[wet] /= mass[wet]
        self.v[wet] /= mass[wet]
        # Extend sparse grid velocities into nearby empty cells before solving.
        for _ in range(2):
            neighbor_mass = (np.roll(wet, 1, 0).astype(np.int8)
                             + np.roll(wet, -1, 0).astype(np.int8)
                             + np.roll(wet, 1, 1).astype(np.int8)
                             + np.roll(wet, -1, 1).astype(np.int8))
            fill = (~wet) & (neighbor_mass > 0)
            for field in (self.u, self.v):
                neighbor = (np.roll(field, 1, 0) * np.roll(wet, 1, 0)
                            + np.roll(field, -1, 0) * np.roll(wet, -1, 0)
                            + np.roll(field, 1, 1) * np.roll(wet, 1, 1)
                            + np.roll(field, -1, 1) * np.roll(wet, -1, 1))
                field[fill] = neighbor[fill] / neighbor_mass[fill]
            wet[fill] = True
        old_u, old_v = self.u.copy(), self.v.copy()

        self.u[:, 0] = self.u[:, -1] = 0
        self.v[0, :] = self.v[-1, :] = 0
        divergence = np.zeros_like(self.u)
        divergence[1:-1, 1:-1] = (
            self.u[1:-1, 2:] - self.u[1:-1, :-2]
            + self.v[2:, 1:-1] - self.v[:-2, 1:-1]
        ) * 0.5
        pressure = np.zeros_like(self.pressure)
        for _ in range(28):
            pressure[1:-1, 1:-1] = (
                self.pressure[1:-1, :-2] + self.pressure[1:-1, 2:]
                + self.pressure[:-2, 1:-1] + self.pressure[2:, 1:-1]
                - divergence[1:-1, 1:-1]
            ) * 0.25
            self.pressure, pressure = pressure, self.pressure
        self.u[1:-1, 1:-1] -= (
            self.pressure[1:-1, 2:] - self.pressure[1:-1, :-2]) * 0.5
        self.v[1:-1, 1:-1] -= (
            self.pressure[2:, 1:-1] - self.pressure[:-2, 1:-1]) * 0.5
        self.u[:, 0] = self.u[:, -1] = 0
        self.v[0, :] = self.v[-1, :] = 0

        pic_u, pic_v = self.sample(self.u), self.sample(self.v)
        flip_u = self.vx + self.sample(self.u - old_u)
        flip_v = self.vy + self.sample(self.v - old_v)
        self.vx = self.blend * flip_u + (1-self.blend) * pic_u
        self.vy = self.blend * flip_v + (1-self.blend) * pic_v
        self.vx = np.clip(self.vx, -2.5, 2.5)
        self.vy = np.clip(self.vy, -2.5, 2.5)
        self.x += self.vx
        self.y += self.vy
        left, right = self.x < 2, self.x > self.w-3
        top, bottom = self.y < 2, self.y > self.h-3
        self.x = np.clip(self.x, 2, self.w-3)
        self.y = np.clip(self.y, 2, self.h-3)
        self.vx[left | right] *= -0.36
        self.vy[top | bottom] *= -0.30
        # The coarse projection grid cannot resolve a tall particle stack by
        # itself. Gently redistribute each horizontal slice by vertical rank
        # so particles keep their volume instead of accumulating on the floor.
        bins = np.clip((self.x / self.w * 16).astype(np.int32), 0, 15)
        for column in range(16):
            members = np.flatnonzero(bins == column)
            if len(members) < 2:
                continue
            ordered = members[np.argsort(self.y[members])]
            target = np.linspace(self.h-23, self.h-3, len(ordered))
            correction = np.clip((target-self.y[ordered])*0.11, -1.5, 1.5)
            self.y[ordered] += correction
            self.vy[ordered] += correction*0.35
        if self.obstacle is not None:
            ox, oy, radius, ovx, ovy = self.obstacle
            dx, dy = self.x-ox, self.y-oy
            distance = np.maximum(np.hypot(dx, dy), 1e-5)
            inside = distance < radius
            nx, ny = dx[inside]/distance[inside], dy[inside]/distance[inside]
            self.x[inside] = ox + nx*radius
            self.y[inside] = oy + ny*radius
            relative_inward = (self.vx[inside]-ovx)*nx + (self.vy[inside]-ovy)*ny
            bounce = np.minimum(relative_inward, 0)*1.25
            self.vx[inside] -= bounce*nx
            self.vy[inside] -= bounce*ny
            self.x = np.clip(self.x, 2, self.w-3)
            self.y = np.clip(self.y, 2, self.h-3)

    def image(self, halftone=True, out_size=(318, 152)) -> QImage:
        # Reconstruct a single water surface from the FLIP particles. Rendering
        # particle density alone makes a liquid pool look like floating smoke.
        bins = np.clip(self.x.astype(np.int32), 0, self.w-1)
        surface = np.full(self.w, np.nan, dtype=np.float32)
        for column in range(self.w):
            nearby = self.y[np.abs(bins-column) <= 3]
            if len(nearby):
                surface[column] = np.percentile(nearby, 12)
        known = np.flatnonzero(np.isfinite(surface))
        surface = np.interp(np.arange(self.w), known, surface[known])
        for _ in range(3):
            surface = np.convolve(np.pad(surface, (2, 2), mode="edge"),
                                  np.array([1, 2, 3, 2, 1])/9, mode="valid")
        out_w, out_h = out_size
        xs = np.linspace(0, self.w-1, out_w)
        ys = np.linspace(0, self.h-1, out_h)
        crest = np.interp(xs, np.arange(self.w), surface)[None, :]
        depth = ys[:, None] - crest
        water = np.clip((depth+0.35)/0.7, 0, 1)
        water = water*water*(3-2*water)
        gleam = np.exp(-(depth/0.55)**2)
        lower = np.clip(depth/24, 0, 1)
        bayer = np.array([[0, 8, 2, 10], [12, 4, 14, 6],
                          [3, 11, 1, 9], [15, 7, 13, 5]], dtype=np.float32) / 16
        yy, xx = np.indices((out_h, out_w))
        edge = (water > 0.05) & (water < 0.95)
        dots = (bayer[yy % 4, xx % 4] < water) & edge if halftone else np.zeros_like(edge)
        pixels = np.empty((out_h, out_w, 4), dtype=np.uint8)
        pixels[:, :, 0] = np.clip(18 + lower*18 + gleam*107, 0, 255).astype(np.uint8)
        pixels[:, :, 1] = np.clip(108 + lower*43 + gleam*130, 0, 255).astype(np.uint8)
        pixels[:, :, 2] = np.clip(83 + lower*25 + gleam*106, 0, 255).astype(np.uint8)
        pixels[:, :, 3] = np.clip(water*165 + gleam*70 + dots*18, 0, 240).astype(np.uint8)
        if self.obstacle is not None:
            ox, oy, radius, _, _ = self.obstacle
            distance = np.hypot(xs[None, :]-ox, ys[:, None]-oy)
            solid = np.clip((distance-radius+0.2)/0.5, 0, 1)
            rim = np.exp(-((distance-radius)/0.45)**2)*water
            pixels[:, :, 0] = np.clip(pixels[:, :, 0] + rim*43, 0, 255).astype(np.uint8)
            pixels[:, :, 1] = np.clip(pixels[:, :, 1] + rim*55, 0, 255).astype(np.uint8)
            pixels[:, :, 2] = np.clip(pixels[:, :, 2] + rim*47, 0, 255).astype(np.uint8)
            pixels[:, :, 3] = np.clip(pixels[:, :, 3]*solid + rim*65, 0, 240).astype(np.uint8)
        pixels = np.ascontiguousarray(pixels)
        return QImage(pixels.data, out_w, out_h, pixels.strides[0],
                      QImage.Format.Format_RGBA8888).copy()


class Chrome(QWidget):
    def __init__(self, width, height, pass_through=False):
        super().__init__()
        self.setFixedSize(width, height)
        kind = Qt.WindowType.ToolTip if pass_through else Qt.WindowType.Tool
        flags = (kind | Qt.WindowType.FramelessWindowHint
                 | Qt.WindowType.WindowStaysOnTopHint)
        if pass_through:
            flags |= Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._animation = None
        self._move_animation = None
        self._pointer_connection = None
        self._pointer_unavailable = False
        self.prepare_taskbar_hints()

    def prepare_taskbar_hints(self):
        # Set these before mapping: Plasma's task model can cache the first
        # window state and keep an entry after a later client message.
        try:
            from Xlib import X, Xatom, display
            connection = display.Display()
            window = connection.create_resource_object("window", int(self.winId()))
            atoms = [connection.intern_atom("_NET_WM_STATE_SKIP_TASKBAR"),
                     connection.intern_atom("_NET_WM_STATE_SKIP_PAGER")]
            window.change_property(connection.intern_atom("_NET_WM_STATE"),
                                   Xatom.ATOM, 32, atoms, X.PropModeReplace)
            connection.flush()
            connection.close()
        except Exception:
            pass

    def pointer_local(self) -> QPointF:
        # XWayland's global coordinates are inconsistent across monitors with
        # different scales. Query this window directly in physical pixels.
        if not self._pointer_unavailable:
            try:
                if self._pointer_connection is None:
                    from Xlib import display
                    self._pointer_connection = display.Display()
                window = self._pointer_connection.create_resource_object("window", int(self.winId()))
                pointer = window.query_pointer()
                scale = self.devicePixelRatioF()
                return QPointF(pointer.win_x/scale, pointer.win_y/scale)
            except Exception:
                self._pointer_unavailable = True
        return QPointF(self.mapFromGlobal(QCursor.pos()))

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.exclude_from_taskbar)

    def exclude_from_taskbar(self):
        # Qt.Tool alone still appears in KWin's task switcher under XWayland.
        from Xlib import X, display, protocol
        connection = display.Display()
        state = connection.intern_atom("_NET_WM_STATE")
        skip_bar = connection.intern_atom("_NET_WM_STATE_SKIP_TASKBAR")
        skip_pager = connection.intern_atom("_NET_WM_STATE_SKIP_PAGER")
        window = connection.create_resource_object("window", int(self.winId()))
        request = protocol.event.ClientMessage(
            window=window, client_type=state,
            data=(32, [1, skip_bar, skip_pager, 1, 0]))
        connection.screen().root.send_event(
            request, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        connection.flush()
        connection.close()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(BORDER, 1))
        p.setBrush(QColor(8, 19, 16, 148))
        p.drawRoundedRect(self.rect().adjusted(1, 1, -2, -2), 4, 4)
        p.setPen(QPen(QColor(72, 145, 105, 24), 1))
        for x in range(8, self.width()-2, 16):
            p.drawLine(x, 3, x, self.height()-4)
        for y in range(8, self.height()-2, 16):
            p.drawLine(3, y, self.width()-4, y)

    def reveal(self):
        self.setWindowOpacity(0)
        self.show()
        self.raise_()
        self._animation = QPropertyAnimation(self, b"windowOpacity")
        self._animation.setDuration(260)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.start()

    def glide_to(self, target: QPoint):
        if self.pos() == target:
            return
        if not self.isVisible():
            self.move(target)
            return
        if self._move_animation:
            self._move_animation.stop()
        self._move_animation = QPropertyAnimation(self, b"pos")
        self._move_animation.setDuration(260)
        self._move_animation.setStartValue(self.pos())
        self._move_animation.setEndValue(target)
        self._move_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._move_animation.start()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)


class FluidPopup(Chrome):
    def __init__(self, values):
        super().__init__(336, 210, pass_through=True)
        self.values = values
        self.fluid = FlipFluid(values["particle_count"], values["flip_blend"])
        self.frame = self.fluid.image(values["halftone"], self.render_size())
        self.last_local = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.advance)
        self.timer.setInterval(33)

    def render_size(self):
        ratio = self.devicePixelRatioF()
        return (round((self.width()-18)*ratio), round((self.height()-59)*ratio))

    def showEvent(self, event):
        self.timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def advance(self):
        local = self.pointer_local()
        rect = QRect(8, 33, self.width()-16, self.height()-57)
        if QRectF(rect).contains(local):
            px = (local.x()-rect.x()) / rect.width() * self.fluid.w
            py = (local.y()-rect.y()) / rect.height() * self.fluid.h
            dx = local.x()-self.last_local.x() if self.last_local else 0
            dy = local.y()-self.last_local.y() if self.last_local else 0
            vx = dx*self.fluid.w/rect.width()
            vy = dy*self.fluid.h/rect.height()
            self.fluid.set_obstacle(px, py, vx, vy)
            if self.last_local is not None:
                self.fluid.add_impulse(px, py, vx*7, vy*7)
            self.last_local = local
        else:
            self.fluid.obstacle = None
            self.last_local = None
        self.fluid.step()
        self.frame = self.fluid.image(self.values["halftone"], self.render_size())
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(ACCENT)
        p.setFont(QFont("monospace", 9, QFont.Weight.Bold))
        p.drawText(10, 22, "FLUID / FLIP")
        p.setPen(QColor("#679979"))
        p.setFont(QFont("monospace", 7))
        p.drawText(self.width()-118, 21, f"N={len(self.fluid.x)}")
        rect = QRect(8, 33, self.width()-16, self.height()-57)
        p.setPen(QPen(QColor(87, 180, 124, 120), 1))
        p.drawRect(rect)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.drawImage(rect.adjusted(1, 1, -1, -1), self.frame)
        p.setPen(QColor("#6fae84"))
        p.drawText(10, self.height()-7, f"FLIP {self.fluid.blend:.2f}")
        p.drawText(self.width()-83, self.height()-7,
                   "HALFTONE" if self.values["halftone"] else "SMOOTH")


class SystemPopup(Chrome):
    def __init__(self):
        super().__init__(214, 145, pass_through=True)
        self.cpu = 0
        self.mem = 0
        self.disk = 0
        self.hover_row = -1
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.setInterval(1000)
        self.pointer_timer = QTimer(self)
        self.pointer_timer.timeout.connect(self.track_pointer)
        self.pointer_timer.setInterval(50)
        psutil.cpu_percent(interval=None)
        self.refresh()

    def showEvent(self, event):
        self.timer.start()
        self.pointer_timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self.timer.stop()
        self.pointer_timer.stop()
        super().hideEvent(event)

    def refresh(self):
        self.cpu = psutil.cpu_percent(interval=None)
        self.mem = psutil.virtual_memory().percent
        self.disk = psutil.disk_usage("/").percent
        self.update()

    def track_pointer(self):
        pos = self.pointer_local()
        row = (pos.y()-40) // 27 if 8 <= pos.x() < self.width()-8 else -1
        self.hover_row = row if 0 <= row <= 2 else -1
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(QFont("monospace", 9, QFont.Weight.Bold))
        p.setPen(ACCENT)
        p.drawText(10, 22, "SYSTEM / LIVE")
        p.setFont(QFont("monospace", 8))
        for row, (label, value) in enumerate((('CPU', self.cpu), ('RAM', self.mem), ('DISK', self.disk))):
            y = 54 + row*27
            p.setPen(ACCENT if row == self.hover_row else QColor("#aedac0"))
            p.drawText(10, y, label)
            p.drawText(166, y, f"{value:3.0f}%")
            p.setPen(QPen(QColor("#315c41"), 1))
            p.drawRect(9, y+4, 195, 5)
            p.fillRect(11, y+6, int(191*value/100), 2,
                       QColor("#c5ffcf") if row == self.hover_row else QColor("#8effaa"))
        p.setPen(QColor("#6fae84"))
        p.drawText(10, 137, "LIVE TELEMETRY")


class CursorFieldPopup(Chrome):
    """A damped halftone lattice displaced by the global pointer."""

    def __init__(self):
        super().__init__(260, 184, pass_through=True)
        self.area = QRect(10, 34, 240, 130)
        xs, ys = np.meshgrid(np.linspace(21, 239, 17), np.linspace(45, 151, 9))
        self.rest = np.column_stack((xs.ravel(), ys.ravel()))
        self.offset = np.zeros_like(self.rest)
        self.velocity = np.zeros_like(self.rest)
        self.pointer = None
        self.started = time.monotonic()
        self.last_tick = self.started
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self.advance)

    def showEvent(self, event):
        self.last_tick = time.monotonic()
        self.timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def advance(self):
        now = time.monotonic()
        dt = min(now-self.last_tick, 0.05)
        self.last_tick = now
        cursor = self.pointer_local()
        self.pointer = np.array([cursor.x(), cursor.y()]) if QRectF(self.area).contains(cursor) else None
        phase = now-self.started
        idle = np.column_stack((np.sin(self.rest[:, 1]*0.056+phase*1.2),
                                np.cos(self.rest[:, 0]*0.046+phase*0.9))) * 0.8
        target = idle
        if self.pointer is not None:
            delta = self.rest-self.pointer
            distance = np.maximum(np.linalg.norm(delta, axis=1), 0.01)
            influence = np.maximum(0, 1-distance/62)**2
            target = idle + delta/distance[:, None] * (18*influence)[:, None]
        # Stable spring response independent of the timer's exact cadence.
        self.velocity += (target-self.offset) * (95*dt)
        self.velocity *= math.exp(-11*dt)
        self.offset += self.velocity*dt
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(ACCENT)
        p.setFont(QFont("monospace", 9, QFont.Weight.Bold))
        p.drawText(10, 22, "CURSOR / FIELD")
        p.setPen(QPen(QColor(87, 180, 124, 95), 1))
        p.drawRect(self.area)
        p.save()
        p.setClipRect(self.area.adjusted(1, 1, -1, -1))
        displacement = np.linalg.norm(self.offset, axis=1)
        for index, (rest, offset) in enumerate(zip(self.rest, self.offset)):
            x, y = rest+offset
            radius = 0.9 + 0.38*((index % 4) == 0) + min(displacement[index]/10, 1.2)
            alpha = min(245, round(115+displacement[index]*14))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(150, 244, 176, alpha))
            p.drawEllipse(QPointF(float(x), float(y)), radius, radius)
        if self.pointer is not None:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(173, 255, 194, 130), 1))
            p.drawEllipse(QPointF(*self.pointer), 10, 10)
            p.setPen(QPen(QColor(95, 207, 132, 55), 1))
            p.drawEllipse(QPointF(*self.pointer), 28, 28)
        p.restore()
        p.setPen(QColor("#6fae84"))
        p.setFont(QFont("monospace", 7))
        p.drawText(10, 178, "HALFTONE / 153 NODES")


class SignalScopePopup(Chrome):
    """Two live traces: CPU utilization and aggregate network throughput."""

    def __init__(self):
        super().__init__(260, 174, pass_through=True)
        self.cpu = 0.0
        self.network_rate = 0.0
        self.network_peak = 65536.0
        self.cpu_history = [0.0]*118
        self.network_history = [0.0]*118
        self.last_io = psutil.net_io_counters()
        self.last_sample = time.monotonic()
        psutil.cpu_percent(interval=None)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.sample)

    def showEvent(self, event):
        self.last_io = psutil.net_io_counters()
        self.last_sample = time.monotonic()
        psutil.cpu_percent(interval=None)
        self.timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def sample(self):
        now = time.monotonic()
        elapsed = max(now-self.last_sample, 0.001)
        io = psutil.net_io_counters()
        delta = max(0, io.bytes_recv-self.last_io.bytes_recv) + max(0, io.bytes_sent-self.last_io.bytes_sent)
        self.network_rate = delta/elapsed
        self.cpu = psutil.cpu_percent(interval=None)
        self.network_peak = max(65536, self.network_rate, self.network_peak*0.992)
        self.cpu_history.pop(0)
        self.cpu_history.append(self.cpu/100)
        self.network_history.pop(0)
        self.network_history.append(min(1, self.network_rate/self.network_peak))
        self.last_io = io
        self.last_sample = now
        self.update()

    @staticmethod
    def trace(painter, values, rect, color):
        points = QPolygonF([QPointF(rect.left()+i*(rect.width()/(len(values)-1)),
                                    rect.bottom()-value*rect.height())
                            for i, value in enumerate(values)])
        painter.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 38), 5))
        painter.drawPolyline(points)
        painter.setPen(QPen(color, 1.35))
        painter.drawPolyline(points)

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(ACCENT)
        p.setFont(QFont("monospace", 9, QFont.Weight.Bold))
        p.drawText(10, 22, "SIGNAL / SCOPE")
        upper = QRectF(40, 37, 210, 51)
        lower = QRectF(40, 100, 210, 51)
        p.save()
        p.setClipRect(QRectF(8, 34, 244, 122))
        for rect in (upper, lower):
            p.setPen(QPen(QColor(63, 132, 89, 95), 1))
            p.drawRect(rect)
            p.setPen(QPen(QColor(63, 132, 89, 38), 1))
            for fraction in (0.25, 0.5, 0.75):
                x = rect.left()+rect.width()*fraction
                y = rect.top()+rect.height()*fraction
                p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
                p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        self.trace(p, self.cpu_history, upper, QColor("#a8ffb7"))
        self.trace(p, self.network_history, lower, QColor("#66dca6"))
        p.restore()
        p.setPen(QColor("#b9ebc7"))
        p.setFont(QFont("monospace", 7, QFont.Weight.Bold))
        p.drawText(10, 54, "CPU")
        p.drawText(10, 117, "NET")
        p.setPen(QColor("#6fae84"))
        p.drawText(10, 168, f"CPU {self.cpu:.0f}%")
        p.drawText(145, 168, f"NET {self.network_rate/1024:.0f} KiB/s")


class GlobeRenderer:
    """Software-rendered orthographic Earth using Natural Earth land polygons."""

    def __init__(self):
        data = json.loads(Path(__file__).with_name("land-110m.geojson").read_text())
        width, height = 720, 360
        mask = QImage(width, height, QImage.Format.Format_Grayscale8)
        mask.fill(0)
        painter = QPainter(mask)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(Qt.GlobalColor.white)
        for feature in data["features"]:
            path = QPainterPath()
            path.setFillRule(Qt.FillRule.OddEvenFill)
            for ring in feature["geometry"]["coordinates"]:
                polygon = QPolygonF([QPointF((lon+180)*2, (90-lat)*2)
                                     for lon, lat in ring])
                path.addPolygon(polygon)
            painter.drawPath(path)
        painter.end()
        self.land = np.frombuffer(mask.bits().asstring(mask.bytesPerLine()*height),
                                  dtype=np.uint8).reshape(height, mask.bytesPerLine())[:, :width].copy()
        self.land_points = np.argwhere(self.land > 127)

    @staticmethod
    def project(latitude: float, longitude: float, angle: float):
        relative = longitude-angle
        x = math.cos(latitude)*math.sin(relative)
        world_z = math.cos(latitude)*math.cos(relative)
        world_y = math.sin(latitude)
        tilt = math.radians(23.4)
        y = world_y*math.cos(tilt)-world_z*math.sin(tilt)
        depth = world_y*math.sin(tilt)+world_z*math.cos(tilt)
        return x, y, depth

    def image(self, angle: float, out_size: tuple[int, int]) -> QImage:
        width, height = out_size
        xx = (np.arange(width, dtype=np.float32)+0.5-width/2)/(width/2)
        yy = -(np.arange(height, dtype=np.float32)+0.5-height/2)/(height/2)
        x, y = np.broadcast_arrays(xx[None, :], yy[:, None])
        radius2 = x*x+y*y
        z = np.sqrt(np.clip(1-radius2, 0, 1))
        tilt = math.radians(23.4)
        world_y = y*math.cos(tilt)+z*math.sin(tilt)
        world_z = z*math.cos(tilt)-y*math.sin(tilt)
        latitude = np.arcsin(np.clip(world_y, -1, 1))
        longitude = (np.arctan2(x, world_z)+angle+math.pi)%(2*math.pi)-math.pi
        ix = ((longitude+math.pi)/(2*math.pi)*self.land.shape[1]).astype(np.int32) % self.land.shape[1]
        iy = np.clip(((math.pi/2-latitude)/math.pi*self.land.shape[0]).astype(np.int32),
                     0, self.land.shape[0]-1)
        land = self.land[iy, ix] > 127
        coast = land & (~np.roll(land, 1, 0) | ~np.roll(land, -1, 0)
                        | ~np.roll(land, 1, 1) | ~np.roll(land, -1, 1))
        light = np.clip(0.28 + 0.72*(0.28*x+0.25*y+0.88*z), 0.16, 1.0)
        bayer = np.array([[0, 8, 2, 10], [12, 4, 14, 6],
                          [3, 11, 1, 9], [15, 7, 13, 5]], dtype=np.float32)/16
        by, bx = np.indices((height, width))
        shade = light*(0.91 + 0.09*(bayer[by%4, bx%4] < light))
        grid_lon = np.abs((np.degrees(longitude)+15)%30-15) < 0.55
        grid_lat = np.abs((np.degrees(latitude)+15)%30-15) < 0.55
        graticule = (grid_lon | grid_lat) & (radius2 < 1)
        rgb = np.empty((height, width, 4), dtype=np.uint8)
        for channel, (ocean, earth) in enumerate(((15, 92), (89, 192), (85, 122))):
            value = np.where(land, earth, ocean)*shade
            value += coast*40 + graticule*18
            rgb[:, :, channel] = np.clip(value, 0, 255).astype(np.uint8)
        coverage = np.clip((1-np.sqrt(radius2))*min(width, height)/2, 0, 1)
        rgb[:, :, 3] = (coverage*232).astype(np.uint8)
        rgb = np.ascontiguousarray(rgb)
        return QImage(rgb.data, width, height, rgb.strides[0],
                      QImage.Format.Format_RGBA8888).copy()


class GlobePopup(Chrome):
    def __init__(self):
        super().__init__(260, 291, pass_through=True)
        self.renderer = GlobeRenderer()
        self.started = time.monotonic()
        self.angle = 0.0
        self.rng = np.random.default_rng()
        self.pings = []
        self.next_ping = self.started + 1.2
        self.frame = self.renderer.image(0, self.render_size())
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self.advance)

    def render_size(self):
        side = round(224*self.devicePixelRatioF())
        return (side, side)

    def showEvent(self, event):
        self.timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def advance(self):
        now = time.monotonic()
        self.angle = (now-self.started)*0.22
        if now >= self.next_ping:
            self.spawn_ping(now)
            self.next_ping = now + self.rng.uniform(1.3, 3.2)
        self.pings = [ping for ping in self.pings if now-ping[2] < 1.8]
        self.frame = self.renderer.image(self.angle, self.render_size())
        self.update()

    def spawn_ping(self, now):
        points = self.renderer.land_points
        for _ in range(64):
            iy, ix = points[self.rng.integers(len(points))]
            latitude = math.pi/2-(iy+0.5)/self.renderer.land.shape[0]*math.pi
            longitude = (ix+0.5)/self.renderer.land.shape[1]*2*math.pi-math.pi
            if self.renderer.project(latitude, longitude, self.angle)[2] > 0.32:
                self.pings.append((latitude, longitude, now))
                return

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(ACCENT)
        painter.setFont(QFont("monospace", 9, QFont.Weight.Bold))
        painter.drawText(10, 22, "GLOBE / EARTH")
        painter.setPen(QColor("#679979"))
        painter.setFont(QFont("monospace", 7))
        painter.drawText(196, 21, "23.4 DEG")
        rect = QRect(18, 40, 224, 224)
        painter.setPen(QPen(QColor(87, 180, 124, 70), 1))
        painter.drawEllipse(rect.adjusted(-2, -2, 2, 2))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(rect, self.frame)
        painter.save()
        sphere_clip = QPainterPath()
        sphere_clip.addEllipse(QRectF(rect))
        painter.setClipPath(sphere_clip)
        now = time.monotonic()
        for latitude, longitude, started in self.pings:
            x, y, depth = self.renderer.project(latitude, longitude, self.angle)
            if depth <= 0:
                continue
            cx = rect.center().x() + x*rect.width()/2
            cy = rect.center().y() - y*rect.height()/2
            age = now-started
            for delay in (0.0, 0.32):
                progress = (age/1.8-delay)/(1-delay)
                if 0 <= progress <= 1:
                    radius = 3 + 21*progress
                    alpha = round(205*(1-progress)**1.5)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.setPen(QPen(QColor(197, 255, 210, alpha), 1.5))
                    painter.drawEllipse(QPointF(cx, cy), radius, radius)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(211, 255, 220, round(220*max(0, 1-age/1.8))))
            painter.drawEllipse(QPointF(cx, cy), 2.3, 2.3)
        painter.restore()
        painter.setPen(QColor("#6fae84"))
        painter.drawText(10, 281, "NATURAL EARTH / 110M")


class ConfigDialog(QDialog):
    def __init__(self, values, changed, parent=None):
        super().__init__(parent)
        self.values = values
        self.changed = changed
        self.setWindowTitle("MALCIP / CONFIG")
        self.setMinimumWidth(380)
        self.setStyleSheet("QDialog{background:#09130f;color:#c7f8d4;} QLabel{color:#c7f8d4;}"
                           "QSlider::groove:horizontal{height:5px;background:#326346;}"
                           "QSlider::handle:horizontal{width:15px;background:#a8ffb7;margin:-5px 0;}")
        layout = QVBoxLayout(self)
        title = QLabel("MALCIP  //  CONFIG")
        title.setFont(QFont("monospace", 16, QFont.Weight.Bold))
        layout.addWidget(title)
        layout.addWidget(QLabel("Particles"))
        self.count = QSlider(Qt.Orientation.Horizontal)
        self.count.setRange(800, 4000)
        self.count.setSingleStep(100)
        self.count.setValue(values["particle_count"])
        layout.addWidget(self.count)
        layout.addWidget(QLabel("FLIP blend"))
        self.blend = QSlider(Qt.Orientation.Horizontal)
        self.blend.setRange(0, 100)
        self.blend.setValue(round(values["flip_blend"]*100))
        layout.addWidget(self.blend)
        self.halftone = QCheckBox("Halftone / ordered dither")
        self.halftone.setChecked(values["halftone"])
        layout.addWidget(self.halftone)
        apply = QPushButton("APPLY")
        apply.clicked.connect(self.apply)
        layout.addWidget(apply)

    def apply(self):
        self.values.update(particle_count=self.count.value(),
                           flip_blend=self.blend.value()/100,
                           halftone=self.halftone.isChecked())
        save_config(self.values)
        self.changed()
        self.accept()


class ControlBar(Chrome):
    def __init__(self, owner):
        super().__init__(475, 91)
        self.owner = owner
        self.hover_cell = -1
        self.setMouseTracking(True)
        self.visual_cell = 0.0
        self.target_cell = 0.0
        self.press_started = 0.0
        self.last_tick = 0.0
        self.selector_timer = QTimer(self)
        self.selector_timer.setInterval(16)
        self.selector_timer.timeout.connect(self.animate_selector)

    def cell(self, index):
        return QRect(135 + index*55, 32, 50, 50)

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QColor("#cfe3d1"))
        p.setFont(QFont("JetBrainsMono Nerd Font", 12, QFont.Weight.Bold))
        p.drawText(15, 29, "MALCIP")
        p.setPen(QColor("#76ab85"))
        p.setFont(QFont("JetBrainsMono Nerd Font", 7))
        p.drawText(16, 51, "A D / ARROWS")
        p.drawText(16, 65, "ENTER / E")
        p.drawText(16, 79, "SHIFT+ENTER")
        for i, (label, active) in enumerate((("FLUID", self.owner.fluid.isVisible()),
                                             ("SYSTEM", self.owner.system.isVisible()),
                                             ("GLOBE", self.owner.globe.isVisible()),
                                             ("FIELD", self.owner.field.isVisible()),
                                             ("SCOPE", self.owner.scope.isVisible()),
                                             ("CONFIG", False))):
            cell = self.cell(i)
            p.setBrush(QColor("#408f61" if active else "#002f18"))
            p.setPen(QPen(QColor("#76d191" if active or i == self.hover_cell else "#4fae78"),
                          2 if active else 1))
            p.drawRoundedRect(cell, 8, 8)
            p.setPen(QPen(QColor("#cfe3d1" if active else "#8fc39c"), 1.5))
            cx = cell.center().x()
            if i == 0:
                path = QPainterPath()
                path.moveTo(cx, 39)
                path.cubicTo(cx-4, 45, cx-9, 49, cx-9, 54)
                path.cubicTo(cx-9, 67, cx+9, 67, cx+9, 54)
                path.cubicTo(cx+9, 49, cx+4, 45, cx, 39)
                p.drawPath(path)
            elif i == 1:
                for j, height in enumerate((7, 13, 10)):
                    p.drawRect(cx-10+j*7, 60-height, 4, height)
            elif i == 2:
                p.drawEllipse(QRectF(cx-10, 42, 20, 20))
                p.drawEllipse(QRectF(cx-5, 42, 10, 20))
                p.drawLine(cx-10, 52, cx+10, 52)
            elif i == 3:
                for dx, dy, radius in ((-7,-7,1.5),(0,-7,1.5),(7,-7,1.5),
                                       (-7,0,1.5),(7,0,1.5),
                                       (-7,7,1.5),(0,7,1.5),(7,7,1.5)):
                    p.setBrush(QColor("#cfe3d1"))
                    p.drawEllipse(QPointF(cx+dx, 53+dy), radius, radius)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(cx, 53), 4, 4)
            elif i == 4:
                path = QPainterPath()
                path.moveTo(cx-11, 54)
                for dx, dy in ((-7,54),(-4,48),(-1,59),(3,43),(6,55),(11,55)):
                    path.lineTo(cx+dx, dy)
                p.drawPath(path)
            else:
                p.drawLine(cx-10, 47, cx+10, 47)
                p.drawLine(cx-10, 54, cx+10, 54)
                p.drawLine(cx-10, 61, cx+10, 61)
                for x, y in ((cx-3,47),(cx+5,54),(cx-5,61)):
                    p.setBrush(QColor("#cfe3d1"))
                    p.drawEllipse(x-2, y-2, 4, 4)
                    p.setBrush(Qt.BrushStyle.NoBrush)
            p.setFont(QFont("JetBrainsMono Nerd Font", 6))
            p.drawText(cell.adjusted(0, 32, 0, 0), Qt.AlignmentFlag.AlignHCenter, label)

        # Workspace Field's separate selection frame glides above the cells.
        selection = QRectF(self.cell(0).x() + self.visual_cell*55 - 1.5,
                           30.5, 53, 53)
        p.setBrush(QColor(79, 174, 120, 19))
        p.setPen(QPen(QColor(117, 209, 145, 235), 2.2))
        p.drawRoundedRect(selection, 10, 10)
        if self.press_started:
            progress = (time.monotonic() - self.press_started) / 0.165
            if progress < 1:
                inset = 3 + math.sin(progress*math.pi)*3.5
                cell = self.cell(int(self.target_cell))
                p.setBrush(QColor(0, 19, 11, int(85*math.sin(progress*math.pi))))
                p.setPen(QPen(QColor(158, 232, 179,
                                       int(158*math.sin(progress*math.pi))), 1.5))
                p.drawRoundedRect(QRectF(cell).adjusted(inset, inset, -inset, -inset), 7, 7)

    def select(self, index, pressed=False):
        if index < 0 or index > 5:
            return
        self.target_cell = float(index)
        if pressed:
            self.press_started = time.monotonic()
        self.last_tick = time.monotonic()
        if not self.selector_timer.isActive():
            self.selector_timer.start()
        self.update()

    def select_relative(self, step):
        self.select((int(self.target_cell)+step) % 6)

    def activate_selected(self):
        index = int(self.target_cell)
        self.select(index, pressed=True)
        (self.owner.toggle_fluid, self.owner.toggle_system,
         self.owner.toggle_globe, self.owner.toggle_field,
         self.owner.toggle_scope, self.owner.show_config)[index]()

    def animate_selector(self):
        now = time.monotonic()
        elapsed_ms = min(50, (now-self.last_tick)*1000)
        self.last_tick = now
        delta = self.target_cell-self.visual_cell
        if abs(delta) > 0.003:
            factor = 1-math.exp(-elapsed_ms/52)
            step = min(abs(delta)*factor, max(1, elapsed_ms*0.8)/55)
            self.visual_cell += math.copysign(step, delta)
        else:
            self.visual_cell = self.target_cell
        if self.visual_cell == self.target_cell and now-self.press_started >= 0.165:
            self.selector_timer.stop()
        self.update()

    def mouseMoveEvent(self, event):
        self.hover_cell = next((i for i in range(6) if self.cell(i).contains(event.pos())), -1)
        if self.hover_cell >= 0 and self.hover_cell != self.target_cell:
            self.select(self.hover_cell)
        self.update()

    def leaveEvent(self, event):
        self.hover_cell = -1
        self.update()

    def mousePressEvent(self, event):
        for i, action in enumerate((self.owner.toggle_fluid, self.owner.toggle_system,
                                    self.owner.toggle_globe, self.owner.toggle_field,
                                    self.owner.toggle_scope, self.owner.show_config)):
            if self.cell(i).contains(event.pos()):
                self.select(i, pressed=True)
                action()
                return


class Malcip(QObject):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.values = load_config()
        self.bar = ControlBar(self)
        self.fluid = FluidPopup(self.values)
        self.system = SystemPopup()
        self.globe = GlobePopup()
        self.field = CursorFieldPopup()
        self.scope = SignalScopePopup()
        self.server = QLocalServer()
        SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
        QLocalServer.removeServer(str(SOCKET_PATH))
        if not self.server.listen(str(SOCKET_PATH)):
            raise RuntimeError(f"Could not listen on {SOCKET_PATH}: {self.server.errorString()}")
        self.server.newConnection.connect(self.on_connection)
        app.aboutToQuit.connect(self.cleanup)
        app.installEventFilter(self)
        self.place()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress and isinstance(watched, QWidget):
            if isinstance(watched.window(), QDialog):
                return False
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.bar.select(5, pressed=True)
                self.show_config()
                return True
            if self.bar.isVisible() and key in (Qt.Key.Key_Left, Qt.Key.Key_Up, Qt.Key.Key_A):
                self.bar.select_relative(-1)
                return True
            if self.bar.isVisible() and key in (Qt.Key.Key_Right, Qt.Key.Key_Down, Qt.Key.Key_D):
                self.bar.select_relative(1)
                return True
            if self.bar.isVisible() and key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_E):
                self.bar.activate_selected()
                return True
            if key in (Qt.Key.Key_1, Qt.Key.Key_F):
                self.bar.select(0, pressed=True)
                self.toggle_fluid()
                return True
            if key in (Qt.Key.Key_2, Qt.Key.Key_S):
                self.bar.select(1, pressed=True)
                self.toggle_system()
                return True
            if key in (Qt.Key.Key_3, Qt.Key.Key_G):
                self.bar.select(2, pressed=True)
                self.toggle_globe()
                return True
            if key in (Qt.Key.Key_4, Qt.Key.Key_C):
                self.bar.select(3, pressed=True)
                self.toggle_field()
                return True
            if key in (Qt.Key.Key_5, Qt.Key.Key_V):
                self.bar.select(4, pressed=True)
                self.toggle_scope()
                return True
            if key == Qt.Key.Key_R:
                self.fluid.fluid = FlipFluid(self.values["particle_count"], self.values["flip_blend"])
                return True
            if key == Qt.Key.Key_H:
                self.values["halftone"] = not self.values["halftone"]
                save_config(self.values)
                return True
            if key == Qt.Key.Key_Escape:
                self.bar.hide()
                return True
        return False

    def screen(self):
        requested = os.environ.get("MALCIP_SCREEN", "")
        return next((s for s in self.app.screens() if s.name() == requested),
                    self.app.primaryScreen())

    def place(self):
        bounds = self.screen().availableGeometry()
        center_x = bounds.x() + bounds.width()//2
        margin = 20
        self.bar.move(center_x-self.bar.width()//2, bounds.y()+margin)
        visible = [popup for popup in (self.fluid, self.system, self.globe,
                                       self.field, self.scope) if popup.isVisible()]
        column_right = bounds.right()-margin
        y = bounds.y()+margin
        column_width = 0
        for popup in visible:
            # Give the two small instruments separate columns. On mixed-scale
            # XWayland desktops KWin can clamp the final stacked window upward.
            if popup is self.scope and self.field.isVisible():
                column_right -= column_width+12
                y = bounds.y()+margin
                column_width = 0
            if y > bounds.y()+margin and y+popup.height() > bounds.bottom()-margin:
                column_right -= column_width+12
                y = bounds.y()+margin
                column_width = 0
            popup.glide_to(QPoint(column_right-popup.width(), y))
            column_width = max(column_width, popup.width())
            y += popup.height()+12

    def toggle_bar(self):
        self.place()
        if self.bar.isVisible():
            self.bar.hide()
        else:
            self.bar.reveal()
            self.bar.activateWindow()

    def hide_all(self):
        self.bar.hide()
        self.fluid.hide()
        self.system.hide()
        self.globe.hide()
        self.field.hide()
        self.scope.hide()

    def toggle_popup(self, popup):
        if popup.isVisible():
            popup.hide()
        else:
            bounds = self.screen().availableGeometry()
            popup.move(bounds.right()-popup.width()-20, bounds.y()+20)
            popup.reveal()
        self.place()
        self.bar.update()

    def toggle_fluid(self):
        self.toggle_popup(self.fluid)

    def toggle_system(self):
        self.toggle_popup(self.system)

    def toggle_globe(self):
        self.toggle_popup(self.globe)

    def toggle_field(self):
        self.toggle_popup(self.field)

    def toggle_scope(self):
        self.toggle_popup(self.scope)

    def show_config(self):
        ConfigDialog(self.values, self.apply_config, self.bar).exec()

    def apply_config(self):
        self.fluid.fluid.resize_particles(self.values["particle_count"])
        self.fluid.fluid.blend = self.values["flip_blend"]
        self.fluid.update()

    def on_connection(self):
        while self.server.hasPendingConnections():
            client = self.server.nextPendingConnection()
            if client.waitForReadyRead(500):
                command = bytes(client.readAll()).decode().strip()
                if command == "toggle":
                    self.toggle_bar()
                elif command == "fluid":
                    self.toggle_fluid()
                elif command == "system":
                    self.toggle_system()
                elif command == "globe":
                    self.toggle_globe()
                elif command == "field":
                    self.toggle_field()
                elif command == "scope":
                    self.toggle_scope()
                elif command == "config":
                    self.show_config()
            client.disconnectFromServer()

    def cleanup(self):
        self.server.close()
        QLocalServer.removeServer(str(SOCKET_PATH))


def send_command(command: str) -> bool:
    client = QLocalSocket()
    client.connectToServer(str(SOCKET_PATH))
    if not client.waitForConnected(500):
        return False
    client.write(command.encode())
    client.flush()
    client.waitForBytesWritten(500)
    client.disconnectFromServer()
    return True


def main():
    parser = argparse.ArgumentParser(description="MALCIP desktop popup utility")
    parser.add_argument("command", nargs="?", default="toggle",
                        choices=("toggle", "fluid", "system", "globe", "field", "scope", "config"))
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP)
    if send_command(args.command):
        return 0
    owner = Malcip(app)
    owner.bar.reveal()
    owner.toggle_fluid()
    owner.bar.activateWindow()
    if args.command == "system":
        owner.toggle_system()
    elif args.command == "globe":
        owner.toggle_globe()
    elif args.command == "field":
        owner.toggle_field()
    elif args.command == "scope":
        owner.toggle_scope()
    elif args.command == "config":
        QTimer.singleShot(0, owner.show_config)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
