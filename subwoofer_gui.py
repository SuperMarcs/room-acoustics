#!/usr/bin/env python3
"""Live rectangular-room bass maps and subwoofer placement search."""
from __future__ import annotations

import copy
from datetime import datetime
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import numpy as np
from PySide6 import QtCore as C, QtGui as G, QtWidgets as W

from room_gui_multi import apply_light_theme, clamp_position, prepare_qt_plugins
from subwoofer_model import PROJECT, fingerprint, load_result, validate_config

os.environ.setdefault("MPLCONFIGDIR", str(PROJECT / ".mplconfig"))
from matplotlib import colormaps
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure


COLORS = ("#2563eb", "#e87919", "#059669", "#9333ea", "#dc2626")


class BassMap(W.QWidget):
    selected = C.Signal(str)
    moved = C.Signal(str, float, float)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(480, 330)
        self.setMouseTracking(True)
        self.cfg = None
        self.active = "sub:0"
        self.data = None
        self.frequency_index = 0
        self.image = None
        self.dragging = None
        self.markers = {}
        self.editable = True

    def to_pixel(self, x, y):
        return C.QPointF(self.origin_x + x * self.scale, self.origin_y - y * self.scale)

    def set_data(self, data, index):
        self.data, self.frequency_index = data, index
        self.image = None
        if data is not None:
            lo, hi = self.cfg["map"]["color_limits_db"]
            levels = data["map_db"][index]
            rgba = np.asarray(colormaps["viridis"](np.clip((levels - lo) / (hi - lo), 0, 1)) * 255, dtype=np.uint8)
            rgba[~np.isfinite(levels)] = [224, 228, 235, 255]
            rgba = np.ascontiguousarray(rgba[::-1])  # y grows north; QImage rows grow down.
            self.image = G.QImage(rgba.data, rgba.shape[1], rgba.shape[0], rgba.strides[0],
                                   G.QImage.Format.Format_RGBA8888).copy()
        self.update()

    def paintEvent(self, event):
        painter = G.QPainter(self)
        painter.setRenderHints(G.QPainter.RenderHint.Antialiasing | G.QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.rect(), G.QColor("#f8fafc"))
        if self.cfg is None:
            return
        width, length, _ = self.cfg["room"]["dimensions_m"]
        self.scale = min((self.width() - 150) / width, (self.height() - 75) / length)
        self.origin_x = (self.width() - 70 - width * self.scale) / 2
        self.origin_y = (self.height() + length * self.scale) / 2
        self.room_rect = C.QRectF(self.to_pixel(0, length), self.to_pixel(width, 0))
        painter.fillRect(self.room_rect, G.QColor("white"))
        if self.image is not None:
            painter.drawImage(self.room_rect, self.image)
        else:
            painter.setPen(G.QColor("#667085"))
            painter.drawText(self.room_rect, C.Qt.AlignmentFlag.AlignCenter, "Press Play to calculate the bass map")
        painter.setBrush(C.Qt.BrushStyle.NoBrush)
        painter.setPen(G.QPen(G.QColor("#344054"), 2))
        painter.drawRect(self.room_rect)
        painter.setPen(G.QColor("#475467"))
        painter.drawText(C.QRectF(self.room_rect.left(), self.room_rect.top() - 29, self.room_rect.width(), 24),
                         C.Qt.AlignmentFlag.AlignCenter, f"Width {width:g} m · north")
        painter.drawText(C.QRectF(self.room_rect.left(), self.room_rect.bottom() + 6, self.room_rect.width(), 24),
                         C.Qt.AlignmentFlag.AlignCenter, f"Length {length:g} m · south / y = 0")
        self.markers = {}
        label_rectangles = []
        for collection, prefix in (("subwoofers", "sub"), ("listeners", "listener")):
            for index, item in enumerate(self.cfg[collection]):
                point = self.to_pixel(*item["position_m"][:2])
                key = f"{prefix}:{index}"
                self.markers[key] = point
                color = COLORS[index % len(COLORS)] if prefix == "sub" else "#101828"
                if prefix == "sub" and not item["enabled"]:
                    color = "#98a2b3"
                if key == self.active:
                    painter.setPen(G.QPen(G.QColor("white"), 4))
                    painter.drawEllipse(point, 14, 14)
                    painter.setPen(G.QPen(G.QColor(color), 2))
                    painter.drawEllipse(point, 14, 14)
                painter.setPen(G.QPen(G.QColor("white"), 2))
                painter.setBrush(G.QColor(color))
                if prefix == "sub":
                    painter.drawRoundedRect(C.QRectF(point.x() - 7, point.y() - 7, 14, 14), 2, 2)
                else:
                    painter.drawPolygon(G.QPolygonF([point + C.QPointF(0, -9), point + C.QPointF(9, 0),
                                                     point + C.QPointF(0, 9), point + C.QPointF(-9, 0)]))
                text = item["name"] + (" (fixed)" if prefix == "sub" and item["locked"] else "")
                metrics = painter.fontMetrics()
                label = C.QRectF(point.x() - metrics.horizontalAdvance(text) / 2 - 4, point.y() - 30,
                                  metrics.horizontalAdvance(text) + 8, metrics.height() + 4)
                label.moveLeft(max(1, min(label.left(), self.width() - label.width() - 1)))
                for offset in (-30, 12, -52, 34, -74, 56):
                    label.moveTop(max(1, point.y() + offset))
                    if not any(label.adjusted(-2, -2, 2, 2).intersects(previous) for previous in label_rectangles):
                        break
                label_rectangles.append(C.QRectF(label))
                painter.setPen(C.Qt.PenStyle.NoPen)
                painter.setBrush(G.QColor(255, 255, 255, 230))
                painter.drawRoundedRect(label, 3, 3)
                painter.setPen(G.QColor(color))
                painter.drawText(label, C.Qt.AlignmentFlag.AlignCenter, text)
        lo, hi = self.cfg["map"]["color_limits_db"]
        bar = C.QRectF(self.width() - 62, self.room_rect.top(), 14, self.room_rect.height())
        gradient = G.QLinearGradient(bar.bottomLeft(), bar.topLeft())
        for value in np.linspace(0, 1, 20):
            rgba = colormaps["viridis"](value)
            gradient.setColorAt(float(value), G.QColor.fromRgbF(*rgba))
        painter.fillRect(bar, gradient)
        painter.setPen(G.QColor("#344054"))
        for value in np.linspace(lo, hi, 5):
            y = bar.bottom() - (value - lo) / (hi - lo) * bar.height()
            painter.drawText(C.QRectF(bar.right() + 4, y - 9, 42, 20), f"{value:g}")

    def mousePressEvent(self, event):
        if not self.editable or not self.markers or event.button() != C.Qt.MouseButton.LeftButton:
            return
        position = event.position()
        key = min(self.markers, key=lambda item: (position - self.markers[item]).manhattanLength())
        offset = position - self.markers[key]
        if math.hypot(offset.x(), offset.y()) <= 24:
            self.dragging = (key, offset)
            self.selected.emit(key)

    def mouseMoveEvent(self, event):
        if self.dragging and self.editable:
            key, offset = self.dragging
            point = event.position() - offset
            self.moved.emit(key, (point.x() - self.origin_x) / self.scale,
                            (self.origin_y - point.y()) / self.scale)
        elif self.data is not None:
            x, y = (event.position().x() - self.origin_x) / self.scale, (self.origin_y - event.position().y()) / self.scale
            if 0 <= x <= self.cfg["room"]["dimensions_m"][0] and 0 <= y <= self.cfg["room"]["dimensions_m"][1]:
                ix, iy = np.argmin(abs(self.data["x_m"] - x)), np.argmin(abs(self.data["y_m"] - y))
                value = self.data["map_db"][self.frequency_index, iy, ix]
                self.setToolTip(f"Grid sample x={self.data['x_m'][ix]:.2f}, y={self.data['y_m'][iy]:.2f} m\n"
                                + (f"{value:.1f} relative dB" if np.isfinite(value) else "Near-source region excluded"))

    def mouseReleaseEvent(self, event):
        self.dragging = None


class ResponsePlot(FigureCanvasQTAgg):
    def __init__(self):
        self.figure = Figure(figsize=(7, 2.0), dpi=100)
        super().__init__(self.figure)
        self.setMinimumHeight(175)
        self.setMaximumHeight(245)
        self.axis = self.figure.add_subplot(111)
        self.figure.subplots_adjust(left=0.10, right=0.98, bottom=0.25, top=0.88)
        self.cursor = None
        self.clear()

    def clear(self):
        self.axis.clear()
        self.axis.text(0.5, 0.5, "Listening-position response appears after Play", ha="center", transform=self.axis.transAxes)
        self.cursor = None
        self.draw_idle()

    def show_data(self, cfg, data, frequency):
        self.axis.clear()
        for i, seat in enumerate(cfg["listeners"]):
            self.axis.plot(data["frequencies_hz"], data["seat_db"][i], label=seat["name"], linewidth=1)
        self.axis.set(xlabel="Frequency (Hz)", ylabel="Relative dB", xlim=cfg["model"]["frequency_range_hz"])
        self.axis.grid(alpha=0.18)
        self.axis.legend(loc="lower left", fontsize=7, ncol=3)
        self.axis.tick_params(labelsize=8)
        self.cursor = self.axis.axvline(frequency, color="#101828", linestyle="--", linewidth=1)
        self.draw_idle()

    def set_frequency(self, frequency):
        if self.cursor is not None:
            self.cursor.set_xdata([frequency, frequency])
            self.draw_idle()


class SubwooferEditor(W.QMainWindow):
    def __init__(self, config_path=PROJECT / "subwoofer_config.json"):
        super().__init__()
        apply_light_theme(W.QApplication.instance())
        self.cfg = validate_config(json.loads(Path(config_path).read_text()))
        self.config_path = Path(config_path)
        self.selected = "sub:0"
        self.syncing = False
        self.form_valid = True
        self.revision = 0
        self.saved_revision = 0
        self.playing = False
        self.data = None
        self.optimization = None
        self.undo_config = None
        self.process = None
        self.job_temp = None
        self.job_dir = None
        self.log_handle = None
        self.last_report = None
        self.cancelled = False
        self.worker_starts = 0
        self.setWindowTitle("Subwoofer placement studio")
        self.resize(1260, 930)
        self.setMinimumSize(1040, 780)
        self.poll_timer = C.QTimer(self)
        self.poll_timer.setInterval(100)
        self.poll_timer.timeout.connect(self._poll)
        self.debounce = C.QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(350)
        self.debounce.timeout.connect(self._refresh_if_playing)
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
        box = W.QGroupBox(title)
        box.setLayout(layout)
        return box

    def _build(self):
        self.setStyleSheet("""
            QWidget { background: white; color: #101828; font-size: 13px; }
            QGroupBox { border: 1px solid #d0d5dd; border-radius: 6px; margin-top: 13px; padding: 15px 8px 8px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
            QLineEdit, QComboBox, QSpinBox { background: white; border: 1px solid #d0d5dd; border-radius: 4px; padding: 5px; }
            QPushButton { background: #f8fafc; border: 1px solid #d0d5dd; border-radius: 5px; padding: 8px 10px; }
            QPushButton:hover { background: #eef2f6; }
            QPushButton:disabled, QLineEdit:disabled { background: #f2f4f7; color: #667085; }
            QPushButton#primary { background: #2563eb; color: white; border: none; font-weight: 600; }
            QListWidget { border: 1px solid #d0d5dd; border-radius: 4px; }
            QListWidget::item { padding: 5px; }
            QListWidget::item:selected { background: #e9f0ff; color: #1d4ed8; }
            QTabBar::tab { background: #eef2f6; color: #344054; padding: 8px 10px; }
            QTabBar::tab:selected { background: white; color: #101828; }
        """)
        central = W.QWidget()
        self.setCentralWidget(central)
        outer = W.QVBoxLayout(central)
        outer.setContentsMargins(18, 16, 18, 16)
        header = W.QHBoxLayout()
        title = self.label("Subwoofer placement studio")
        title.setStyleSheet("font-size: 24px; font-weight: 700")
        header.addWidget(title, 1)
        header.addWidget(self.button("Load…", self.load_config))
        header.addWidget(self.button("Save configuration…", self.save_config))
        outer.addLayout(header)
        actions = W.QHBoxLayout()
        self.play_button = self.button("▶ Play", self.toggle_play)
        self.play_button.setObjectName("primary")
        self.optimize_button = self.button("Optimize placement", lambda: self.start_job("optimize"))
        self.undo_button = self.button("Undo optimization", self.undo_optimization)
        self.cancel_button = self.button("Cancel calculation", self.cancel_job)
        self.report_button = self.button("Create report", lambda: self.start_job("report"))
        self.open_button = self.button("Open PDF", self.open_report)
        for button in (self.play_button, self.optimize_button, self.undo_button, self.cancel_button, self.report_button, self.open_button):
            actions.addWidget(button)
        outer.addLayout(actions)
        body = W.QSplitter()
        outer.addWidget(body, 1)
        tabs = W.QTabWidget()
        tabs.setMinimumWidth(340)
        tabs.setMaximumWidth(390)
        body.addWidget(tabs)
        layout_page, model_page = W.QWidget(), W.QWidget()
        for page, text in ((layout_page, "Room and placement"), (model_page, "Map and search")):
            scroll = W.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            scroll.setFrameShape(W.QFrame.Shape.NoFrame)
            tabs.addTab(scroll, text)
        left, settings = W.QVBoxLayout(layout_page), W.QVBoxLayout(model_page)
        form = W.QFormLayout()
        self.dim_fields = [self.field() for _ in range(3)]
        for label, field in zip(("Width x (m)", "Length y (m)", "Height z (m)"), self.dim_fields):
            form.addRow(label, field)
        left.addWidget(self.group("Rectangular room", form))
        self.objects = W.QListWidget()
        self.objects.setMaximumHeight(135)
        self.objects.currentRowChanged.connect(self._select_row)
        left.addWidget(self.objects)
        row = W.QHBoxLayout()
        row.addWidget(self.button("Add sub", lambda: self.add_object("subwoofers")))
        row.addWidget(self.button("Add seat", lambda: self.add_object("listeners")))
        self.remove_button = self.button("Remove", self.remove_object)
        row.addWidget(self.remove_button)
        left.addLayout(row)
        self.name_field = self.field()
        self.position_fields = [self.field() for _ in range(3)]
        self.gain_field, self.delay_field = self.field(), self.field()
        self.polarity_combo = W.QComboBox()
        self.polarity_combo.addItem("Normal (+)", 1)
        self.polarity_combo.addItem("Inverted (−)", -1)
        self.polarity_combo.currentIndexChanged.connect(self._edit)
        self.enabled_check = W.QCheckBox("Active")
        self.locked_check = W.QCheckBox("Keep fixed during optimization")
        self.enabled_check.toggled.connect(self._edit)
        self.locked_check.toggled.connect(self._edit)
        form = W.QFormLayout()
        for label, field in [("Name", self.name_field), ("x from west (m)", self.position_fields[0]),
                             ("y from south (m)", self.position_fields[1]), ("z above floor (m)", self.position_fields[2]),
                             ("Gain (dB)", self.gain_field), ("Delay (ms)", self.delay_field),
                             ("Polarity", self.polarity_combo)]:
            form.addRow(label, field)
        form.addRow(self.enabled_check)
        form.addRow(self.locked_check)
        left.addWidget(self.group("Selected subwoofer / seat", form))
        left.addWidget(self.label("Drag the square subs and diamond seats. Play refreshes after edits; the frequency slider uses cached maps."))
        left.addStretch()
        self.settings_fields = {key: self.field() for key in
                                ("rt60", "minimum", "maximum", "cutoff", "plane", "grid", "floor", "ceiling",
                                 "search_grid", "clearance", "rounds", "level_drop")}
        form = W.QFormLayout()
        for key, label in (("rt60", "Assumed RT60 (s)"), ("minimum", "Minimum frequency (Hz)"),
                           ("maximum", "Maximum frequency (Hz)"), ("cutoff", "Mode cutoff (Hz / auto)"),
                           ("plane", "Map plane height (m)"), ("grid", "Map grid spacing (m)"),
                           ("floor", "Map minimum (dB)"), ("ceiling", "Map maximum (dB)")):
            form.addRow(label, self.settings_fields[key])
        settings.addWidget(self.group("Bass map", form))
        self.region_combo = W.QComboBox()
        self.region_combo.addItem("Around the perimeter", "perimeter")
        self.region_combo.addItem("Across the whole floor", "floor")
        self.region_combo.currentIndexChanged.connect(self._edit)
        form = W.QFormLayout()
        form.addRow("Search region", self.region_combo)
        for key, label in (("search_grid", "Candidate spacing (m)"), ("clearance", "Wall clearance (m)"),
                           ("rounds", "Search rounds"), ("level_drop", "Allowed mean drop (dB)")):
            form.addRow(label, self.settings_fields[key])
        settings.addWidget(self.group("Placement search", form))
        settings.addWidget(self.label("Search moves active, unlocked subs in x/y at their existing heights. "
                                      "It balances response smoothness across frequency with consistency between seats; gain, delay and polarity stay fixed."))
        settings.addWidget(self.label("Model: damped modes of an empty rectangular room. Ideal monopoles, fixed relative-dB reference. "
                                      "No calibrated SPL, furniture, connected rooms or wall-transmission model."))
        settings.addStretch()
        preview = W.QWidget()
        right = W.QVBoxLayout(preview)
        right.setContentsMargins(12, 4, 0, 0)
        self.map_label = self.label("Bass map · press Play")
        self.map_label.setStyleSheet("font-size: 16px; font-weight: 600")
        right.addWidget(self.map_label)
        self.canvas = BassMap()
        self.canvas.selected.connect(self.select)
        self.canvas.moved.connect(self.move_marker)
        right.addWidget(self.canvas, 1)
        slider_row = W.QHBoxLayout()
        slider_row.addWidget(self.label("Frequency"))
        self.frequency_slider = W.QSlider(C.Qt.Orientation.Horizontal)
        self.frequency_spin = W.QSpinBox()
        self.frequency_spin.setSuffix(" Hz")
        self.frequency_slider.valueChanged.connect(self.set_frequency)
        self.frequency_spin.valueChanged.connect(self.set_frequency)
        slider_row.addWidget(self.frequency_slider, 1)
        slider_row.addWidget(self.frequency_spin)
        right.addLayout(slider_row)
        right.addWidget(self.label("Relative dB · fixed scale · markers show x/y positions projected onto the chosen height plane"))
        self.response_plot = ResponsePlot()
        right.addWidget(self.response_plot)
        self.metrics_label = self.label("Listening-position metrics appear after Play.")
        right.addWidget(self.metrics_label)
        body.addWidget(preview)
        body.setStretchFactor(1, 1)
        self.error_label = self.label("")
        self.error_label.setStyleSheet("color: #b42318")
        outer.addWidget(self.error_label)
        output_row = W.QHBoxLayout()
        output_row.addWidget(self.label("Report folder"))
        self.output_field = W.QLineEdit(str(PROJECT / "reports_subwoofer"))
        output_row.addWidget(self.output_field, 1)
        output_row.addWidget(self.button("Browse…", self.browse_output))
        outer.addLayout(output_row)
        self.status = self.label("Ready. Press Play to calculate, then drag positions or move the frequency slider.")
        outer.addWidget(self.status)

    def target(self):
        kind, index = self.selected.split(":")
        return self.cfg["subwoofers" if kind == "sub" else "listeners"][int(index)]

    def _refresh_list(self):
        self.objects.blockSignals(True)
        self.objects.clear()
        for sub in self.cfg["subwoofers"]:
            self.objects.addItem("■ " + sub["name"] + (" · muted" if not sub["enabled"] else "") + (" · fixed" if sub["locked"] else ""))
        for seat in self.cfg["listeners"]:
            self.objects.addItem("◇ " + seat["name"])
        kind, index = self.selected.split(":")
        self.objects.setCurrentRow(int(index) + (len(self.cfg["subwoofers"]) if kind == "listener" else 0))
        self.objects.blockSignals(False)

    def _sync_target(self):
        self.syncing = True
        item, is_sub = self.target(), self.selected.startswith("sub:")
        self.name_field.setText(item["name"])
        for field, value in zip(self.position_fields, item["position_m"]):
            field.setText(f"{value:.6g}")
        self.gain_field.setText(str(item["gain_db"]) if is_sub else "")
        self.delay_field.setText(str(item["delay_ms"]) if is_sub else "")
        self.polarity_combo.setCurrentIndex(self.polarity_combo.findData(item["polarity"]) if is_sub else 0)
        self.enabled_check.setChecked(item["enabled"] if is_sub else False)
        self.locked_check.setChecked(item["locked"] if is_sub else False)
        for widget in (self.gain_field, self.delay_field, self.polarity_combo, self.enabled_check, self.locked_check):
            widget.setEnabled(is_sub)
        self.remove_button.setEnabled(len(self.cfg["subwoofers"] if is_sub else self.cfg["listeners"]) > 1)
        self.syncing = False
        self.canvas.active = self.selected
        self.canvas.update()

    def _load_fields(self):
        self.syncing = True
        for field, value in zip(self.dim_fields, self.cfg["room"]["dimensions_m"]):
            field.setText(str(value))
        model, mapping, search = self.cfg["model"], self.cfg["map"], self.cfg["optimization"]
        values = {"rt60": model["rt60_s"], "minimum": model["frequency_range_hz"][0],
                  "maximum": model["frequency_range_hz"][1], "cutoff": model["mode_cutoff_hz"],
                  "plane": mapping["height_m"], "grid": mapping["grid_spacing_m"],
                  "floor": mapping["color_limits_db"][0], "ceiling": mapping["color_limits_db"][1],
                  "search_grid": search["grid_spacing_m"], "clearance": search["wall_clearance_m"],
                  "rounds": search["rounds"], "level_drop": search["maximum_level_drop_db"]}
        for key, value in values.items():
            self.settings_fields[key].setText(str(value))
        self.region_combo.setCurrentIndex(self.region_combo.findData(search["region"]))
        self.syncing = False
        self._sync_target()
        self._refresh_list()
        self._slider_range()
        self.canvas.cfg = self.cfg
        self.canvas.update()
        self._buttons()

    def _slider_range(self):
        lo, hi = (int(value) for value in self.cfg["model"]["frequency_range_hz"])
        value = self.frequency_spin.value() or 80
        for widget in (self.frequency_slider, self.frequency_spin):
            widget.blockSignals(True)
            widget.setRange(lo, hi)
            widget.setValue(max(lo, min(hi, value)))
            widget.blockSignals(False)

    def _buttons(self):
        busy = self.process is not None
        self.play_button.setText("⏸ Pause" if self.playing else "▶ Play")
        self.play_button.setEnabled(self.playing or self.form_valid)
        self.optimize_button.setEnabled(not busy and self.form_valid and any(
            sub["enabled"] and not sub["locked"] for sub in self.cfg["subwoofers"]))
        self.report_button.setEnabled(not busy and self.form_valid)
        self.cancel_button.setEnabled(busy)
        self.undo_button.setEnabled(not busy and self.undo_config is not None)
        self.open_button.setEnabled(self.last_report is not None)
        self.canvas.editable = self.form_valid

    def _edit(self, *_):
        if self.syncing:
            return
        try:
            cfg = copy.deepcopy(self.cfg)
            dims = [float(field.text()) for field in self.dim_fields]
            changed_room = dims != cfg["room"]["dimensions_m"]
            cfg["room"]["dimensions_m"] = dims
            kind, index = self.selected.split(":")
            item = cfg["subwoofers" if kind == "sub" else "listeners"][int(index)]
            item.update(name=self.name_field.text(), position_m=[float(field.text()) for field in self.position_fields])
            if kind == "sub":
                item.update(gain_db=float(self.gain_field.text()), delay_ms=float(self.delay_field.text()),
                            polarity=self.polarity_combo.currentData(), enabled=self.enabled_check.isChecked(),
                            locked=self.locked_check.isChecked())
            fields = self.settings_fields
            cutoff = fields["cutoff"].text().strip().lower()
            cfg["model"].update(rt60_s=float(fields["rt60"].text()),
                                frequency_range_hz=[float(fields[key].text()) for key in ("minimum", "maximum")],
                                mode_cutoff_hz="auto" if cutoff == "auto" else float(cutoff))
            cfg["map"].update(height_m=float(fields["plane"].text()), grid_spacing_m=float(fields["grid"].text()),
                              color_limits_db=[float(fields[key].text()) for key in ("floor", "ceiling")])
            cfg["optimization"].update(region=self.region_combo.currentData(),
                                       grid_spacing_m=float(fields["search_grid"].text()),
                                       wall_clearance_m=float(fields["clearance"].text()),
                                       rounds=float(fields["rounds"].text()),
                                       maximum_level_drop_db=float(fields["level_drop"].text()))
            if changed_room and all(value > 0 for value in dims):
                for collection in ("subwoofers", "listeners"):
                    for obj in cfg[collection]:
                        obj["position_m"] = clamp_position(obj["position_m"], dims)
                cfg["map"]["height_m"] = min(cfg["map"]["height_m"], dims[2] - 0.01)
            cfg = validate_config(cfg)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self.form_valid = False
            self.error_label.setText(str(exc))
            self.canvas.set_data(None, 0)
            self._buttons()
            return
        self.form_valid = True
        self.error_label.setText("")
        if cfg != self.cfg:
            self._commit(cfg)
            if changed_room:
                self._load_fields()
        else:
            available = self._show_current_data()
            self._buttons()
            if self.playing and not available:
                self.debounce.start()

    def _commit(self, cfg, manual=True):
        self.cfg = cfg
        self.revision += 1
        if manual:
            self.optimization = self.undo_config = None
        self.setWindowTitle("Subwoofer placement studio · unsaved changes")
        self.canvas.cfg = self.cfg
        self.canvas.set_data(None, 0)
        self.response_plot.clear()
        self.map_label.setText("Layout updated · calculating…" if self.playing else "Layout updated · press Play")
        self._refresh_list()
        self._slider_range()
        self._buttons()
        if self.playing:
            self.debounce.start()

    def _select_row(self, row):
        if row >= 0:
            self.select(f"sub:{row}" if row < len(self.cfg["subwoofers"]) else f"listener:{row - len(self.cfg['subwoofers'])}")

    def select(self, key):
        if not self.form_valid:
            self._refresh_list()
            return
        self.selected = key
        self._sync_target()
        self._refresh_list()

    def move_marker(self, key, x, y):
        if not self.form_valid:
            return
        cfg = copy.deepcopy(self.cfg)
        kind, index = key.split(":")
        item = cfg["subwoofers" if kind == "sub" else "listeners"][int(index)]
        item["position_m"] = clamp_position([x, y, item["position_m"][2]], cfg["room"]["dimensions_m"])
        try:
            cfg = validate_config(cfg)
        except ValueError:
            return
        self._commit(cfg)
        self._sync_target()

    def add_object(self, collection):
        if not self.form_valid:
            return
        cfg = copy.deepcopy(self.cfg)
        width, length, height = cfg["room"]["dimensions_m"]
        prefix = "Sub" if collection == "subwoofers" else "Seat"
        names = {obj["name"] for obj in cfg[collection]}
        index = len(names) + 1
        while f"{prefix} {index}" in names:
            index += 1
        z = min(0.3, height / 2) if collection == "subwoofers" else cfg["map"]["height_m"]
        positions = [[width * x, length * y, z] for x in (0.1, 0.3, 0.5, 0.7, 0.9) for y in (0.15, 0.35, 0.65, 0.85)]
        occupied = [obj["position_m"] for obj in cfg["subwoofers"] + cfg["listeners"]]
        position = max(positions, key=lambda p: min(math.dist(p, other) for other in occupied))
        item = {"name": f"{prefix} {index}", "position_m": position}
        if collection == "subwoofers":
            item.update(gain_db=0, delay_ms=0, polarity=1, enabled=True, locked=False)
        cfg[collection].append(item)
        try:
            cfg = validate_config(cfg)
        except ValueError as exc:
            self.error_label.setText(str(exc))
            return
        self.selected = f"{'sub' if collection == 'subwoofers' else 'listener'}:{len(cfg[collection]) - 1}"
        self._commit(cfg)
        self._sync_target()

    def remove_object(self):
        if not self.form_valid:
            return
        kind, index = self.selected.split(":")
        collection = "subwoofers" if kind == "sub" else "listeners"
        if len(self.cfg[collection]) <= 1:
            return
        cfg = copy.deepcopy(self.cfg)
        del cfg[collection][int(index)]
        try:
            cfg = validate_config(cfg)
        except ValueError as exc:
            self.error_label.setText(str(exc))
            return
        self.selected = kind + ":0"
        self._commit(cfg)
        self._sync_target()

    def set_frequency(self, value):
        for widget in (self.frequency_slider, self.frequency_spin):
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)
        self._show_current_data(update_response=False)
        self.response_plot.set_frequency(value)

    def _show_current_data(self, update_response=True):
        if self.data is None or not self.form_valid or str(self.data["fingerprint"]) != fingerprint(self.cfg):
            return False
        value = self.frequency_spin.value()
        index = int(np.argmin(abs(self.data["frequencies_hz"] - value)))
        self.canvas.cfg = self.cfg
        self.canvas.set_data(self.data, index)
        self.map_label.setText(f"Bass map · {self.data['frequencies_hz'][index]:g} Hz · height {self.cfg['map']['height_m']:g} m")
        summary = json.loads(str(self.data["summary_json"]))
        metrics = summary["metrics"]
        self.metrics_label.setText(f"Score {metrics['score_db']:.2f} dB · ripple {metrics['spectral_ripple_db']:.2f} dB · "
                                   f"seat spread {metrics['seat_spread_db']:.2f} dB · lower score is better")
        if update_response:
            self.response_plot.show_data(self.cfg, self.data, value)
        return True

    def toggle_play(self):
        self.playing = not self.playing
        self._buttons()
        if self.playing:
            self._refresh_if_playing()
        else:
            self.debounce.stop()
            self.status.setText("Paused. The slider still explores the current cached map; press Play after further edits.")

    def _refresh_if_playing(self):
        if self.playing and self.form_valid and self.process is None:
            if not self._show_current_data():
                self.start_job("calculate")

    def start_job(self, kind):
        if self.process is not None or not self.form_valid:
            return
        self.debounce.stop()
        try:
            self.job_kind, self.job_revision = kind, self.revision
            if kind == "report":
                if not self.output_field.text().strip():
                    raise ValueError("Choose a report folder")
                self.job_dir = Path(self.output_field.text()).expanduser().resolve() / datetime.now().strftime("subwoofer_%Y-%m-%d_%H-%M-%S_%f")
                self.job_dir.mkdir(parents=True)
            else:
                cache = PROJECT / ".subwoofer_jobs"
                cache.mkdir(exist_ok=True)
                self.job_temp = tempfile.TemporaryDirectory(prefix="job_", dir=cache)
                self.job_dir = Path(self.job_temp.name)
            request = {"task": kind, "config": self.cfg, "frequency_hz": self.frequency_spin.value(),
                       "comparison": self.optimization}
            if kind == "report" and self.data is not None and str(self.data["fingerprint"]) == fingerprint(self.cfg):
                cached = self.job_dir / "cached_map.npz"
                np.savez_compressed(cached, **self.data)
                request["cached_result"] = str(cached)
            path = self.job_dir / "request.json"
            path.write_text(json.dumps(request, indent=2) + "\n")
            self.log_handle = (self.job_dir / "worker.log").open("w")
            env = os.environ.copy()
            for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "MKL_NUM_THREADS"):
                env[key] = "2"
            env["MPLBACKEND"] = "Agg"
            self.process = subprocess.Popen([sys.executable, "-u", str(PROJECT / "subwoofer_model.py"),
                                             "--request", str(path), "--output", str(self.job_dir)],
                                            cwd=str(PROJECT), env=env, stdout=self.log_handle, stderr=subprocess.STDOUT)
        except (OSError, ValueError) as exc:
            self._cleanup_job()
            W.QMessageBox.warning(self, "Cannot start calculation", str(exc))
            return
        self.cancelled = False
        self.worker_starts += 1
        self._buttons()
        self.status.setText({"calculate": "Calculating all frequency maps…", "optimize": "Searching subwoofer placements…",
                             "report": "Creating a compact PDF from the captured settings…"}[kind])
        self.poll_timer.start()

    def _poll(self):
        if self.process is None or self.process.poll() is None:
            return
        code = self.process.returncode
        self.process = None
        self.poll_timer.stop()
        self.log_handle.close()
        self.log_handle = None
        try:
            if self.cancelled:
                self.status.setText("Calculation cancelled.")
            elif code != 0:
                log = (self.job_dir / "worker.log").read_text(errors="replace")
                W.QMessageBox.warning(self, "Calculation failed", log[-3000:])
                self.status.setText("Calculation failed. Check the model settings.")
                self.playing = False
            elif self.job_kind == "report":
                self.last_report = self.job_dir / "subwoofer_report.pdf"
                self.status.setText(f"Report ready: {self.last_report}" + (" · settings changed during export" if self.revision != self.job_revision else ""))
                if self.revision == self.job_revision:
                    self.data = load_result(self.job_dir / "map_data.npz")
                    self._show_current_data()
            elif self.revision != self.job_revision or not self.form_valid:
                self.status.setText("Layout changed during calculation; updating the preview…" if self.playing
                                    else "Layout changed during calculation. Press Play to calculate the current layout.")
            else:
                if self.job_kind == "optimize":
                    comparison = json.loads((self.job_dir / "optimization.json").read_text())
                    self.undo_config = copy.deepcopy(self.cfg)
                    self._commit(comparison["best_config"], manual=False)
                    self.optimization = comparison
                    self.playing = True
                    self._load_fields()
                    before, after = comparison["baseline_metrics"]["score_db"], comparison["best_metrics"]["score_db"]
                    self.status.setText(f"Search applied · score {before:.2f} → {after:.2f} dB. Undo restores the previous placement.")
                else:
                    self.status.setText("Live preview ready. Drag positions or change frequency.")
                self.data = load_result(self.job_dir / "map_data.npz")
                self._show_current_data()
        except (ValueError, OSError, KeyError) as exc:
            self.playing = False
            W.QMessageBox.warning(self, "Cannot read calculation", str(exc))
        finally:
            self._cleanup_job()
            self._buttons()
            if self.playing and not self.cancelled:
                self.debounce.start()

    def _cleanup_job(self):
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None
        if self.job_temp:
            self.job_temp.cleanup()
            self.job_temp = None

    def cancel_job(self):
        self.playing = False
        self.debounce.stop()
        if self.process is not None:
            self.cancelled = True
            self.process.terminate()
            self.cancel_button.setEnabled(False)
        self._buttons()

    def stop_worker(self):
        self.debounce.stop()
        self.poll_timer.stop()
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None
        self._cleanup_job()

    def undo_optimization(self):
        if self.undo_config is None or self.process is not None:
            return
        previous = self.undo_config
        self._commit(previous)
        self._load_fields()
        self.playing = True
        self._refresh_if_playing()

    def save_config(self):
        if not self.form_valid:
            W.QMessageBox.warning(self, "Check entries", self.error_label.text())
            return
        path, _ = W.QFileDialog.getSaveFileName(self, "Save subwoofer configuration", str(self.config_path), "Configuration (*.json)")
        if path:
            try:
                Path(path).write_text(json.dumps(self.cfg, indent=2) + "\n")
            except OSError as exc:
                W.QMessageBox.warning(self, "Cannot save", str(exc))
                return
            self.config_path, self.saved_revision = Path(path), self.revision
            self.setWindowTitle("Subwoofer placement studio")

    def load_config(self):
        if self.revision != self.saved_revision:
            if W.QMessageBox.question(self, "Load configuration", "Discard unsaved layout edits and load another configuration?") != W.QMessageBox.StandardButton.Yes:
                return
        path, _ = W.QFileDialog.getOpenFileName(self, "Load subwoofer or rectangular-room configuration", str(PROJECT), "Configuration (*.json)")
        if path:
            try:
                cfg = validate_config(json.loads(Path(path).read_text()))
            except (ValueError, KeyError, TypeError, OSError) as exc:
                W.QMessageBox.warning(self, "Cannot load", str(exc))
                return
            self.stop_worker()
            self.selected = "sub:0"
            self.form_valid = True
            self.error_label.setText("")
            self._commit(cfg)
            self.config_path, self.saved_revision = Path(path), self.revision
            self.setWindowTitle("Subwoofer placement studio")
            self._load_fields()

    def browse_output(self):
        folder = W.QFileDialog.getExistingDirectory(self, "Choose report folder", self.output_field.text())
        if folder:
            self.output_field.setText(folder)

    def open_report(self):
        if self.last_report:
            G.QDesktopServices.openUrl(C.QUrl.fromLocalFile(str(self.last_report)))

    def closeEvent(self, event):
        if self.process is not None or self.revision != self.saved_revision:
            if W.QMessageBox.question(self, "Close studio", "Close the editor, stop any calculation, and discard unsaved edits?") != W.QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.stop_worker()
        event.accept()


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PROJECT / "subwoofer_config.json")
    args = parser.parse_args()
    prepare_qt_plugins()
    app = W.QApplication(sys.argv[:1])
    if sys.platform == "darwin":
        app.setFont(G.QFont("Helvetica", 13))
    try:
        window = SubwooferEditor(args.config)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        W.QMessageBox.critical(None, "Cannot open configuration", str(exc))
        return 1
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
