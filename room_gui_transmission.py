#!/usr/bin/env python3
"""Connected-room transmission editor. Launch with: python room_gui_transmission.py"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys

from PySide6 import QtCore as C, QtGui as G, QtWidgets as W

from report_rendering import add_report_controls, load_report_controls, read_report_controls

from simulate_transmission import validate_config
from transmission_model import LINKS, LINK_NAMES
from connected_geometry import (PLAN_FIELDS, SPACES, SPACE_NAMES, dimensions, locate_space,
                                plan_segments, resize_positions, space_boxes, validate_plan)

WALLS = ("wall", "floor", "ceiling", "door")


PROJECT = Path(__file__).resolve().parent
COLORS = ("#2563eb", "#e87919", "#059669", "#9333ea", "#dc2626")
TITLE = "Room acoustics studio · transmission"


def clamp_position(position, dimensions):
    """Keep a marker strictly inside the room, including very small rooms."""
    return [max(min(0.01, d / 10), min(p, d - min(0.01, d / 10)))
            for p, d in zip(position, dimensions)]



def report_command(config_path, output_path):
    return [sys.executable, "-u", str(PROJECT / "simulate_transmission.py"),
            "--config", str(config_path), "--output", str(output_path)]


def prepare_qt_plugins():
    """Qt's scanner skips Finder-hidden plugins in some managed macOS projects.

    Clear that flag only for plugins inside this project's environment. Leave
    external Python installations and the hidden environment directory alone.
    """
    if sys.platform != "darwin":
        return
    plugins = Path(C.QLibraryInfo.path(C.QLibraryInfo.LibraryPath.PluginsPath))
    if PROJECT not in plugins.resolve().parents:
        return
    for plugin in plugins.rglob("*.dylib"):
        flags = plugin.stat().st_flags
        if flags & stat.UF_HIDDEN:
            os.chflags(plugin, flags & ~stat.UF_HIDDEN)


def apply_light_theme(app):
    """Keep widget backgrounds and text consistent with macOS dark mode enabled."""
    app.setStyle("Fusion")
    palette = G.QPalette()
    for role, color in {
        "Window": "#ffffff", "WindowText": "#101828",
        "Base": "#ffffff", "AlternateBase": "#f8fafc",
        "Text": "#101828", "Button": "#f8fafc", "ButtonText": "#101828",
        "ToolTipBase": "#ffffff", "ToolTipText": "#101828",
        "Highlight": "#e9f0ff", "HighlightedText": "#1d4ed8",
        "PlaceholderText": "#667085", "Light": "#ffffff", "Midlight": "#eef2f6",
        "Mid": "#d0d5dd", "Dark": "#98a2b3", "Shadow": "#667085",
        "Link": "#1d4ed8", "LinkVisited": "#6941c6",
    }.items():
        palette.setColor(getattr(G.QPalette.ColorRole, role), G.QColor(color))
    for role in (G.QPalette.ColorRole.Text, G.QPalette.ColorRole.WindowText,
                 G.QPalette.ColorRole.ButtonText):
        palette.setColor(G.QPalette.ColorGroup.Disabled, role, G.QColor("#667085"))
    app.setPalette(palette)


class RoomPlan(W.QWidget):
    selected = C.Signal(str)
    moved = C.Signal(str, float, float)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(440, 400)
        self.setCursor(C.Qt.CursorShape.CrossCursor)
        self.cfg = None
        self.active = "speaker:0"
        self.editable = True
        self.dragging = None
        self.marker_pixels = {}

    def to_pixel(self, x, y):
        return C.QPointF(self.origin_x + x * self.scale, self.origin_y - y * self.scale)

    def paintEvent(self, event):
        painter = G.QPainter(self)
        painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), G.QColor("#f8fafc"))
        if self.cfg is None:
            return
        width, length, _ = self.cfg["room"]["dimensions_m"]
        self.scale = min((self.width() - 150) / width, (self.height() - 120) / length)
        self.origin_x = (self.width() - width * self.scale) / 2
        self.origin_y = (self.height() + length * self.scale) / 2
        top_left, bottom_right = self.to_pixel(0, length), self.to_pixel(width, 0)
        room = C.QRectF(top_left, bottom_right)
        painter.fillRect(room, G.QColor("#e9eef5"))
        for lower, upper in space_boxes(self.cfg["floor_plan"]).values():
            painter.fillRect(C.QRectF(self.to_pixel(lower[0], upper[1]), self.to_pixel(upper[0], lower[1])),
                             G.QColor("white"))
        spacing = 10 ** math.floor(math.log10(max(width, length) / 6))
        while max(width, length) / spacing > 16:
            spacing *= 2
        painter.setPen(G.QPen(G.QColor("#e9eef5"), 1))
        for axis, limit in ((0, width), (1, length)):
            for index in range(1, int(limit / spacing) + 1):
                value = spacing * index
                if value >= limit:
                    break
                start = self.to_pixel(value, 0) if axis == 0 else self.to_pixel(0, value)
                end = self.to_pixel(value, length) if axis == 0 else self.to_pixel(width, value)
                painter.drawLine(start, end)
        painter.setPen(G.QPen(G.QColor("#344054"), 2.5))
        key = json.dumps(self.cfg["floor_plan"], sort_keys=True)
        if getattr(self, "_segment_key", None) != key:
            self._segment_key = key
            self._walls, self._doors = plan_segments(self.cfg["floor_plan"])
        for segment in self._walls:
            painter.drawLine(self.to_pixel(*segment[0]), self.to_pixel(*segment[1]))
        for _, segment, opened in self._doors:
            painter.setPen(G.QPen(G.QColor("#059669" if opened else "#dc2626"), 4,
                                 C.Qt.PenStyle.DashLine if opened else C.Qt.PenStyle.SolidLine))
            painter.drawLine(self.to_pixel(*segment[0]), self.to_pixel(*segment[1]))

        def text(x, y, value, color="#475467", bold=False):
            font = painter.font()
            font.setPointSize(10)
            font.setBold(bold)
            painter.setFont(font)
            painter.setPen(G.QColor(color))
            painter.drawText(C.QRectF(x - 100, y - 18, 200, 36), C.Qt.AlignmentFlag.AlignCenter, value)

        text(room.center().x(), room.top() - 28, f"North · width {width:g} m")
        text(room.center().x(), room.bottom() + 28, "South · y = 0")
        text(room.left() - 38, room.center().y(), f"West\n{length:g} m")
        text(room.right() + 32, room.center().y(), "East")
        for key, (lower, upper) in space_boxes(self.cfg["floor_plan"]).items():
            center = self.to_pixel((lower[0] + upper[0]) / 2, upper[1] - 0.25)
            text(center.x(), center.y(), SPACE_NAMES[key], "#667085")
        self.marker_pixels = {}
        for index, speaker in enumerate(self.cfg["speakers"]):
            point = self.to_pixel(*speaker["position_m"][:2])
            color = G.QColor(COLORS[index % len(COLORS)])
            painter.setPen(G.QPen(G.QColor("#cbd5e1"), 1, C.Qt.PenStyle.DashLine))
            key = f"speaker:{index}"
            self.marker_pixels[key] = point
            if key == self.active:
                painter.setBrush(C.Qt.BrushStyle.NoBrush)
                painter.setPen(G.QPen(color, 2))
                painter.drawEllipse(point, 16, 16)
            painter.setPen(G.QPen(G.QColor("white"), 2))
            painter.setBrush(color)
            painter.drawRoundedRect(C.QRectF(point.x() - 8, point.y() - 8, 16, 16), 3, 3)
            text(point.x(), point.y() - 28, speaker["name"], color.name(), True)
        for index, listener in enumerate(self.cfg["listeners"]):
            point = self.to_pixel(*listener["position_m"][:2])
            key = f"listener:{index}"
            color = COLORS[(index + 2) % len(COLORS)]
            self.marker_pixels[key] = point
            if self.active == key:
                painter.setBrush(C.Qt.BrushStyle.NoBrush)
                painter.setPen(G.QPen(G.QColor(color), 2))
                painter.drawEllipse(point, 18, 18)
            painter.setPen(G.QPen(G.QColor("white"), 2))
            painter.setBrush(G.QColor(color))
            painter.drawPolygon(G.QPolygonF([point + C.QPointF(0, -11), point + C.QPointF(11, 0),
                                             point + C.QPointF(0, 11), point + C.QPointF(-11, 0)]))
            text(point.x(), point.y() - 29, listener["name"], color, True)

    def mousePressEvent(self, event):
        if not self.editable or not self.marker_pixels or event.button() != C.Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        key = min(self.marker_pixels, key=lambda k: (pos - self.marker_pixels[k]).manhattanLength())
        offset = pos - self.marker_pixels[key]
        if math.hypot(offset.x(), offset.y()) <= 24:
            self.dragging = (key, offset)
            self.selected.emit(key)

    def mouseMoveEvent(self, event):
        if self.dragging is None or not self.editable:
            return
        key, offset = self.dragging
        point = event.position() - offset
        self.moved.emit(key, (point.x() - self.origin_x) / self.scale,
                        (self.origin_y - point.y()) / self.scale)

    def mouseReleaseEvent(self, event):
        self.dragging = None


class RoomEditor(W.QMainWindow):
    def __init__(self, config_path=PROJECT / "room_config_transmission.json"):
        super().__init__()
        apply_light_theme(W.QApplication.instance())
        self.cfg = validate_config(json.loads(Path(config_path).read_text()))
        self.config_path = Path(config_path)
        self.selected = "speaker:0"
        self.syncing = False
        self.form_valid = True
        self.revision = self.saved_revision = 0
        self.report_revision = None
        self.process = self.last_report = self.run_dir = self.log_handle = None
        self.cancelled = False
        self.setWindowTitle(TITLE)
        self.resize(1220, 860)
        self.setMinimumSize(1000, 740)
        self.timer = C.QTimer(self)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self._poll_report)
        self._build()
        self._load_fields()
        G.QShortcut(G.QKeySequence.StandardKey.Save, self, activated=self.save_config)

    def field(self):
        field = W.QLineEdit()
        field.textChanged.connect(self._edit)
        return field

    def button(self, text, callback):
        button = W.QPushButton(text)
        button.clicked.connect(callback)
        return button

    def label(self, text):
        label = W.QLabel(text)
        label.setWordWrap(True)
        return label

    def group(self, title, layout):
        group = W.QGroupBox(title)
        group.setLayout(layout)
        return group

    def _build(self):
        self.setStyleSheet("""
            QWidget { background-color: #ffffff; color: #101828; font-size: 13px; }
            QMainWindow, QScrollArea, QTabWidget::pane { background: #ffffff; }
            QGroupBox { border: 1px solid #d0d5dd; border-radius: 6px; margin-top: 14px;
                        padding: 14px 8px 8px; font-weight: 600; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
            QLineEdit { background: white; border: 1px solid #d0d5dd; border-radius: 4px; padding: 6px; }
            QLineEdit:disabled { background: #f2f4f7; color: #667085; }
            QPushButton { background: #f8fafc; border: 1px solid #d0d5dd; border-radius: 5px; padding: 8px 12px; }
            QPushButton:hover { background: #eef2f6; }
            QPushButton:disabled { color: #667085; background: #f2f4f7; }
            QPushButton#primary { background: #2563eb; color: white; border: none; font-weight: 600; }
            QPushButton#primary:disabled { background: #e9eef8; color: #667085; }
            QListWidget { background: white; border: 1px solid #d0d5dd; border-radius: 4px; }
            QListWidget::item { padding: 5px; }
            QListWidget::item:selected { background: #e9f0ff; color: #1d4ed8; }
            QTabBar::tab { background: #eef2f6; color: #344054; padding: 9px 10px; }
            QTabBar::tab:selected { background: #ffffff; color: #101828; }
            QTabBar::tab:hover { background: #e9f0ff; }
        """)
        central = W.QWidget()
        self.setCentralWidget(central)
        outer = W.QVBoxLayout(central)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(12)
        header = W.QHBoxLayout()
        title = self.label("Room acoustics studio · transmission")
        title.setStyleSheet("font-size: 24px; font-weight: 700")
        header.addWidget(title, 1)
        header.addWidget(self.button("Save configuration…", self.save_config))
        header.addWidget(self.button("Load configuration…", self.load_config))
        outer.addLayout(header)
        body = W.QSplitter()
        outer.addWidget(body, 1)
        tabs = W.QTabWidget()
        tabs.setMinimumWidth(420)
        tabs.setMaximumWidth(490)
        body.addWidget(tabs)
        geometry, settings, plan_page, transmission_page = W.QWidget(), W.QWidget(), W.QWidget(), W.QWidget()
        for widget, name in ((geometry, "Positions"), (plan_page, "Rooms/doors"), (transmission_page, "Transmission"), (settings, "Report")):
            scroll = W.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(widget)
            scroll.setFrameShape(W.QFrame.Shape.NoFrame)
            tabs.addTab(scroll, name)
        geometry_layout, settings_layout = W.QVBoxLayout(geometry), W.QVBoxLayout(settings)
        plan_layout = W.QVBoxLayout(plan_page)
        self.plan_vars = {key: self.field() for key in PLAN_FIELDS}
        form = W.QFormLayout()
        labels = ("Room A width (m)", "Room A length (m)", "Room B width (m)", "Room B length (m)",
                  "Corridor depth (m)", "Ceiling height (m)", "Wall thickness (m)")
        for label, key in zip(labels, PLAN_FIELDS):
            form.addRow(label, self.plan_vars[key])
        plan_layout.addWidget(self.group("Room dimensions · enclosed corridor", form))
        self.door_vars, self.door_open = {}, {}
        for key in ("room_a", "room_b"):
            form = W.QFormLayout()
            self.door_vars[key] = {name: self.field() for name in ("offset_m", "width_m", "height_m")}
            for name, label in (("offset_m", "From west wall (m)"), ("width_m", "Door width (m)"),
                                ("height_m", "Door height (m)")):
                form.addRow(label, self.door_vars[key][name])
            check = self.door_open[key] = W.QCheckBox("Open · unchecked means closed")
            check.toggled.connect(self._edit)
            form.addRow(check)
            plan_layout.addWidget(self.group(SPACE_NAMES[key] + " door to corridor", form))
        plan_layout.addWidget(self.label("Closed doors block air paths. Their leakage is estimated separately in the Transmission tab. Diffraction is not modeled."))
        plan_layout.addStretch()
        geometry_layout.addWidget(self.label("Select a marker to edit or drag"))
        self.objects = W.QListWidget()
        self.objects.setMaximumHeight(130)
        self.objects.currentRowChanged.connect(self._select_row)
        geometry_layout.addWidget(self.objects)
        actions = W.QHBoxLayout()
        actions.addWidget(self.button("Add speaker", self.add_speaker))
        actions.addWidget(self.button("Add listener", self.add_listener))
        self.remove_button = self.button("Remove", self.remove_object)
        actions.addWidget(self.remove_button)
        geometry_layout.addLayout(actions)
        self.name_var = self.field()
        self.pos_vars = [self.field() for _ in range(3)]
        self.gain_var, self.delay_var = self.field(), self.field()
        form = W.QFormLayout()
        self.space_combo = W.QComboBox()
        for key in SPACES:
            self.space_combo.addItem(SPACE_NAMES[key], key)
        self.space_combo.currentIndexChanged.connect(self.change_space)
        form.addRow("Space", self.space_combo)
        for label, field in [("Name", self.name_var), ("Local x · from west (m)", self.pos_vars[0]),
                             ("Local y · from south (m)", self.pos_vars[1]), ("z · above floor (m)", self.pos_vars[2]),
                             ("Gain (dB)", self.gain_var), ("Delay (ms)", self.delay_var)]:
            form.addRow(label, field)
        geometry_layout.addWidget(self.group("Selected object", form))
        self.distance_label = self.label("")
        geometry_layout.addWidget(self.distance_label)
        geometry_layout.addWidget(self.label("Coordinates are local to the selected space. Resize rooms and toggle doors in Rooms and doors."))
        geometry_layout.addStretch()

        form = W.QFormLayout()
        self.wall_vars = {wall: self.field() for wall in WALLS}
        for wall, field in self.wall_vars.items():
            form.addRow(wall.capitalize(), field)
        form.addRow(self.label("0 = reflective · 1 = absorbing\nAlso accepts material names or band JSON."))
        settings_layout.addWidget(self.group("Surface energy absorption", form))
        self.fs_var, self.duration_var, self.order_var = self.field(), self.field(), self.field()
        self.wf_duration_var, self.wf_window_var = self.field(), self.field()
        form = W.QFormLayout()
        for label, field in [("Sample rate (Hz)", self.fs_var), ("Analysis length (s)", self.duration_var),
                             ("Reflection order", self.order_var), ("Waterfall time (s)", self.wf_duration_var),
                             ("Waterfall window (s)", self.wf_window_var)]:
            form.addRow(label, field)
        form.addRow(self.label("Hybrid simulation: use ISM order 0–4 (default 2).\nAnalysis length must cover waterfall time + window."))
        self.rays_var = self.field()
        form.addRow("Rays per source", self.rays_var)
        add_report_controls(self, form)
        settings_layout.addWidget(self.group("Report settings", form))
        settings_layout.addStretch()
        transmission_layout = W.QVBoxLayout(transmission_page)
        self.transmission_enabled = W.QCheckBox("Enable diffuse transmission estimate")
        self.transmission_enabled.toggled.connect(self._edit)
        transmission_layout.addWidget(self.transmission_enabled)
        transmission_layout.addWidget(self.label("Transmission loss (dB) by frequency band. Higher values mean less leakage. "
                                                "Enter 'blocked' for no transmission. Open doors use 0 dB regardless of this table."))
        self.tl_table = W.QTableWidget()
        self.tl_table.setMinimumHeight(255)
        self.tl_table.setAlternatingRowColors(True)
        self.tl_table.setWordWrap(True)
        self.tl_table.cellChanged.connect(self._edit)
        transmission_layout.addWidget(self.tl_table)
        transmission_layout.addWidget(self.label("Scroll horizontally for the higher frequency bands. Values are illustrative defaults, "
                                                "not measurements of a particular wall or door. Replace them with measured band data."))
        transmission_layout.addWidget(self.label("This is a separate room-average energy estimate. Listeners in the same room get "
                                                "the same diffuse result. It does not predict transmitted pressure phase, exact bass "
                                                "resonances, propagation delays, or flanking paths. Sources add as energy."))
        transmission_layout.addWidget(self.label("Surface absorption and transmission are separate: their fractions must sum to at most 1."))
        transmission_layout.addStretch()
        preview = W.QWidget()
        layout = W.QVBoxLayout(preview)
        layout.setContentsMargins(16, 4, 0, 0)
        heading = self.label("Room plan · top view")
        heading.setStyleSheet("font-size: 17px; font-weight: 600")
        layout.addWidget(heading)
        layout.addWidget(self.label("Drag markers between rooms. Green doors are open; red doors are closed."))
        self.canvas = RoomPlan()
        self.canvas.selected.connect(self.select)
        self.canvas.moved.connect(self.move_marker)
        layout.addWidget(self.canvas, 1)
        self.layout_label = self.label("")
        layout.addWidget(self.layout_label)
        body.addWidget(preview)
        body.setStretchFactor(1, 1)
        self.error_label = self.label("")
        self.error_label.setStyleSheet("color: #b42318")
        outer.addWidget(self.error_label)
        output = W.QHBoxLayout()
        output.addWidget(self.label("Report folder"))
        self.output_var = W.QLineEdit(str(PROJECT / "reports_transmission"))
        output.addWidget(self.output_var, 1)
        output.addWidget(self.button("Browse…", self.browse_output))
        outer.addLayout(output)
        footer = W.QHBoxLayout()
        self.create_button = self.button("Create report", self.create_report)
        self.create_button.setObjectName("primary")
        self.cancel_button = self.button("Cancel", self.cancel_report)
        self.cancel_button.setEnabled(False)
        self.open_button = self.button("Open PDF", self.open_report)
        self.open_button.setEnabled(False)
        for button in (self.create_button, self.cancel_button, self.open_button):
            footer.addWidget(button)
        footer.addStretch()
        self.progress = W.QProgressBar()
        self.progress.setMaximumWidth(160)
        self.progress.setTextVisible(False)
        self.progress.hide()
        footer.addWidget(self.progress)
        outer.addLayout(footer)
        self.status_label = self.label("Ready · edit your layout, then create a report.")
        outer.addWidget(self.status_label)

    def _speaker_index(self):
        return int(self.selected.split(":")[1]) if self.selected.startswith("speaker:") else None

    def _listener_index(self):
        return int(self.selected.split(":")[1]) if self.selected.startswith("listener:") else None

    def _position(self):
        index = self._speaker_index()
        return (self.cfg["listeners"][self._listener_index()]["position_m"] if index is None
                else self.cfg["speakers"][index]["position_m"])

    def _load_fields(self):
        self.syncing = True
        for key, field in self.plan_vars.items():
            field.setText(str(self.cfg["floor_plan"][key]))
        for key in ("room_a", "room_b"):
            for name, field in self.door_vars[key].items():
                field.setText(str(self.cfg["floor_plan"]["doors"][key][name]))
            self.door_open[key].setChecked(self.cfg["floor_plan"]["doors"][key]["open"])
        for key, field in self.wall_vars.items():
            value = self.cfg["materials"][key]
            field.setText(value if isinstance(value, str) else json.dumps(value))
        self.rays_var.setText(str(self.cfg["simulation"]["n_rays"]))
        self.transmission_enabled.setChecked(self.cfg["transmission"]["enabled"])
        bands = self.cfg["transmission"]["center_freqs_hz"]
        self.tl_table.blockSignals(True)
        self.tl_table.setRowCount(len(LINKS))
        self.tl_table.setColumnCount(len(bands))
        self.tl_table.setHorizontalHeaderLabels([f"{band:g} Hz" for band in bands])
        self.tl_table.setVerticalHeaderLabels([LINK_NAMES[key] for key in LINKS])
        for row, key in enumerate(LINKS):
            for column, value in enumerate(self.cfg["transmission"]["loss_db"][key]):
                item = W.QTableWidgetItem("blocked" if value is None else f"{value:g}")
                self.tl_table.setItem(row, column, item)
                self.tl_table.setColumnWidth(column, 76)
            self.tl_table.setRowHeight(row, 45)
        self.tl_table.blockSignals(False)

        for field, value in [(self.fs_var, self.cfg["simulation"]["sample_rate_hz"]),
                             (self.duration_var, self.cfg["simulation"]["duration_s"]),
                             (self.order_var, self.cfg["simulation"]["max_order"]),
                             (self.wf_duration_var, self.cfg["plots"]["waterfall_duration_s"]),
                             (self.wf_window_var, self.cfg["plots"]["waterfall_window_s"])]:
            field.setText(str(value))
        load_report_controls(self)
        self.syncing = False
        self._sync_object()
        self._refresh_list()
        self._validity(True)
        self.draw()

    def _sync_object(self):
        self.syncing = True
        index = self._speaker_index()
        speaker = None if index is None else self.cfg["speakers"][index]
        self.name_var.setText(self.cfg["listeners"][self._listener_index()]["name"] if speaker is None else speaker["name"])
        space = locate_space(self.cfg["floor_plan"], self._position())
        self.space_combo.setCurrentIndex(SPACES.index(space))
        local_position = [value - origin for value, origin in zip(self._position(), space_boxes(self.cfg["floor_plan"])[space][0])]
        for field, value in zip(self.pos_vars, local_position):
            field.setText(f"{value:.6f}".rstrip("0").rstrip("."))
        self.gain_var.setText("" if speaker is None else str(speaker["gain_db"]))
        self.delay_var.setText("" if speaker is None else str(speaker["delay_ms"]))
        for field in (self.gain_var, self.delay_var):
            field.setEnabled(speaker is not None)
        self.remove_button.setEnabled(len(self.cfg["listeners"] if speaker is None else self.cfg["speakers"]) > 1)
        self.syncing = False
        self._update_distances()

    def _refresh_list(self):
        self.objects.blockSignals(True)
        self.objects.clear()
        for speaker in self.cfg["speakers"]:
            self.objects.addItem(f"{speaker['name']}    ·    z {speaker['position_m'][2]:g} m")
        for listener in self.cfg["listeners"]:
            self.objects.addItem(f"◇ {listener['name']}    ·    z {listener['position_m'][2]:g} m")
        index = self._speaker_index()
        self.objects.setCurrentRow(len(self.cfg["speakers"]) + self._listener_index() if index is None else index)
        self.objects.blockSignals(False)

    def _validity(self, valid, error=""):
        self.form_valid = valid
        self.error_label.setText(error)
        self.canvas.editable = valid
        self.space_combo.setEnabled(valid)
        self.create_button.setEnabled(valid and self.process is None)

    def _changed(self):
        self.revision += 1
        if self.process is None:
            self.status_label.setText("Layout updated · create a report to calculate this configuration.")
        self.setWindowTitle(TITLE + " · unsaved changes")
        self._refresh_list()
        self._update_distances()
        self.draw()

    def _edit(self, *_):
        if self.syncing:
            return
        try:
            plan = copy.deepcopy(self.cfg["floor_plan"])
            for key, field in self.plan_vars.items():
                plan[key] = float(field.text())
            for key in ("room_a", "room_b"):
                for name, field in self.door_vars[key].items():
                    plan["doors"][key][name] = float(field.text())
                plan["doors"][key]["open"] = self.door_open[key].isChecked()
            validate_plan(plan)
            candidate = copy.deepcopy(self.cfg)
            room_changed = plan != candidate["floor_plan"]
            index = self._speaker_index()
            space = locate_space(candidate["floor_plan"], self._position())
            lower, _ = space_boxes(candidate["floor_plan"])[space]
            pos = [float(field.text()) + origin for field, origin in zip(self.pos_vars, lower)]
            if index is None:
                candidate["listeners"][self._listener_index()].update(name=self.name_var.text(), position_m=pos)
            else:
                candidate["speakers"][index].update(name=self.name_var.text(), position_m=pos,
                                                   gain_db=float(self.gain_var.text()), delay_ms=float(self.delay_var.text()))
            if room_changed:
                candidate = resize_positions(candidate, plan)
            material_values = {}
            for wall, field in self.wall_vars.items():
                text = field.text().strip()
                try:
                    material_values[wall] = json.loads(text)
                except json.JSONDecodeError:
                    if text.startswith(("{", "[", '"')) or not text:
                        raise ValueError(f"Finish the material value for {wall}.")
                    material_values[wall] = text
            candidate["materials"] = material_values
            candidate["simulation"]["n_rays"] = float(self.rays_var.text())
            fs = float(self.fs_var.text())
            if not math.isfinite(fs) or int(fs) != fs:
                raise ValueError("Sample rate must be a whole number.")
            candidate["simulation"].update(sample_rate_hz=int(fs), duration_s=float(self.duration_var.text()))
            order = self.order_var.text().strip().lower()
            candidate["simulation"]["max_order"] = "auto" if order == "auto" else float(order)
            candidate["plots"].update(waterfall_duration_s=float(self.wf_duration_var.text()),
                                      waterfall_window_s=float(self.wf_window_var.text()))
            for key in ("frequency_range_hz", "bass_range_hz"):
                candidate["plots"][key][1] = min(candidate["plots"][key][1], fs * 0.45)
            candidate["transmission"]["enabled"] = self.transmission_enabled.isChecked()
            for row, key in enumerate(LINKS):
                values = []
                for column in range(self.tl_table.columnCount()):
                    text = self.tl_table.item(row, column).text().strip().lower()
                    values.append(None if text == "blocked" else float(text))
                candidate["transmission"]["loss_db"][key] = values
            read_report_controls(self, candidate["plots"])
            validated = validate_config(candidate)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self._validity(False, f"Check your entries: {exc}")
            return
        self._validity(True)
        if validated != self.cfg:
            self.cfg = validated
            if room_changed:
                self._sync_object()
            self._changed()

    def select(self, key):
        if key == self.selected:
            return
        if not self.form_valid:
            self._refresh_list()
            return
        self.selected = key
        self._sync_object()
        self._refresh_list()
        self.draw()

    def _select_row(self, row):
        if row >= 0:
            self.select(f"listener:{row - len(self.cfg['speakers'])}" if row >= len(self.cfg["speakers"])
                        else f"speaker:{row}")

    def change_space(self, row):
        if self.syncing or not self.form_valid:
            return
        key = SPACES[row]
        lower, upper = space_boxes(self.cfg["floor_plan"])[key]
        local = [float(field.text()) for field in self.pos_vars]
        local = clamp_position(local, [hi - lo for lo, hi in zip(lower, upper)])
        candidate = copy.deepcopy(self.cfg)
        collection = "listeners" if self._speaker_index() is None else "speakers"
        index = self._listener_index() if collection == "listeners" else self._speaker_index()
        candidate[collection][index]["position_m"] = [lo + value for lo, value in zip(lower, local)]
        try:
            self.cfg = validate_config(candidate)
        except ValueError as exc:
            self.syncing = True
            self.space_combo.setCurrentIndex(SPACES.index(locate_space(self.cfg["floor_plan"], self._position())))
            self.syncing = False
            self.error_label.setText(str(exc))
            return
        self._sync_object()
        self._changed()

    def _update_distances(self):
        space = locate_space(self.cfg["floor_plan"], self._position())
        _, upper = space_boxes(self.cfg["floor_plan"])[space]
        x, y, z = self._position()
        self.distance_label.setText(f"{SPACE_NAMES[space]} · east wall: {upper[0] - x:.2f} m · north wall: {upper[1] - y:.2f} m\n"
                                    f"Below ceiling: {upper[2] - z:.2f} m")

    def draw(self):
        self.canvas.cfg = self.cfg
        self.canvas.active = self.selected
        self.canvas.update()
        width, length, height = self.cfg["room"]["dimensions_m"]
        self.layout_label.setText(f"{width:g} × {length:g} × {height:g} m · "
                                  f"{len(self.cfg['speakers'])} speakers · {len(self.cfg['listeners'])} listeners · metres")

    def move_marker(self, key, x, y):
        if not self.form_valid:
            return
        candidate = copy.deepcopy(self.cfg)
        kind, index = key.split(":")
        target = candidate["listeners" if kind == "listener" else "speakers"][int(index)]
        options = []
        for lower, upper in space_boxes(candidate["floor_plan"]).values():
            local = clamp_position([x - lower[0], y - lower[1], target["position_m"][2]],
                                   [hi - lo for lo, hi in zip(lower, upper)])
            options.append([lo + value for lo, value in zip(lower, local)])
        target["position_m"] = min(options, key=lambda p: math.hypot(p[0] - x, p[1] - y))
        try:
            self.cfg = validate_config(candidate)
        except ValueError:
            return
        self._sync_object()
        self._changed()

    def add_object(self, collection):
        if not self.form_valid:
            return
        candidate = copy.deepcopy(self.cfg)
        space = locate_space(candidate["floor_plan"], self._position())
        lower, upper = space_boxes(candidate["floor_plan"])[space]
        names = {item["name"] for item in candidate[collection]}
        prefix = "Listener" if collection == "listeners" else "Speaker"
        suffix = len(names) + 1
        while f"{prefix} {suffix}" in names:
            suffix += 1
        occupied = [item["position_m"] for item in candidate["speakers"] + candidate["listeners"]]
        options = [[lower[0] + (upper[0] - lower[0]) * x,
                    lower[1] + (upper[1] - lower[1]) * y, min(1.2, upper[2] * 0.5)]
                   for y in (0.35, 0.65, 0.8, 0.2) for x in (0.3, 0.5, 0.7, 0.85, 0.15)]
        pos = max(options, key=lambda p: min(math.dist(p, other) for other in occupied))
        item = {"name": f"{prefix} {suffix}", "position_m": pos}
        if collection == "speakers":
            item.update(gain_db=0, delay_ms=0)
        candidate[collection].append(item)
        try:
            self.cfg = validate_config(candidate)
        except ValueError as exc:
            W.QMessageBox.warning(self, "Cannot add object", str(exc))
            return
        self.selected = f"{'listener' if collection == 'listeners' else 'speaker'}:{len(candidate[collection]) - 1}"
        self._sync_object()
        self._changed()

    def add_speaker(self):
        self.add_object("speakers")

    def add_listener(self):
        self.add_object("listeners")

    def remove_object(self):
        if not self.form_valid:
            return
        collection = "listeners" if self._speaker_index() is None else "speakers"
        if len(self.cfg[collection]) < 2:
            return
        index = self._listener_index() if collection == "listeners" else self._speaker_index()
        del self.cfg[collection][index]
        self.selected = "listener:0" if collection == "listeners" else "speaker:0"
        self._sync_object()
        self._changed()

    def save_config(self):
        if not self.form_valid:
            W.QMessageBox.warning(self, "Check configuration", self.error_label.text())
            return
        path, _ = W.QFileDialog.getSaveFileName(self, "Save room configuration", str(self.config_path), "Room configuration (*.json)")
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(self.cfg, indent=2) + "\n")
        except OSError as exc:
            W.QMessageBox.warning(self, "Cannot save", str(exc))
            return
        self.config_path = Path(path)
        self.saved_revision = self.revision
        self.setWindowTitle(TITLE)
        self.status_label.setText(f"Configuration saved: {path}")

    def load_config(self):
        if self.revision != self.saved_revision or not self.form_valid:
            if W.QMessageBox.question(self, "Load configuration", "Discard current edits and load another configuration?") != W.QMessageBox.StandardButton.Yes:
                return
        path, _ = W.QFileDialog.getOpenFileName(self, "Load room configuration", str(self.config_path.parent), "Room configuration (*.json)")
        if not path:
            return
        try:
            cfg = validate_config(json.loads(Path(path).read_text()))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            W.QMessageBox.warning(self, "Cannot load", str(exc))
            return
        self.cfg, self.config_path, self.selected = cfg, Path(path), "speaker:0"
        self.revision += 1
        self.saved_revision = self.revision
        self._load_fields()
        self.setWindowTitle(TITLE)
        self.status_label.setText(f"Loaded {Path(path).name}")

    def browse_output(self):
        path = W.QFileDialog.getExistingDirectory(self, "Choose report folder", self.output_var.text())
        if path:
            self.output_var.setText(path)

    def create_report(self):
        if self.process is not None or not self.form_valid:
            return
        try:
            snapshot = validate_config(self.cfg)
            if not self.output_var.text().strip():
                raise ValueError("Choose a report folder first.")
            parent = Path(self.output_var.text()).expanduser().resolve()
            run_dir = parent / datetime.now().strftime("room_%Y-%m-%d_%H-%M-%S_%f")
            run_dir.mkdir(parents=True)
            snapshot_path = run_dir / "room_config.json"
            snapshot_path.write_text(json.dumps(snapshot, indent=2) + "\n")
            self.log_handle = (run_dir / "simulation.log").open("w")
            self.process = subprocess.Popen(report_command(snapshot_path, run_dir), cwd=str(PROJECT),
                                            stdout=self.log_handle, stderr=subprocess.STDOUT)
        except (ValueError, OSError) as exc:
            if self.log_handle:
                self.log_handle.close()
                self.log_handle = None
            W.QMessageBox.warning(self, "Cannot create report", str(exc))
            return
        self.run_dir = run_dir
        self.report_revision = self.revision
        self.cancelled = False
        self._validity(True)
        self.cancel_button.setEnabled(True)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.status_label.setText("Creating report… You can keep editing; this report uses the layout captured when you clicked.")
        self.timer.start()

    def _poll_report(self):
        if self.process is None:
            return
        code = self.process.poll()
        if code is None:
            return
        self.process = None
        self.timer.stop()
        self.log_handle.close()
        self.log_handle = None
        self.progress.hide()
        self.cancel_button.setEnabled(False)
        self._validity(self.form_valid, self.error_label.text())
        if self.cancelled:
            self.status_label.setText("Report cancelled. You can edit the layout and try again.")
            return
        report = self.run_dir / "acoustics_report.pdf"
        if code != 0 or not report.exists():
            log = (self.run_dir / "simulation.log").read_text(errors="replace")
            self.status_label.setText(f"Report failed · details in {self.run_dir / 'simulation.log'}")
            W.QMessageBox.warning(self, "Report failed", log[-3000:] or "No report was generated.")
            return
        self.last_report = report
        self.open_button.setEnabled(True)
        message = f"Report ready: {report}"
        if self.revision != self.report_revision or not self.form_valid:
            message += " · Layout has changed since this report started."
        self.status_label.setText(message)

    def cancel_report(self):
        if self.process and self.process.poll() is None:
            self.cancelled = True
            self.process.terminate()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("Cancelling report…")

    def open_report(self):
        if self.last_report:
            if not G.QDesktopServices.openUrl(C.QUrl.fromLocalFile(str(self.last_report))):
                W.QMessageBox.warning(self, "Cannot open PDF", f"Open this file manually:\n{self.last_report}")

    def stop_worker(self):
        self.timer.stop()
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None

    def closeEvent(self, event):
        question = None
        if self.process is not None:
            question = "Cancel the running report and close the editor?"
        elif self.revision != self.saved_revision or not self.form_valid:
            question = "Close without saving your configuration edits?"
        if question and W.QMessageBox.question(self, "Close editor", question) != W.QMessageBox.StandardButton.Yes:
            event.ignore()
            return
        self.stop_worker()
        event.accept()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PROJECT / "room_config_transmission.json")
    args = parser.parse_args()
    prepare_qt_plugins()
    app = W.QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    if sys.platform == "darwin":
        app.setFont(G.QFont("Helvetica", 13))
    try:
        editor = RoomEditor(args.config)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        W.QMessageBox.critical(None, "Cannot start editor", str(exc))
        return 1
    editor.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
