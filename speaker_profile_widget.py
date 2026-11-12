"""Lightweight Qt-only preview of an imported speaker response."""
from __future__ import annotations

import math
import numpy as np
from PySide6 import QtCore as C, QtGui as G, QtWidgets as W
from speaker_profile import profile_response


class SpeakerProfilePlot(W.QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(185)
        self.profile = None
        self.speaker_name = "Flat reference"
        self.points = None

    def set_profile(self, profile, name):
        self.profile, self.speaker_name = profile, name
        lo, hi = (20.0, 20000.0) if profile is None else (profile["frequency_hz"][0], profile["frequency_hz"][-1])
        freq = np.geomspace(lo, hi, 600)
        values = 20 * np.log10(np.maximum(np.abs(profile_response(profile, freq)), 1e-12) / 20e-6)
        self.points = freq, values
        self.update()

    def paintEvent(self, event):
        painter = G.QPainter(self)
        painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), G.QColor("white"))
        if self.points is None:
            return
        freq, values = self.points
        finite = values[np.isfinite(values)]
        top = math.ceil(float(finite.max()) / 10) * 10 + 5 if len(finite) else 100
        bottom = top - 70
        rect = C.QRectF(58, 27, max(20, self.width() - 80), max(20, self.height() - 67))
        left, right = math.log10(freq[0]), math.log10(freq[-1])

        def point(f, db):
            return C.QPointF(rect.left() + (math.log10(f) - left) / (right - left) * rect.width(),
                            rect.bottom() - (db - bottom) / (top - bottom) * rect.height())

        painter.setPen(G.QColor("#101828"))
        font = painter.font(); font.setPointSize(10); painter.setFont(font)
        painter.drawText(C.QRectF(5, 2, self.width() - 10, 22), C.Qt.AlignmentFlag.AlignCenter,
                         "Speaker design · on-axis 1 m response (dB SPL)")
        for value in range(int(bottom), int(top) + 1, 20):
            y = point(freq[0], value).y()
            painter.setPen(G.QPen(G.QColor("#e5eaf1"), 1))
            painter.drawLine(C.QPointF(rect.left(), y), C.QPointF(rect.right(), y))
            painter.setPen(G.QColor("#475467"))
            painter.drawText(C.QRectF(1, y-9, 49, 18), C.Qt.AlignmentFlag.AlignRight | C.Qt.AlignmentFlag.AlignVCenter, str(value))
        for f in (10, 20, 100, 1000, 10000, 20000):
            if freq[0] <= f <= freq[-1]:
                x = point(f, bottom).x()
                painter.setPen(G.QPen(G.QColor("#e5eaf1"), 1))
                painter.drawLine(C.QPointF(x, rect.top()), C.QPointF(x, rect.bottom()))
                painter.setPen(G.QColor("#475467"))
                painter.drawText(C.QRectF(x-22, rect.bottom()+5, 44, 18), C.Qt.AlignmentFlag.AlignCenter,
                                 f"{f//1000}k" if f >= 1000 else str(f))
        path = G.QPainterPath(); started = False
        for f, value in zip(freq, values):
            if not np.isfinite(value):
                started = False
                continue
            p = point(f, float(np.clip(value, bottom, top)))
            if started:
                path.lineTo(p)
            else:
                path.moveTo(p); started = True
        painter.save(); painter.setClipRect(rect)
        painter.setPen(G.QPen(G.QColor("#2563eb"), 2)); painter.drawPath(path)
        painter.restore()
        painter.setPen(G.QPen(G.QColor("#98a2b3"), 1)); painter.drawRect(rect)
