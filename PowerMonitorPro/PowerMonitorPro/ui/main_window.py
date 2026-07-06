import csv
import time
import math
import collections
from pathlib import Path

import pyqtgraph as pg

from PySide6.QtCore import Qt, QTimer, Slot, QSettings
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QPushButton,
    QFrame,
    QFileDialog,
    QComboBox,
    QCheckBox,
    QSlider,
    QPlainTextEdit,
    QSplitter,
)

from config import *
from models import Sample, DeviceCapabilities, DeviceInfo
from helpers import coulombs_to_mah, joules_to_wh, guess_protocol, clamp_min_zero, parse_bool
from backends import FNIRSIBackend, SUPPORTED_PROFILES, AtorchT3Backend, RP2040INA260Backend, ESP32INA260Backend, MainboardMedicsBackend

pg.setConfigOptions(useOpenGL=False)

# =========================================================
# SMOOTHING
# =========================================================

class SmoothingEngine:
    MODES = {
        "Off": 1,
        "Light": 4,
        "Heavy": 10,
    }

    def __init__(self):
        self.buffers = {
            "voltage_v": collections.deque(maxlen=20),
            "current_a": collections.deque(maxlen=20),
            "power_w": collections.deque(maxlen=20),
            "temp_c": collections.deque(maxlen=20),
            "dp_v": collections.deque(maxlen=20),
            "dn_v": collections.deque(maxlen=20),
        }

    def reset(self):
        for buf in self.buffers.values():
            buf.clear()

    def apply(self, sample: Sample, mode: str) -> Sample:
        window = self.MODES.get(mode, 1)
        if window <= 1:
            return sample

        for key in self.buffers.keys():
            self.buffers[key].append(getattr(sample, key))

        def avg(key: str) -> float:
            vals = list(self.buffers[key])[-window:]
            if not vals:
                return getattr(sample, key)
            return sum(vals) / len(vals)

        return Sample(
            voltage_v=avg("voltage_v"),
            current_a=avg("current_a"),
            power_w=avg("power_w"),
            dp_v=avg("dp_v"),
            dn_v=avg("dn_v"),
            temp_c=avg("temp_c"),
            timestamp=sample.timestamp,
        )


# =========================================================
# STATS MANAGER
# =========================================================

class SessionStats:
    def __init__(self):
        self.reset()

    def reset(self):
        self.max_v = 0.0
        self.max_c = 0.0
        self.max_p = 0.0
        self.min_v = math.inf
        self.min_c = math.inf
        self.min_p = math.inf

        self.charge_coulombs = 0.0
        self.energy_joules = 0.0
        self.last_sample_time = None
        self.session_start_time = time.time()

        self.v_window = collections.deque()
        self.c_window = collections.deque()
        self.p_window = collections.deque()

        self.prev_current = 0.0

    def update(self, raw_sample: Sample):
        now = raw_sample.timestamp

        if self.last_sample_time is not None:
            dt = max(0.0, now - self.last_sample_time)
            self.charge_coulombs += raw_sample.current_a * dt
            self.energy_joules += raw_sample.power_w * dt
        self.last_sample_time = now

        self.max_v = max(self.max_v, raw_sample.voltage_v)
        self.max_c = max(self.max_c, raw_sample.current_a)
        self.max_p = max(self.max_p, raw_sample.power_w)

        self.min_v = min(self.min_v, raw_sample.voltage_v)
        self.min_c = min(self.min_c, raw_sample.current_a)
        self.min_p = min(self.min_p, raw_sample.power_w)

        self._append_window(self.v_window, now, raw_sample.voltage_v)
        self._append_window(self.c_window, now, raw_sample.current_a)
        self._append_window(self.p_window, now, raw_sample.power_w)

    def _append_window(self, dq, ts, value):
        dq.append((ts, value))
        cutoff = ts - max(DISPLAY_AVG_SECONDS, DISPLAY_PEAK_SECONDS)
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def avg_v(self):
        return self._avg(self.v_window, DISPLAY_AVG_SECONDS)

    def avg_c(self):
        return self._avg(self.c_window, DISPLAY_AVG_SECONDS)

    def avg_p(self):
        return self._avg(self.p_window, DISPLAY_AVG_SECONDS)

    def recent_peak_v(self):
        return self._peak(self.v_window, DISPLAY_PEAK_SECONDS)

    def recent_peak_c(self):
        return self._peak(self.c_window, DISPLAY_PEAK_SECONDS)

    def recent_peak_p(self):
        return self._peak(self.p_window, DISPLAY_PEAK_SECONDS)

    def mah(self):
        return coulombs_to_mah(self.charge_coulombs)

    def wh(self):
        return joules_to_wh(self.energy_joules)

    def elapsed_s(self):
        return time.time() - self.session_start_time

    def _avg(self, dq, seconds):
        if not dq:
            return 0.0
        cutoff = dq[-1][0] - seconds
        vals = [v for ts, v in dq if ts >= cutoff]
        if not vals:
            return 0.0
        return sum(vals) / len(vals)

    def _peak(self, dq, seconds):
        if not dq:
            return 0.0
        cutoff = dq[-1][0] - seconds
        vals = [v for ts, v in dq if ts >= cutoff]
        if not vals:
            return 0.0
        return max(vals)

    def summary_rows(self):
        min_v = 0.0 if self.min_v is math.inf else self.min_v
        min_c = 0.0 if self.min_c is math.inf else self.min_c
        min_p = 0.0 if self.min_p is math.inf else self.min_p

        return [
            [],
            ["SESSION SUMMARY"],
            ["duration_s", f"{self.elapsed_s():.3f}"],
            ["max_voltage_v", f"{self.max_v:.5f}"],
            ["max_current_a", f"{self.max_c:.5f}"],
            ["max_power_w", f"{self.max_p:.5f}"],
            ["min_voltage_v", f"{min_v:.5f}"],
            ["min_current_a", f"{min_c:.5f}"],
            ["min_power_w", f"{min_p:.5f}"],
            ["mah_total", f"{self.mah():.3f}"],
            ["wh_total", f"{self.wh():.5f}"],
        ]


# =========================================================
# LOGGING
# =========================================================

class CsvLogger:
    def __init__(self):
        self.enabled = False
        self.live_mode = False
        self.file_handle = None
        self.writer = None
        self.file_path = None
        self.rows_buffered = 0
        self.rows_memory = []
        self.header_written = False

    def reset(self):
        self.close()
        self.rows_memory = []
        self.header_written = False

    def start(self, live_mode=False, file_path=None):
        self.enabled = True
        self.live_mode = live_mode
        self.rows_buffered = 0

        if not self.rows_memory:
            self.rows_memory.append(self.header())

        if self.live_mode and file_path:
            self.file_path = file_path
            self.file_handle = open(file_path, "w", newline="", encoding="utf-8")
            self.writer = csv.writer(self.file_handle)
            self.writer.writerow(self.header())
            self.file_handle.flush()
            self.header_written = True

    def stop(self):
        self.enabled = False
        self.close()

    def close(self):
        if self.file_handle is not None:
            try:
                self.file_handle.flush()
                self.file_handle.close()
            except Exception:
                pass
        self.file_handle = None
        self.writer = None
        self.file_path = None

    def header(self):
        return [
            "timestamp",
            "elapsed_s",
            "device",
            "source_family",
            "voltage_v_raw",
            "current_a_raw",
            "power_w_raw",
            "temp_c_raw",
            "dp_v_raw",
            "dn_v_raw",
            "mah_total",
            "wh_total",
            "protocol",
            "alerts"
        ]

    def append(self, row):
        if not self.enabled:
            return

        self.rows_memory.append(row)

        if self.live_mode and self.writer is not None:
            self.writer.writerow(row)
            self.rows_buffered += 1
            if self.rows_buffered >= LIVE_LOG_FLUSH_EVERY:
                self.file_handle.flush()
                self.rows_buffered = 0

    def save_memory_csv(self, file_path, summary_rows=None):
        with open(file_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerows(self.rows_memory)
            if summary_rows:
                writer.writerows(summary_rows)


# =========================================================
# UI WIDGETS
# =========================================================

class DataBlock(QWidget):
    def __init__(self, title, unit, color):
        super().__init__()

        self.unit = unit.upper()

        layout = QVBoxLayout(self)
        layout.setSpacing(2)
        layout.setContentsMargins(10, 10, 10, 10)

        self.lbl_title = QLabel(title.upper())
        self.lbl_title.setFont(QFont("Arial", 16, QFont.Bold))
        self.lbl_title.setStyleSheet("color: white; border: none; background: transparent;")
        self.lbl_title.setAlignment(Qt.AlignCenter)

        self.lbl_value = QLabel(f"00.000 {self.unit}")
        self.lbl_value.setFont(QFont("Consolas", 75, QFont.Bold))
        self.lbl_value.setStyleSheet(f"color: {color}; border: none; background: transparent;")
        self.lbl_value.setAlignment(Qt.AlignCenter)

        layout.addWidget(self.lbl_title)
        layout.addWidget(self.lbl_value)

    def update_value(self, val, precision=3):
        self.lbl_value.setText(f"{val:0>6.{precision}f} {self.unit}")

    def set_title(self, text):
        self.lbl_title.setText(text.upper())


class MiniRow(QFrame):
    def __init__(self, color="#00FF7F", unit="V", line_color=None):
        super().__init__()

        self.unit = unit
        self.color = color
        self.graph_enabled = True

        self.setMinimumHeight(80)
        self.setStyleSheet("background: transparent; border: none;")

        self.graph = pg.PlotWidget(self)
        self.graph.setBackground(None)
        self.graph.setMouseEnabled(x=False, y=False)
        self.graph.hideAxis("bottom")
        self.graph.hideAxis("left")
        self.graph.setMenuEnabled(False)
        self.graph.setClipToView(True)
        self.graph.setXRange(-MINI_SECONDS, 0, padding=0)

        self.time_history = collections.deque(maxlen=MINI_PLOT_SIZE)
        self.data_buffer = collections.deque(maxlen=MINI_PLOT_SIZE)

        if line_color is None:
            line_color = color

        self.plot_line = self.graph.plot(
            [],
            [],
            pen=pg.mkPen(color=line_color, width=0.8, alpha=80)
        )

        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(10, 0, 10, 0)
        self.layout.setAlignment(Qt.AlignVCenter)

        self.lbl_main = QLabel(f"00.000{unit}")
        self.lbl_main.setFont(QFont("Consolas", 42, QFont.Bold))
        self.lbl_main.setStyleSheet(f"color: {color}; background: transparent;")
        self.lbl_main.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)

        self.stats_layout = QVBoxLayout()
        self.stats_layout.setSpacing(2)
        self.stats_layout.setContentsMargins(0, 0, 0, 0)
        self.stats_layout.setAlignment(Qt.AlignVCenter)

        self.lbl_peak = QLabel("0.000")
        self.lbl_avg = QLabel("0.000")

        for lbl in [self.lbl_peak, self.lbl_avg]:
            lbl.setFont(QFont("Consolas", 20, QFont.Bold))
            lbl.setStyleSheet(f"color: {color}; background: transparent;")
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.stats_layout.addWidget(lbl)

        self.layout.addWidget(self.lbl_main, alignment=Qt.AlignVCenter)
        self.layout.addStretch()
        self.layout.addLayout(self.stats_layout)

        self.start_time = time.time()

    def resizeEvent(self, event):
        self.graph.setFixedSize(self.size())
        super().resizeEvent(event)

    def set_graph_enabled(self, enabled: bool):
        self.graph_enabled = enabled
        self.graph.setVisible(enabled)

    def reset_graph(self):
        self.time_history = collections.deque(maxlen=MINI_PLOT_SIZE)
        self.data_buffer = collections.deque(maxlen=MINI_PLOT_SIZE)
        self.start_time = time.time()
        self.plot_line.setData([], [])
        self.graph.setXRange(-MINI_SECONDS, 0, padding=0)
        self.graph.setYRange(0.0, 1.0, padding=0)

    def update_row(self, val, peak, avg, unit):
        self.lbl_main.setText(f"{val:0>6.3f}{unit}")
        self.lbl_peak.setText(f"{peak:0>6.3f}")
        self.lbl_avg.setText(f"{avg:0>6.3f}")

        if not self.graph_enabled:
            return

        elapsed = time.time() - self.start_time
        self.time_history.append(elapsed)
        self.data_buffer.append(val)

        x_vals = [t - elapsed for t in self.time_history]
        y_vals = list(self.data_buffer)
        self.plot_line.setData(x_vals, y_vals)
        self.graph.setXRange(-MINI_SECONDS, 0, padding=0)

        if y_vals:
            y_min = min(y_vals)
            y_max = max(y_vals)
            y_range = y_max - y_min

            if y_range < AUTO_SCALE_MIN_RANGE:
                mid = (y_min + y_max) / 2.0
                y_min = mid - (AUTO_SCALE_MIN_RANGE / 2.0)
                y_max = mid + (AUTO_SCALE_MIN_RANGE / 2.0)
            else:
                pad = y_range * AUTO_SCALE_MARGIN
                y_min -= pad
                y_max += pad

            y_min = clamp_min_zero(y_min)
            self.graph.setYRange(y_min, y_max, padding=0)

    def refresh_graph_scroll(self):
        """Pan the mini graph smoothly between real data samples."""
        if not self.graph_enabled or not self.time_history:
            return

        elapsed = time.time() - self.start_time
        x_vals = [t - elapsed for t in self.time_history]
        y_vals = list(self.data_buffer)
        self.plot_line.setData(x_vals, y_vals)
        self.graph.setXRange(-MINI_SECONDS, 0, padding=0)


# =========================================================
# PLAYBACK CONTROL WINDOW
# =========================================================

class PlaybackControlsWindow(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.setWindowTitle("CSV Playback Controls")
        self.setMinimumWidth(560)
        self.setStyleSheet(f"background-color: {UIColors.BG_MAIN}; color: white;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        self.lbl_file = QLabel("No CSV loaded")
        self.lbl_file.setFont(QFont("Arial", 10, QFont.Bold))
        self.lbl_file.setStyleSheet("color: white;")

        self.lbl_time = QLabel("00.000s / 00.000s")
        self.lbl_time.setFont(QFont("Consolas", 10, QFont.Bold))
        self.lbl_time.setStyleSheet("color: #AAAAAA;")

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setMinimum(0)
        self.slider.setMaximum(0)
        self.slider.sliderPressed.connect(self.main_window.playback_slider_pressed)
        self.slider.sliderReleased.connect(self.main_window.playback_slider_released)
        self.slider.valueChanged.connect(self.main_window.playback_slider_changed)
        self.slider.setStyleSheet("""
            QSlider::groove:horizontal { height: 6px; background: #333; border-radius: 3px; }
            QSlider::handle:horizontal { width: 14px; background: #00BFFF; margin: -5px 0; border-radius: 7px; }
        """)

        row = QHBoxLayout()
        row.setSpacing(8)

        self.btn_play = QPushButton("PAUSE")
        self.btn_play.clicked.connect(self.main_window.toggle_playback_pause)

        self.btn_restart = QPushButton("RESTART")
        self.btn_restart.clicked.connect(self.main_window.restart_playback)

        self.btn_stop = QPushButton("STOP")
        self.btn_stop.clicked.connect(self.main_window.stop_playback)

        self.cmb_speed = QComboBox()
        self.cmb_speed.addItems(PLAYBACK_SPEEDS)
        self.cmb_speed.setCurrentText("1x")
        self.cmb_speed.currentTextChanged.connect(self.main_window.set_playback_speed)
        self.cmb_speed.setFixedWidth(90)

        for btn in [self.btn_play, self.btn_restart, self.btn_stop]:
            btn.setFixedHeight(28)
            btn.setStyleSheet("background: #333; color: white; border-radius: 4px; font-size: 10px;")

        self.cmb_speed.setStyleSheet("""
            QComboBox {
                background: #2a2a2a;
                color: white;
                border: 1px solid #555;
                padding: 2px 6px;
                min-height: 24px;
            }
        """)

        row.addWidget(self.btn_play)
        row.addWidget(self.btn_restart)
        row.addWidget(QLabel("Speed:"))
        row.addWidget(self.cmb_speed)
        row.addStretch()
        row.addWidget(self.btn_stop)

        layout.addWidget(self.lbl_file)
        layout.addWidget(self.lbl_time)
        layout.addWidget(self.slider)
        layout.addLayout(row)

    def set_loaded_file(self, file_path: str):
        self.lbl_file.setText(f"Playback: {file_path}")

    def set_range(self, max_index: int):
        self.slider.setMaximum(max(0, max_index))

    def set_position(self, index: int, elapsed_s: float, total_s: float):
        if not self.slider.isSliderDown():
            self.slider.blockSignals(True)
            self.slider.setValue(max(0, min(index, self.slider.maximum())))
            self.slider.blockSignals(False)
        self.lbl_time.setText(f"{elapsed_s:0.3f}s / {total_s:0.3f}s")

    def set_paused(self, paused: bool):
        self.btn_play.setText("PLAY" if paused else "PAUSE")

    def closeEvent(self, event):
        self.main_window.stop_playback()
        super().closeEvent(event)


# =========================================================
# DIAGNOSTICS / EXTRA INFO WINDOW
# =========================================================

class DiagnosticsWindow(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.raw_paused = False
        self.raw_seen = set()
        self.raw_lines = collections.deque(maxlen=1000)

        self.setWindowTitle("Device Diagnostics / Extra Info")
        self.setMinimumSize(780, 680)
        self.setStyleSheet(f"background-color: {UIColors.BG_MAIN}; color: white;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        self.lbl_title = QLabel("Device Diagnostics")
        self.lbl_title.setFont(QFont("Arial", 12, QFont.Bold))
        self.lbl_title.setStyleSheet(f"color: {UIColors.CURRENT}; background: transparent;")

        self.lbl_debug = QLabel("Backend Debug")
        self.lbl_debug.setFont(QFont("Arial", 10, QFont.Bold))
        self.lbl_debug.setStyleSheet("color: white; background: transparent;")

        self.txt_debug = QPlainTextEdit()
        self.txt_debug.setReadOnly(True)
        self.txt_debug.setFont(QFont("Consolas", 10))
        self.txt_debug.setMinimumHeight(230)
        self.txt_debug.setStyleSheet("""
            QPlainTextEdit {
                background: #050505;
                color: #DDDDDD;
                border: 1px solid #333;
                padding: 8px;
            }
        """)

        self.lbl_raw = QLabel("Raw Data / Device Responses")
        self.lbl_raw.setFont(QFont("Arial", 10, QFont.Bold))
        self.lbl_raw.setStyleSheet(f"color: {UIColors.CURRENT}; background: transparent;")

        self.txt_raw = QPlainTextEdit()
        self.txt_raw.setReadOnly(True)
        self.txt_raw.setFont(QFont("Consolas", 9))
        self.txt_raw.setMinimumHeight(260)
        self.txt_raw.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.txt_raw.setStyleSheet("""
            QPlainTextEdit {
                background: #000000;
                color: #00FF7F;
                border: 1px solid #333;
                padding: 8px;
            }
        """)

        row = QHBoxLayout()
        self.btn_refresh = QPushButton("REFRESH")
        self.btn_refresh.clicked.connect(self.update_info)

        self.btn_pause_raw = QPushButton("PAUSE RAW")
        self.btn_pause_raw.clicked.connect(self.toggle_raw_pause)

        self.btn_clear_raw = QPushButton("CLEAR RAW")
        self.btn_clear_raw.clicked.connect(self.clear_raw)

        self.btn_copy_raw = QPushButton("COPY RAW")
        self.btn_copy_raw.clicked.connect(self.copy_raw)

        self.btn_close = QPushButton("CLOSE")
        self.btn_close.clicked.connect(self.hide)

        for btn in [self.btn_refresh, self.btn_pause_raw, self.btn_clear_raw, self.btn_copy_raw, self.btn_close]:
            btn.setFixedHeight(28)
            btn.setStyleSheet("background: #333; color: white; border-radius: 4px; font-size: 10px;")

        row.addWidget(self.btn_refresh)
        row.addWidget(self.btn_pause_raw)
        row.addWidget(self.btn_clear_raw)
        row.addWidget(self.btn_copy_raw)
        row.addStretch()
        row.addWidget(self.btn_close)

        # Resizable splitter: drag the handle to give more space to Backend Debug or Raw Data.
        debug_pane = QWidget()
        debug_layout = QVBoxLayout(debug_pane)
        debug_layout.setContentsMargins(0, 0, 0, 0)
        debug_layout.setSpacing(4)
        debug_layout.addWidget(self.lbl_debug)
        debug_layout.addWidget(self.txt_debug)

        raw_pane = QWidget()
        raw_layout = QVBoxLayout(raw_pane)
        raw_layout.setContentsMargins(0, 0, 0, 0)
        raw_layout.setSpacing(4)
        raw_layout.addWidget(self.lbl_raw)
        raw_layout.addWidget(self.txt_raw)

        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.addWidget(debug_pane)
        self.splitter.addWidget(raw_pane)
        self.splitter.setSizes([260, 360])
        self.splitter.setStyleSheet("""
            QSplitter::handle {
                background: #333;
                height: 6px;
            }
            QSplitter::handle:hover {
                background: #00BFFF;
            }
        """)

        layout.addWidget(self.lbl_title)
        layout.addWidget(self.splitter, stretch=1)
        layout.addLayout(row)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_info)
        self.timer.start(500)

    def toggle_raw_pause(self):
        self.raw_paused = not self.raw_paused
        self.btn_pause_raw.setText("RESUME RAW" if self.raw_paused else "PAUSE RAW")

    def clear_raw(self):
        self.raw_seen.clear()
        self.raw_lines.clear()
        self.txt_raw.clear()

    def copy_raw(self):
        self.txt_raw.selectAll()
        self.txt_raw.copy()

    def _raw_from_backend(self, backend):
        if backend is None:
            return None

        # FNIRSI / ATORCH HID packets
        packet_hex = getattr(backend, "last_packet_hex", "")
        if packet_hex:
            return f"{backend.current_device_name()} RAW HEX: {packet_hex}"

        # RP2040 serial line
        last_line = getattr(backend, "last_line", "")
        if last_line:
            return f"{backend.current_device_name()} SERIAL: {last_line}"

        return None

    def _append_raw_line(self, line):
        if not line or self.raw_paused:
            return

        # Do not dedupe here: repeated serial lines are useful when checking real update rate.
        timestamp = time.strftime("%H:%M:%S")
        self.raw_lines.append(f"[{timestamp}] {line}")

        scrollbar = self.txt_raw.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 2
        self.txt_raw.setPlainText("\n".join(self.raw_lines))
        if at_bottom:
            self.txt_raw.verticalScrollBar().setValue(self.txt_raw.verticalScrollBar().maximum())

    def update_info(self):
        mw = self.main_window
        backend = mw.active_backend

        total_attempts = mw.samples_received + mw.samples_dropped
        quality = (mw.samples_received / total_attempts * 100.0) if total_attempts else 100.0

        lines = []
        lines.append("POWER MONITOR PRO v2.2 - DIAGNOSTICS")
        lines.append("=" * 72)
        lines.append(f"Playback active : {mw.playback_active}")
        lines.append(f"Graph mode      : {mw.graph_mode}")
        lines.append(f"Smooth mode     : {mw.smoothing_mode}")
        lines.append("")
        lines.append("LIVE RATE")
        lines.append("-" * 72)
        lines.append(f"Rate            : {mw.current_hz:.1f} Hz")
        lines.append(f"Samples received: {mw.samples_received}")
        lines.append(f"Dropped/disconn. : {mw.samples_dropped}")
        lines.append(f"Quality         : {quality:.1f}%")
        lines.append("")

        if backend is None:
            lines.append("DEVICE")
            lines.append("-" * 72)
            lines.append("No live device currently active.")
            if mw.playback_active:
                lines.append("CSV playback is active.")
        else:
            lines.append("DEVICE")
            lines.append("-" * 72)
            lines.append(f"Name            : {backend.current_device_name()}")
            lines.append(f"Family/source   : {backend.source_label} / {backend.source_key}")
            try:
                lines.append(f"Connected       : {backend.is_connected()}")
            except Exception:
                lines.append("Connected       : unknown")
            lines.append("")
            lines.append("BACKEND DEBUG")
            lines.append("-" * 72)
            try:
                debug = backend.debug_info()
            except Exception as e:
                debug = {"debug_error": str(e)}

            if isinstance(debug, dict):
                for k, v in debug.items():
                    # Keep large raw packet/serial values in the Raw Data box below.
                    if str(k).lower() in ("last_packet_hex", "last_line"):
                        continue
                    lines.append(f"{k:<18}: {v}")
            else:
                lines.append(str(debug))

        self.txt_debug.setPlainText("\n".join(lines))
        self._append_raw_line(self._raw_from_backend(backend))

    def showEvent(self, event):
        self.update_info()
        super().showEvent(event)

# =========================================================
# MAIN WINDOW
# =========================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.settings = QSettings(APP_ORG, APP_NAME)

        self.backends = [
            FNIRSIBackend(SUPPORTED_PROFILES),
            MainboardMedicsBackend(),
            RP2040INA260Backend(),
            ESP32INA260Backend(),
            AtorchT3Backend(), 
        ]
        self.active_backend = None
        self.active_caps = DeviceCapabilities()
        self.last_reconnect_attempt = 0.0
        self.device_registry = {}

        self.stats = SessionStats()
        self.smoothing = SmoothingEngine()
        self.logger = CsvLogger()

        self.playback_rows = []
        self.playback_active = False
        self.playback_paused = False
        self.playback_index = 0
        self.playback_elapsed = 0.0
        self.playback_speed = 1.0
        self.playback_last_tick = time.time()
        self.playback_window = None
        self.diagnostics_window = None
        self.playback_slider_dragging = False

        # Live diagnostics / sample-rate tracking
        self.samples_received = 0
        self.samples_dropped = 0
        self.rate_window = collections.deque(maxlen=500)
        self.current_hz = 0.0

        self.live_csv_path = None
        self._updating_live_checkbox = False

        self.is_mini = False
        self.oldPos = None
        self.drag_locked = False
        self.sticky_alerts = False
        self.active_alerts_sticky = set()

        self.graph_mode = "Current"
        self.smoothing_mode = "Off"

        self.plot_times = collections.deque(maxlen=PLOT_SIZE)
        self.plot_current = collections.deque(maxlen=PLOT_SIZE)
        self.plot_voltage = collections.deque(maxlen=PLOT_SIZE)
        self.plot_power = collections.deque(maxlen=PLOT_SIZE)
        self.plot_start_time = time.time()

        self.init_ui()
        self.load_settings()
        self.scan_available_devices()
        self.update_control_layout_for_mode()

        # Data/update timer stays at UI_INTERVAL_MS so device polling remains stable.
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_loop)
        self.timer.start(UI_INTERVAL_MS)

        # Separate visual render timer pans graphs smoothly between samples.
        self.render_timer = QTimer()
        self.render_timer.timeout.connect(self.refresh_graph_scroll)
        self.render_timer.start(RENDER_INTERVAL_MS)

    def make_separator(self):
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color: #2f2f2f;")
        line.setFixedHeight(1)
        return line

    def make_button(self, text, slot, width):
        btn = QPushButton(text)
        btn.clicked.connect(slot)
        btn.setFixedSize(width, 24)
        btn.setStyleSheet(
            "background: #333; color: white; border-radius: 4px; font-size: 10px;"
        )
        return btn

    # -----------------------------------------------------
    # UI
    # -----------------------------------------------------

    def init_ui(self):
        self.setWindowTitle("POWER MONITOR PRO v2.2")
        self.setStyleSheet(f"background-color: {UIColors.BG_MAIN};")

        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.base_layout = QVBoxLayout(self.central_widget)


        # header row 1
        self.device_ctrl_widget = QWidget()
        self.device_ctrl_layout = QHBoxLayout(self.device_ctrl_widget)
        self.device_ctrl_layout.setContentsMargins(10, 2, 10, 6)
        self.device_ctrl_layout.setSpacing(8)

        self.cmb_device = QComboBox()
        self.cmb_device.setFixedWidth(300)
        self.cmb_device.currentIndexChanged.connect(self.on_device_selection_changed)

        self.btn_refresh_devices = self.make_button("REFRESH", self.scan_available_devices, 80)
        self.btn_connect_device = self.make_button("CONNECT", self.manual_connect_selected_device, 85)

        self.lbl_status = QLabel("OFFLINE")
        self.lbl_status.setFont(QFont("Arial", 9, QFont.Bold))
        self.lbl_status.setStyleSheet(f"color: {UIColors.STATUS_ERR};")

        self.device_ctrl_layout.addWidget(self.cmb_device)
        self.device_ctrl_layout.addWidget(self.btn_refresh_devices)
        self.device_ctrl_layout.addWidget(self.btn_connect_device)
        self.device_ctrl_layout.addStretch()
        self.device_ctrl_layout.addWidget(self.lbl_status)

        # full blocks
        self.full_container = QWidget()
        self.data_grid = QGridLayout(self.full_container)

        self.block_v = DataBlock("Voltage", "V", UIColors.VOLTAGE)
        self.block_a = DataBlock("Current", "A", UIColors.CURRENT)
        self.block_w = DataBlock("Power", "W", UIColors.POWER)
        self.block_peak = DataBlock("Peak Current", "A", UIColors.PEAK)

        self.data_grid.addWidget(self.block_v, 0, 0)
        self.data_grid.addWidget(self.block_a, 0, 1)
        self.data_grid.addWidget(self.block_w, 1, 0)
        self.data_grid.addWidget(self.block_peak, 1, 1)

        # info panel
        self.extra_panel = QWidget()
        self.extra_layout = QGridLayout(self.extra_panel)
        self.extra_layout.setContentsMargins(10, 0, 10, 0)

        self.lbl_temp = QLabel("Temp: n/a")
        self.lbl_dpdm = QLabel("D+ / D-: n/a")
        self.lbl_capacity = QLabel("mAh: 0.00")
        self.lbl_energy = QLabel("Wh: 0.000")
        self.lbl_protocol = QLabel("Protocol: n/a")
        self.lbl_rate = QLabel("Rate: 0.0 Hz   Samples: 0")
        self.lbl_quality = QLabel("Dropped: 0   Quality: 100.0%")
        self.lbl_alert = QLabel("Alerts: none")

        for lbl in [
            self.lbl_temp,
            self.lbl_dpdm,
            self.lbl_capacity,
            self.lbl_energy,
            self.lbl_protocol,
            self.lbl_rate,
            self.lbl_quality,
            self.lbl_alert
        ]:
            lbl.setFont(QFont("Consolas", 12, QFont.Bold))
            lbl.setStyleSheet("color: white; background: transparent;")

        self.lbl_alert.setStyleSheet(f"color: {UIColors.ALERT_IDLE}; background: transparent;")

        self.extra_layout.addWidget(self.lbl_temp, 0, 0)
        self.extra_layout.addWidget(self.lbl_dpdm, 0, 1)
        self.extra_layout.addWidget(self.lbl_capacity, 1, 0)
        self.extra_layout.addWidget(self.lbl_energy, 1, 1)
        self.extra_layout.addWidget(self.lbl_protocol, 2, 0)
        self.extra_layout.addWidget(self.lbl_alert, 2, 1)
        self.extra_layout.addWidget(self.lbl_rate, 3, 0)
        self.extra_layout.addWidget(self.lbl_quality, 3, 1)

        # main graph
        self.main_graph = pg.PlotWidget()
        self.main_graph.setBackground(UIColors.BG_MAIN)
        self.main_graph.setXRange(0, PLOT_SECONDS, padding=0)
        self.main_graph.showGrid(x=True, y=True, alpha=0.2)
        self.main_graph.setMenuEnabled(False)
        self.main_graph.setLabel("left", "Value")
        self.main_graph.setLabel("bottom", "Time", units="s")
        self.plot_line = self.main_graph.plot([], [], pen=pg.mkPen(color=UIColors.CURRENT, width=2))

        # mini mode
        self.mini_container = QWidget()
        self.mini_layout = QVBoxLayout(self.mini_container)
        self.mini_layout.setSpacing(0)
        self.mini_layout.setContentsMargins(0, 0, 0, 0)

        self.mini_v = MiniRow(UIColors.VOLTAGE, "V", line_color="#FF4040")
        self.mini_c = MiniRow(UIColors.CURRENT, "A", line_color="#FFFFFF")

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #333;")

        self.mini_layout.addWidget(self.mini_v)
        self.mini_layout.addWidget(sep)
        self.mini_layout.addWidget(self.mini_c)
        self.mini_container.hide()

        # control rows
        self.ctrl_widget = QWidget()
        self.ctrl_outer = QVBoxLayout(self.ctrl_widget)
        self.ctrl_outer.setContentsMargins(10, 6, 10, 6)
        self.ctrl_outer.setSpacing(6)

        # row 3: actions
        self.ctrl_row_actions = QWidget()
        self.ctrl_row_actions_layout = QHBoxLayout(self.ctrl_row_actions)
        self.ctrl_row_actions_layout.setContentsMargins(0, 0, 0, 0)
        self.ctrl_row_actions_layout.setSpacing(8)

        self.btn_reset = self.make_button("RESET", self.reset_data, 78)
        self.btn_log = self.make_button("START LOG", self.toggle_logging, 95)
        self.btn_save = self.make_button("LOAD CSV", self.load_csv_dialog, 90)
        self.btn_mode = self.make_button("MINI MODE", self.toggle_mode, 95)
        self.btn_clear_alert = self.make_button("CLEAR ALERTS", self.clear_sticky_alerts, 110)
        self.btn_diagnostics = self.make_button("EXTRA INFO", self.show_diagnostics_window, 95)

        self.btn_close = QPushButton("✕")
        self.btn_close.clicked.connect(self.close)
        self.btn_close.setFixedSize(24, 24)
        self.btn_close.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #888;
                border: none;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                color: #ff4c4c;
            }
        """)
        self.btn_close.hide()

        self.ctrl_row_actions_layout.addWidget(self.btn_reset)
        self.ctrl_row_actions_layout.addWidget(self.btn_log)
        self.ctrl_row_actions_layout.addWidget(self.btn_save)
        self.ctrl_row_actions_layout.addWidget(self.btn_mode)
        self.ctrl_row_actions_layout.addWidget(self.btn_clear_alert)
        self.ctrl_row_actions_layout.addWidget(self.btn_diagnostics)
        self.ctrl_row_actions_layout.addStretch()
        self.ctrl_row_actions_layout.addWidget(self.btn_close)

        # row 4: selectors
        self.ctrl_row_selectors = QWidget()
        self.ctrl_row_selectors_layout = QHBoxLayout(self.ctrl_row_selectors)
        self.ctrl_row_selectors_layout.setContentsMargins(0, 0, 0, 0)
        self.ctrl_row_selectors_layout.setSpacing(8)

        self.lbl_graph = QLabel("Graph:")
        self.cmb_graph = QComboBox()
        self.cmb_graph.addItems(["Current", "Voltage", "Power"])
        self.cmb_graph.currentTextChanged.connect(self.set_graph_mode)
        self.cmb_graph.setFixedWidth(110)

        self.lbl_smooth = QLabel("Smooth:")
        self.cmb_smooth = QComboBox()
        self.cmb_smooth.addItems(["Off", "Light", "Heavy"])
        self.cmb_smooth.currentTextChanged.connect(self.set_smoothing_mode)
        self.cmb_smooth.setFixedWidth(110)

        self.ctrl_row_selectors_layout.addWidget(self.lbl_graph)
        self.ctrl_row_selectors_layout.addWidget(self.cmb_graph)
        self.ctrl_row_selectors_layout.addSpacing(14)
        self.ctrl_row_selectors_layout.addWidget(self.lbl_smooth)
        self.ctrl_row_selectors_layout.addWidget(self.cmb_smooth)
        self.ctrl_row_selectors_layout.addStretch()

        # row 5: options
        self.ctrl_row_options = QWidget()
        self.ctrl_row_options_layout = QHBoxLayout(self.ctrl_row_options)
        self.ctrl_row_options_layout.setContentsMargins(0, 0, 0, 0)
        self.ctrl_row_options_layout.setSpacing(12)

        self.chk_live_log = QCheckBox("Enable CSV Logging")
        self.chk_live_log.setStyleSheet("color: white;")
        self.chk_live_log.toggled.connect(self.on_live_csv_toggled)

        self.chk_sticky = QCheckBox("Sticky Alerts")
        self.chk_sticky.setStyleSheet("color: white;")
        self.chk_sticky.toggled.connect(self.set_sticky_alerts)

        self.chk_top = QCheckBox("On Top")
        self.chk_top.setStyleSheet("color: white;")
        self.chk_top.toggled.connect(self.set_always_on_top)

        self.chk_mini_graph = QCheckBox("Mini Graph")
        self.chk_mini_graph.setStyleSheet("color: white;")
        self.chk_mini_graph.setChecked(True)
        self.chk_mini_graph.toggled.connect(self.set_mini_graph_enabled)

        self.chk_drag_lock = QCheckBox("Lock Drag")
        self.chk_drag_lock.setStyleSheet("color: white;")
        self.chk_drag_lock.toggled.connect(self.set_drag_lock)

        self.ctrl_row_options_layout.addWidget(self.chk_live_log)
        self.ctrl_row_options_layout.addWidget(self.chk_sticky)
        self.ctrl_row_options_layout.addWidget(self.chk_top)
        self.ctrl_row_options_layout.addWidget(self.chk_mini_graph)
        self.ctrl_row_options_layout.addWidget(self.chk_drag_lock)
        self.ctrl_row_options_layout.addStretch()

        self.sep_actions_selectors = self.make_separator()
        self.sep_selectors_options = self.make_separator()

        self.ctrl_outer.addWidget(self.ctrl_row_actions)
        self.ctrl_outer.addWidget(self.sep_actions_selectors)
        self.ctrl_outer.addWidget(self.ctrl_row_selectors)
        self.ctrl_outer.addWidget(self.sep_selectors_options)
        self.ctrl_outer.addWidget(self.ctrl_row_options)

        common_style = """
            QLabel {
                color: white;
                background: transparent;
            }
            QComboBox {
                background: #2a2a2a;
                color: white;
                border: 1px solid #555;
                padding: 2px 6px;
                min-height: 22px;
            }
            QCheckBox {
                color: white;
                spacing: 5px;
            }
        """
        self.ctrl_widget.setStyleSheet(common_style)
        self.device_ctrl_widget.setStyleSheet(common_style)

        self.base_layout.addWidget(self.device_ctrl_widget)
        self.base_layout.addWidget(self.full_container)
        self.base_layout.addWidget(self.extra_panel)
        self.base_layout.addWidget(self.ctrl_widget)
        self.base_layout.addWidget(self.main_graph)

        self.resize(1200, 980)

    # -----------------------------------------------------
    # settings
    # -----------------------------------------------------

    def _geometry_key(self):
        selected = self.settings.value("selected_device_id", AUTO_DEVICE_ID)
        clean = str(selected).replace(":", "_")
        return f"geometry_{clean}"

    def load_settings(self):
        self.is_mini = parse_bool(self.settings.value("is_mini", False))
        self.graph_mode = self.settings.value("graph_mode", "Current")
        self.smoothing_mode = self.settings.value("smoothing_mode", "Off")

        sticky = parse_bool(self.settings.value("sticky_alerts", False))
        top = parse_bool(self.settings.value("always_on_top", False))
        mini_graph = parse_bool(self.settings.value("mini_graph", True))
        drag_locked = parse_bool(self.settings.value("drag_locked", False))

        self.cmb_graph.setCurrentText(self.graph_mode)
        self.cmb_smooth.setCurrentText(self.smoothing_mode)
        self.chk_sticky.setChecked(sticky)
        self.chk_top.setChecked(top)
        self.chk_mini_graph.setChecked(mini_graph)
        self.chk_drag_lock.setChecked(drag_locked)

        geom = self.settings.value(self._geometry_key())
        if geom is not None:
            self.restoreGeometry(geom)

        if self.is_mini:
            self.toggle_mode(force_state=True)

    def save_settings(self):
        self.settings.setValue("is_mini", self.is_mini)
        self.settings.setValue("graph_mode", self.graph_mode)
        self.settings.setValue("smoothing_mode", self.smoothing_mode)
        self.settings.setValue("sticky_alerts", self.sticky_alerts)
        self.settings.setValue("always_on_top", self.chk_top.isChecked())
        self.settings.setValue("mini_graph", self.chk_mini_graph.isChecked())
        self.settings.setValue("drag_locked", self.drag_locked)
        self.settings.setValue("selected_device_id", self.current_selected_device_id())
        self.settings.setValue(self._geometry_key(), self.saveGeometry())

    # -----------------------------------------------------
    # device selection
    # -----------------------------------------------------

    def current_selected_device_id(self):
        data = self.cmb_device.currentData()
        return data if data is not None else AUTO_DEVICE_ID

    def scan_available_devices(self):
        current_id = self.current_selected_device_id()
        saved_id = self.settings.value("selected_device_id", AUTO_DEVICE_ID)

        keep_id = current_id if current_id != AUTO_DEVICE_ID else saved_id

        self.device_registry = {}
        devices = []

        for backend in self.backends:
            try:
                devices.extend(backend.list_devices())
            except Exception:
                pass

        self.cmb_device.blockSignals(True)
        self.cmb_device.clear()
        self.cmb_device.addItem("Auto (first available)", AUTO_DEVICE_ID)

        found_ids = {AUTO_DEVICE_ID}

        for info in devices:
            self.device_registry[info.device_id] = info
            self.cmb_device.addItem(info.label, info.device_id)
            found_ids.add(info.device_id)

        # Keep selected device locked even if unplugged
        if keep_id != AUTO_DEVICE_ID and keep_id not in found_ids:
            self.cmb_device.addItem("Selected device missing", keep_id)

        restore_id = keep_id if keep_id else AUTO_DEVICE_ID
        idx = self.cmb_device.findData(restore_id)
        if idx < 0:
            idx = self.cmb_device.findData(AUTO_DEVICE_ID)

        if idx >= 0:
            self.cmb_device.setCurrentIndex(idx)

        self.cmb_device.blockSignals(False)

        if self.active_backend is None:
            self.lbl_status.setText("DEVICE LIST REFRESHED")
            self.lbl_status.setStyleSheet(f"color: {UIColors.STATUS_WARN};")


    def on_device_selection_changed(self):
        selected_id = self.current_selected_device_id()
        if selected_id == AUTO_DEVICE_ID:
            self.lbl_status.setText("AUTO DEVICE SELECT")
        else:
            info = self.device_registry.get(selected_id)
            if info:
                self.lbl_status.setText(f"SELECTED: {info.label}")
            else:
                self.lbl_status.setText("DEVICE SELECTED")
        self.lbl_status.setStyleSheet(f"color: {UIColors.STATUS_WARN};")
        self.save_settings()

    def manual_connect_selected_device(self):
        self.disconnect_active_backend()
        ok = self.connect_selected_device()
        if not ok:
            self.lbl_status.setText("CONNECT FAILED")
            self.lbl_status.setStyleSheet(f"color: {UIColors.STATUS_ERR};")

    def connect_selected_device(self):
        selected_id = self.current_selected_device_id()

        if selected_id == AUTO_DEVICE_ID:
            for backend in self.backends:
                if backend.connect():
                    self.active_backend = backend
                    self.apply_backend_connected_state()
                    return True
            return False

        info = self.device_registry.get(selected_id)
        if info is None:
            return False

        for backend in self.backends:
            if backend.source_key != info.source_key:
                continue
            if backend.connect_to(selected_id):
                self.active_backend = backend
                self.apply_backend_connected_state()
                return True

        return False

    def apply_backend_connected_state(self):
        if self.active_backend is None:
            self.setWindowTitle("POWER MONITOR PRO v2.2")
            self.apply_capabilities(DeviceCapabilities())
            return

        device_name = self.active_backend.current_device_name()
        self.apply_capabilities(self.active_backend.capabilities())
        self.lbl_status.setText(f"ONLINE ({device_name})")
        self.lbl_status.setStyleSheet(f"color: {UIColors.STATUS_OK};")
        self.setWindowTitle(f"POWER MONITOR PRO v2.2 - {device_name}")

    # -----------------------------------------------------
    # control handlers
    # -----------------------------------------------------

    def on_live_csv_toggled(self, checked):
        if self._updating_live_checkbox:
            return

        if checked:
            default_name = time.strftime("power_monitor_live_%Y%m%d_%H%M%S.csv")
            file_path, _ = QFileDialog.getSaveFileName(
                self,
                "Select CSV Log File",
                default_name,
                "CSV Files (*.csv)"
            )

            if file_path:
                self.live_csv_path = file_path
                self.lbl_status.setText("CSV LOG READY")
                self.lbl_status.setStyleSheet(f"color: {UIColors.STATUS_OK};")
            else:
                self._updating_live_checkbox = True
                self.chk_live_log.setChecked(False)
                self._updating_live_checkbox = False
                self.live_csv_path = None
        else:
            self.live_csv_path = None

    def update_control_layout_for_mode(self):
        if self.is_mini:
            self.ctrl_outer.setSpacing(4)
            self.ctrl_row_selectors.hide()
            self.lbl_graph.hide()
            self.cmb_graph.hide()
            self.lbl_smooth.hide()
            self.cmb_smooth.hide()
            self.sep_actions_selectors.hide()
            self.sep_selectors_options.hide()

            self.btn_close.show()
            self.btn_reset.setFixedWidth(70)
            self.btn_log.setFixedWidth(88)
            self.btn_save.setFixedWidth(84)
            self.btn_mode.setFixedWidth(80)
            self.btn_clear_alert.setFixedWidth(98)
            self.btn_diagnostics.setFixedWidth(86)
        else:
            self.ctrl_outer.setSpacing(6)
            self.ctrl_row_selectors.show()
            self.lbl_graph.show()
            self.cmb_graph.show()
            self.lbl_smooth.show()
            self.cmb_smooth.show()
            self.sep_actions_selectors.show()
            self.sep_selectors_options.show()

            self.btn_close.hide()
            self.btn_reset.setFixedWidth(78)
            self.btn_log.setFixedWidth(95)
            self.btn_save.setFixedWidth(90)
            self.btn_mode.setFixedWidth(95)
            self.btn_clear_alert.setFixedWidth(110)
            self.btn_diagnostics.setFixedWidth(95)

            self.cmb_graph.setFixedWidth(110)
            self.cmb_smooth.setFixedWidth(110)

    def set_graph_mode(self, mode):
        self.graph_mode = mode
        self.update_graph_appearance()
        self.save_settings()

    def set_smoothing_mode(self, mode):
        self.smoothing_mode = mode
        self.save_settings()

    def set_sticky_alerts(self, enabled):
        self.sticky_alerts = enabled
        if not enabled:
            self.active_alerts_sticky.clear()
        self.save_settings()

    def clear_sticky_alerts(self):
        self.active_alerts_sticky.clear()
        self.lbl_alert.setText("Alerts: none")
        self.lbl_alert.setStyleSheet(f"color: {UIColors.ALERT_IDLE}; background: transparent;")

    def show_diagnostics_window(self):
        if self.diagnostics_window is None:
            self.diagnostics_window = DiagnosticsWindow(self)
        self.diagnostics_window.show()
        self.diagnostics_window.raise_()
        self.diagnostics_window.activateWindow()

    def set_always_on_top(self, enabled):
        flags = self.windowFlags()

        if enabled:
            flags |= Qt.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowStaysOnTopHint

        self.setWindowFlags(flags)
        self.show()
        self.save_settings()

    def set_mini_graph_enabled(self, enabled):
        self.mini_v.set_graph_enabled(enabled)
        self.mini_c.set_graph_enabled(enabled)
        self.save_settings()

    def set_drag_lock(self, enabled):
        self.drag_locked = enabled
        self.save_settings()

    # -----------------------------------------------------
    # backend handling
    # -----------------------------------------------------

    def disconnect_active_backend(self):
        if self.active_backend is not None:
            try:
                self.active_backend.disconnect()
            except Exception:
                pass
        self.active_backend = None
        self.apply_capabilities(DeviceCapabilities())
        self.setWindowTitle("POWER MONITOR PRO v2.2")

    def apply_capabilities(self, caps: DeviceCapabilities):
        self.active_caps = caps

        self.lbl_temp.setVisible(caps.supports_temp)
        self.lbl_dpdm.setVisible(caps.supports_dpdm)
        self.lbl_protocol.setVisible(caps.supports_protocol)

        if not caps.supports_temp:
            self.lbl_temp.setText("Temp: n/a")
        if not caps.supports_dpdm:
            self.lbl_dpdm.setText("D+ / D-: n/a")
        if not caps.supports_protocol:
            self.lbl_protocol.setText("Protocol: n/a")

    # -----------------------------------------------------
    # mode switching
    # -----------------------------------------------------

    def toggle_mode(self, force_state=False):
        if not force_state:
            self.is_mini = not self.is_mini


        self.base_layout.removeWidget(self.device_ctrl_widget)
        self.base_layout.removeWidget(self.full_container)
        self.base_layout.removeWidget(self.extra_panel)
        self.base_layout.removeWidget(self.mini_container)
        self.base_layout.removeWidget(self.ctrl_widget)
        self.base_layout.removeWidget(self.main_graph)

        if self.is_mini:
            self.btn_mode.setText("FULL")
            self.btn_close.show()
            self.setStyleSheet(f"background-color: {UIColors.BG_MINI};")

            self.full_container.hide()
            self.extra_panel.hide()
            self.main_graph.hide()
            self.mini_container.show()


            self.base_layout.addWidget(self.device_ctrl_widget)
            self.base_layout.addWidget(self.ctrl_widget)
            self.base_layout.addWidget(self.mini_container)

            self.setFixedSize(720, 390)

            flags = Qt.Window | Qt.FramelessWindowHint
            if self.chk_top.isChecked():
                flags |= Qt.WindowStaysOnTopHint
            self.setWindowFlags(flags)
        else:
            self.btn_mode.setText("MINI MODE")
            self.btn_close.hide()
            self.setStyleSheet(f"background-color: {UIColors.BG_MAIN};")

            self.mini_container.hide()
            self.full_container.show()
            self.extra_panel.show()
            self.main_graph.show()


            self.base_layout.addWidget(self.device_ctrl_widget)
            self.base_layout.addWidget(self.full_container)
            self.base_layout.addWidget(self.extra_panel)
            self.base_layout.addWidget(self.ctrl_widget)
            self.base_layout.addWidget(self.main_graph)

            flags = (
                Qt.Window
                | Qt.WindowTitleHint
                | Qt.WindowSystemMenuHint
                | Qt.WindowMinMaxButtonsHint
                | Qt.WindowCloseButtonHint
            )
            if self.chk_top.isChecked():
                flags |= Qt.WindowStaysOnTopHint
            self.setWindowFlags(flags)

            self.setMinimumSize(1200, 980)
            self.setMaximumSize(16777215, 16777215)

        self.update_control_layout_for_mode()
        self.show()
        self.save_settings()

    # -----------------------------------------------------
    # reset / logging
    # -----------------------------------------------------

    def reset_data(self):
        self.stats.reset()
        self.smoothing.reset()

        self.plot_times = collections.deque(maxlen=PLOT_SIZE)
        self.plot_current = collections.deque(maxlen=PLOT_SIZE)
        self.plot_voltage = collections.deque(maxlen=PLOT_SIZE)
        self.plot_power = collections.deque(maxlen=PLOT_SIZE)
        self.plot_start_time = time.time()
        self.plot_line.setData([], [])

        self.mini_v.reset_graph()
        self.mini_c.reset_graph()

        self.block_v.update_value(0.0)
        self.block_a.update_value(0.0)
        self.block_w.update_value(0.0, 2)
        self.block_peak.update_value(0.0)

        self.mini_v.lbl_main.setText("00.000V")
        self.mini_v.lbl_peak.setText("00.000")
        self.mini_v.lbl_avg.setText("00.000")
        self.mini_c.lbl_main.setText("00.000A")
        self.mini_c.lbl_peak.setText("00.000")
        self.mini_c.lbl_avg.setText("00.000")

        self.main_graph.setXRange(0, PLOT_SECONDS, padding=0)
        self.main_graph.setYRange(0.0, 1.0, padding=0)

        self.lbl_temp.setText("Temp: n/a")
        self.lbl_dpdm.setText("D+ / D-: n/a")
        self.lbl_capacity.setText("mAh: 0.00")
        self.lbl_energy.setText("Wh: 0.000")
        self.lbl_protocol.setText("Protocol: n/a")
        self.lbl_rate.setText("Rate: 0.0 Hz   Samples: 0")
        self.lbl_quality.setText("Dropped/disconn: 0   Quality: 100.0%")
        self.samples_received = 0
        self.samples_dropped = 0
        self.current_hz = 0.0
        self.rate_window.clear()
        self.clear_sticky_alerts()

    def auto_save_log(self):
        if len(self.logger.rows_memory) <= 1:
            return None

        try:
            base_dir = Path(__file__).resolve().parent
        except Exception:
            base_dir = Path.cwd()

        log_dir = base_dir / LOG_FOLDER_NAME
        log_dir.mkdir(parents=True, exist_ok=True)

        file_path = log_dir / time.strftime("power_monitor_%Y%m%d_%H%M%S.csv")
        self.logger.save_memory_csv(str(file_path), self.stats.summary_rows())
        return str(file_path)

    def toggle_logging(self):
        if self.playback_active:
            self.lbl_status.setText("STOP PLAYBACK FIRST")
            self.lbl_status.setStyleSheet(f"color: {UIColors.STATUS_WARN};")
            return

        if not self.logger.enabled:
            live_mode = self.chk_live_log.isChecked()
            file_path = self.live_csv_path if live_mode else None

            # One-click CSV logging workflow:
            # START LOG automatically enables CSV logging and asks where to save
            # if the user has not already selected a CSV file.
            if not live_mode or not file_path:
                default_name = time.strftime("power_monitor_live_%Y%m%d_%H%M%S.csv")
                file_path, _ = QFileDialog.getSaveFileName(
                    self,
                    "Select CSV Log File",
                    default_name,
                    "CSV Files (*.csv)"
                )

                if not file_path:
                    self.lbl_status.setText("LOG CANCELLED")
                    self.lbl_status.setStyleSheet(f"color: {UIColors.STATUS_WARN};")
                    return

                self.live_csv_path = file_path
                self._updating_live_checkbox = True
                self.chk_live_log.setChecked(True)
                self._updating_live_checkbox = False
                live_mode = True

            try:
                self.logger.start(live_mode=live_mode, file_path=file_path)
                self.btn_log.setText("STOP LOG")
                self.lbl_status.setText("LOGGING")
                self.lbl_status.setStyleSheet(f"color: {UIColors.STATUS_OK};")
            except Exception as e:
                self.lbl_status.setText("LOG START FAILED")
                self.lbl_status.setStyleSheet(f"color: {UIColors.STATUS_ERR};")
                print("Log start error:", e)
        else:
            was_live = self.logger.live_mode
            live_path = self.logger.file_path
            self.logger.stop()
            self.btn_log.setText("START LOG")

            if was_live and live_path:
                self.lbl_status.setText(f"LOG SAVED: {live_path}")
                self.lbl_status.setStyleSheet(f"color: {UIColors.STATUS_OK};")
                return

            try:
                saved_path = self.auto_save_log()
                if saved_path:
                    self.lbl_status.setText(f"LOG SAVED: {saved_path}")
                    self.lbl_status.setStyleSheet(f"color: {UIColors.STATUS_OK};")
                else:
                    self.lbl_status.setText("NO LOG DATA")
                    self.lbl_status.setStyleSheet(f"color: {UIColors.STATUS_WARN};")
            except Exception as e:
                self.lbl_status.setText("AUTO SAVE FAILED")
                self.lbl_status.setStyleSheet(f"color: {UIColors.STATUS_ERR};")
                print("Auto save error:", e)

    def load_csv_dialog(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Load CSV Playback",
            "",
            "CSV Files (*.csv)"
        )
        if not file_path:
            return

        try:
            self.load_playback_csv(file_path)
            self.start_playback(file_path)
        except Exception as e:
            self.lbl_status.setText("LOAD CSV FAILED")
            self.lbl_status.setStyleSheet(f"color: {UIColors.STATUS_ERR};")
            print("Load CSV error:", e)

    def load_playback_csv(self, file_path: str):
        rows = []
        with open(file_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if not row or row.get("timestamp") == "SESSION SUMMARY":
                    break

                try:
                    elapsed_s = float(row.get("elapsed_s", "0") or 0.0)
                    voltage_v = float(row.get("voltage_v_raw", "0") or 0.0)
                    current_a = float(row.get("current_a_raw", "0") or 0.0)
                    power_w = float(row.get("power_w_raw", "0") or 0.0)
                    temp_c = float(row.get("temp_c_raw", "0") or 0.0)
                    dp_v = float(row.get("dp_v_raw", "0") or 0.0)
                    dn_v = float(row.get("dn_v_raw", "0") or 0.0)
                except Exception:
                    continue

                rows.append({
                    "elapsed_s": elapsed_s,
                    "sample": Sample(
                        voltage_v=voltage_v,
                        current_a=current_a,
                        power_w=power_w,
                        dp_v=dp_v,
                        dn_v=dn_v,
                        temp_c=temp_c,
                        timestamp=time.time(),
                    ),
                    "device": row.get("device", "CSV Playback"),
                    "source_family": row.get("source_family", "Playback"),
                    "protocol": row.get("protocol", "-"),
                    "alerts": row.get("alerts", "none"),
                })

        if not rows:
            raise ValueError("No valid playback rows found")

        rows.sort(key=lambda r: r["elapsed_s"])
        self.playback_rows = rows

    def start_playback(self, file_path: str):
        if self.logger.enabled:
            self.toggle_logging()

        self.disconnect_active_backend()
        self.reset_data()

        self.playback_active = True
        self.playback_paused = False
        self.playback_index = 0
        self.playback_elapsed = self.playback_rows[0]["elapsed_s"] if self.playback_rows else 0.0
        self.playback_speed = 1.0
        self.playback_last_tick = time.time()
        self.playback_slider_dragging = False

        self.apply_capabilities(DeviceCapabilities(True, True, True))
        self.lbl_status.setText("PLAYBACK")
        self.lbl_status.setStyleSheet(f"color: {UIColors.STATUS_OK};")
        self.setWindowTitle("POWER MONITOR PRO v2.2 - CSV Playback")

        if self.playback_window is None:
            self.playback_window = PlaybackControlsWindow(self)
        self.playback_window.set_loaded_file(file_path)
        self.playback_window.set_range(len(self.playback_rows) - 1)
        self.playback_window.set_paused(False)
        self.playback_window.show()
        self.playback_window.raise_()
        self.playback_window.activateWindow()

    def stop_playback(self):
        self.playback_active = False
        self.playback_paused = False
        self.playback_index = 0
        self.playback_elapsed = 0.0

        if self.playback_window is not None:
            try:
                self.playback_window.hide()
            except Exception:
                pass

        self.lbl_status.setText("PLAYBACK STOPPED")
        self.lbl_status.setStyleSheet(f"color: {UIColors.STATUS_WARN};")
        self.setWindowTitle("POWER MONITOR PRO v2.2")
        self.apply_capabilities(DeviceCapabilities())

    def toggle_playback_pause(self):
        if not self.playback_active:
            return
        self.playback_paused = not self.playback_paused
        self.playback_last_tick = time.time()
        if self.playback_window:
            self.playback_window.set_paused(self.playback_paused)

    def restart_playback(self):
        if not self.playback_rows:
            return
        self.reset_data()
        self.playback_index = 0
        self.playback_elapsed = self.playback_rows[0]["elapsed_s"]
        self.playback_paused = False
        self.playback_last_tick = time.time()
        if self.playback_window:
            self.playback_window.set_paused(False)

    def set_playback_speed(self, text: str):
        try:
            self.playback_speed = float(text.replace("x", ""))
        except Exception:
            self.playback_speed = 1.0
        self.playback_last_tick = time.time()

    def playback_slider_pressed(self):
        self.playback_slider_dragging = True

    def playback_slider_released(self):
        self.playback_slider_dragging = False
        self.seek_playback_to_index(self.playback_window.slider.value() if self.playback_window else self.playback_index)

    def playback_slider_changed(self, value: int):
        if self.playback_slider_dragging:
            self.seek_playback_to_index(value, update_display=True)

    def seek_playback_to_index(self, index: int, update_display: bool = True):
        if not self.playback_rows:
            return

        index = max(0, min(index, len(self.playback_rows) - 1))
        self.playback_index = index
        self.playback_elapsed = self.playback_rows[index]["elapsed_s"]
        self.playback_last_tick = time.time()

        if update_display:
            row = self.playback_rows[index]
            sample = row["sample"]
            sample.timestamp = time.time()
            self.process_playback_sample(row)

    def update_playback(self):
        if not self.playback_active or not self.playback_rows:
            return

        now = time.time()
        dt = now - self.playback_last_tick
        self.playback_last_tick = now

        if not self.playback_paused and not self.playback_slider_dragging:
            self.playback_elapsed += dt * self.playback_speed

            while (
                self.playback_index + 1 < len(self.playback_rows)
                and self.playback_rows[self.playback_index + 1]["elapsed_s"] <= self.playback_elapsed
            ):
                self.playback_index += 1

            if self.playback_index >= len(self.playback_rows) - 1:
                self.playback_index = len(self.playback_rows) - 1
                self.playback_elapsed = self.playback_rows[-1]["elapsed_s"]
                self.playback_paused = True
                if self.playback_window:
                    self.playback_window.set_paused(True)

            self.process_playback_sample(self.playback_rows[self.playback_index])

        if self.playback_window:
            total_s = self.playback_rows[-1]["elapsed_s"]
            self.playback_window.set_position(self.playback_index, self.playback_elapsed, total_s)

    def process_playback_sample(self, row):
        sample = row["sample"]
        sample.timestamp = time.time()

        self.stats.update(sample)
        display_sample = self.smoothing.apply(sample, self.smoothing_mode)

        protocol = row.get("protocol") or guess_protocol(sample.dp_v, sample.dn_v, sample.voltage_v)
        self.update_info_panel(sample, protocol)
        self.update_full_display(display_sample)
        self.update_mini_display(display_sample)
        self.update_graph(display_sample)

        alerts_text = row.get("alerts", "none")
        if alerts_text and alerts_text != "none":
            self.lbl_alert.setText("Alerts: " + alerts_text)
            self.lbl_alert.setStyleSheet(f"color: {UIColors.ALERT_ACTIVE}; background: transparent;")
        else:
            self.lbl_alert.setText("Alerts: none")
            self.lbl_alert.setStyleSheet(f"color: {UIColors.ALERT_IDLE}; background: transparent;")

    # -----------------------------------------------------
    # update loop
    # -----------------------------------------------------

    @Slot()
    def update_loop(self):
        if self.playback_active:
            self.update_playback()
            return

        if not self.ensure_connection():
            return

        raw_sample = self.read_valid_sample()
        if raw_sample is None:
            # A nonblocking serial/HID read can legitimately be empty between real samples.
            # Do not count every empty UI tick as a dropped packet; that makes slower devices
            # like RP2040 look far worse than they are. Backend fail counters still track
            # real connection/read problems in the diagnostics window.
            if self.active_backend is None or not self.active_backend.is_connected():
                self.samples_dropped += 1
            self.update_sample_rate()
            return

        self.process_sample(raw_sample)
        self.update_sample_rate()

    def ensure_connection(self):
        if self.active_backend is not None and self.active_backend.is_connected():
            return True

        now = time.time()

        if now - self.last_reconnect_attempt < RECONNECT_INTERVAL_S:
            return False

        self.last_reconnect_attempt = now

        # Preserve selected device
        selected_id = self.current_selected_device_id()

        # Refresh dropdown so unplug/replug is detected
        try:
            self.scan_available_devices()
            idx = self.cmb_device.findData(selected_id)
            if idx >= 0:
                self.cmb_device.setCurrentIndex(idx)
        except Exception:
            pass

        if self.connect_selected_device():
            return True

        self.lbl_status.setText("SEARCHING")
        self.lbl_status.setStyleSheet(f"color: {UIColors.STATUS_WARN};")
        self.setWindowTitle("POWER MONITOR PRO v2.2")
        return False


    def read_valid_sample(self):
        if self.active_backend is None:
            return None

        try:
            sample = self.active_backend.read_sample()
        except Exception:
            sample = None

        # No sample does not always mean disconnected.
        # ATORCH especially may briefly return no data.
        if sample is None:
            if self.active_backend is not None and not self.active_backend.is_connected():
                self.active_backend = None
                self.apply_capabilities(DeviceCapabilities())
                self.lbl_status.setText("DISCONNECTED")
                self.lbl_status.setStyleSheet(f"color: {UIColors.STATUS_WARN};")
                self.setWindowTitle("POWER MONITOR PRO v2.2")
            return None

        if not (0.0 <= sample.voltage_v <= 100.0 and 0.0 <= sample.current_a <= 30.0):
            return None

        device_name = self.active_backend.current_device_name()
        self.lbl_status.setText("ONLINE")
        self.lbl_status.setStyleSheet(f"color: {UIColors.STATUS_OK};")
        self.setWindowTitle(f"POWER MONITOR PRO v2.2 - {device_name}")

        return sample


    def process_sample(self, raw_sample: Sample):
        self.samples_received += 1
        self.rate_window.append(time.time())

        self.stats.update(raw_sample)
        display_sample = self.smoothing.apply(raw_sample, self.smoothing_mode)

        protocol = self.active_backend.protocol_text(raw_sample) if self.active_backend else "-"
        alerts_text = self.evaluate_alerts(raw_sample)

        self.update_info_panel(raw_sample, protocol)
        self.update_full_display(display_sample)
        self.update_mini_display(display_sample)
        self.update_graph(display_sample)
        self.append_log(raw_sample, protocol, alerts_text)

    def update_sample_rate(self):
        now = time.time()

        while self.rate_window and self.rate_window[0] < now - 1.0:
            self.rate_window.popleft()

        self.current_hz = float(len(self.rate_window))

        total_attempts = self.samples_received + self.samples_dropped
        if total_attempts > 0:
            quality = (self.samples_received / total_attempts) * 100.0
        else:
            quality = 100.0

        if hasattr(self, "lbl_rate"):
            self.lbl_rate.setText(
                f"Rate: {self.current_hz:.1f} Hz   Samples: {self.samples_received}"
            )
        if hasattr(self, "lbl_quality"):
            self.lbl_quality.setText(
                f"Dropped/disconn: {self.samples_dropped}   Quality: {quality:.1f}%"
            )
        if getattr(self, "diagnostics_window", None) is not None and self.diagnostics_window.isVisible():
            self.diagnostics_window.update_info()

    # -----------------------------------------------------
    # UI updates
    # -----------------------------------------------------

    def update_info_panel(self, raw_sample: Sample, protocol: str):
        if self.active_caps.supports_temp:
            self.lbl_temp.setText(f"Temp: {raw_sample.temp_c:.1f} °C")
        if self.active_caps.supports_dpdm:
            self.lbl_dpdm.setText(f"D+ / D-: {raw_sample.dp_v:.3f} V / {raw_sample.dn_v:.3f} V")
        if self.active_caps.supports_protocol:
            self.lbl_protocol.setText(f"Protocol: {protocol}")

        self.lbl_capacity.setText(f"mAh: {self.stats.mah():.2f}")
        self.lbl_energy.setText(f"Wh: {self.stats.wh():.3f}")

    def update_full_display(self, display_sample: Sample):
        if self.is_mini:
            return

        self.block_v.update_value(display_sample.voltage_v)
        self.block_a.update_value(display_sample.current_a)
        self.block_w.update_value(display_sample.power_w, 2)

        if self.graph_mode == "Current":
            self.block_peak.set_title("Peak Current")
            self.block_peak.update_value(self.stats.max_c, 3)
        elif self.graph_mode == "Voltage":
            self.block_peak.set_title("Peak Voltage")
            self.block_peak.update_value(self.stats.max_v, 3)
        else:
            self.block_peak.set_title("Peak Power")
            self.block_peak.update_value(self.stats.max_p, 2)

    def update_mini_display(self, display_sample: Sample):
        if not self.is_mini:
            return

        self.mini_v.update_row(
            display_sample.voltage_v,
            self.stats.recent_peak_v(),
            self.stats.avg_v(),
            "V"
        )
        self.mini_c.update_row(
            display_sample.current_a,
            self.stats.recent_peak_c(),
            self.stats.avg_c(),
            "A"
        )

    def update_graph(self, display_sample: Sample):
        elapsed = display_sample.timestamp - self.plot_start_time

        self.plot_times.append(elapsed)
        self.plot_current.append(display_sample.current_a)
        self.plot_voltage.append(display_sample.voltage_v)
        self.plot_power.append(display_sample.power_w)

        # Data is appended here; graph panning is handled by refresh_graph_scroll()
        # at a faster render rate so traces float smoothly instead of stepping.
        self.refresh_graph_scroll()

    def refresh_graph_scroll(self):
        """Smoothly pan full and mini graphs between device samples."""
        now_elapsed = time.time() - self.plot_start_time

        # Mini graphs need their x-values recalculated from current time.
        try:
            self.mini_v.refresh_graph_scroll()
            self.mini_c.refresh_graph_scroll()
        except Exception:
            pass

        if self.is_mini or not self.plot_times:
            return

        x_vals = list(self.plot_times)

        if self.graph_mode == "Current":
            y_vals = list(self.plot_current)
        elif self.graph_mode == "Voltage":
            y_vals = list(self.plot_voltage)
        else:
            y_vals = list(self.plot_power)

        self.plot_line.setData(x_vals, y_vals)

        if now_elapsed <= PLOT_SECONDS:
            self.main_graph.setXRange(0, PLOT_SECONDS, padding=0)
        else:
            self.main_graph.setXRange(now_elapsed - PLOT_SECONDS, now_elapsed, padding=0)

        self.update_autoscale_graph(y_vals)

    def update_graph_appearance(self):
        if self.graph_mode == "Current":
            self.plot_line.setPen(pg.mkPen(color=UIColors.CURRENT, width=2))
            self.main_graph.setLabel("left", "Current", units="A")
        elif self.graph_mode == "Voltage":
            self.plot_line.setPen(pg.mkPen(color=UIColors.VOLTAGE, width=2))
            self.main_graph.setLabel("left", "Voltage", units="V")
        else:
            self.plot_line.setPen(pg.mkPen(color=UIColors.POWER, width=2))
            self.main_graph.setLabel("left", "Power", units="W")

    def update_autoscale_graph(self, y_vals):
        if not y_vals:
            return

        y_min = min(y_vals)
        y_max = max(y_vals)
        y_range = y_max - y_min

        if y_range < AUTO_SCALE_MIN_RANGE:
            mid = (y_min + y_max) / 2.0
            y_min = mid - (AUTO_SCALE_MIN_RANGE / 2.0)
            y_max = mid + (AUTO_SCALE_MIN_RANGE / 2.0)
        else:
            pad = y_range * AUTO_SCALE_MARGIN
            y_min -= pad
            y_max += pad

        y_min = clamp_min_zero(y_min)
        self.main_graph.setYRange(y_min, y_max, padding=0)

    # -----------------------------------------------------
    # alerts
    # -----------------------------------------------------

    def evaluate_alerts(self, raw_sample: Sample):
        alerts = []

        if abs(raw_sample.current_a - self.stats.prev_current) >= ALERT_CURRENT_SPIKE_A:
            alerts.append("Current spike")
        if raw_sample.voltage_v > ALERT_VOLTAGE_MAX:
            alerts.append("High voltage")
        if raw_sample.current_a > ALERT_CURRENT_MAX:
            alerts.append("High current")
        if raw_sample.power_w > ALERT_POWER_MAX:
            alerts.append("High power")
        if self.active_caps.supports_temp and raw_sample.temp_c > ALERT_TEMP_MAX:
            alerts.append("High temperature")

        self.stats.prev_current = raw_sample.current_a

        if self.sticky_alerts:
            self.active_alerts_sticky.update(alerts)
            shown = sorted(self.active_alerts_sticky)
        else:
            shown = alerts

        if shown:
            text = ", ".join(shown)
            self.lbl_alert.setText("Alerts: " + text)
            self.lbl_alert.setStyleSheet(f"color: {UIColors.ALERT_ACTIVE}; background: transparent;")
            return text

        self.lbl_alert.setText("Alerts: none")
        self.lbl_alert.setStyleSheet(f"color: {UIColors.ALERT_IDLE}; background: transparent;")
        return "none"

    # -----------------------------------------------------
    # logging append
    # -----------------------------------------------------

    def append_log(self, raw_sample: Sample, protocol: str, alerts_text: str):
        if self.active_backend is None:
            return

        row = [
            time.strftime("%Y-%m-%dT%H:%M:%S"),
            f"{self.stats.elapsed_s():.3f}",
            self.active_backend.current_device_name(),
            self.active_backend.source_label,
            f"{raw_sample.voltage_v:.5f}",
            f"{raw_sample.current_a:.5f}",
            f"{raw_sample.power_w:.5f}",
            f"{raw_sample.temp_c:.1f}",
            f"{raw_sample.dp_v:.3f}",
            f"{raw_sample.dn_v:.3f}",
            f"{self.stats.mah():.3f}",
            f"{self.stats.wh():.5f}",
            protocol,
            alerts_text
        ]
        self.logger.append(row)

    # -----------------------------------------------------
    # window events
    # -----------------------------------------------------

    def closeEvent(self, event):
        self.save_settings()
        self.stop_playback()
        if self.diagnostics_window is not None:
            self.diagnostics_window.hide()
        self.disconnect_active_backend()
        self.logger.stop()
        super().closeEvent(event)

    def mousePressEvent(self, e):
        if self.is_mini and not self.drag_locked:
            self.oldPos = e.globalPosition().toPoint()
        else:
            self.oldPos = None
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self.is_mini and not self.drag_locked and self.oldPos is not None:
            current_pos = e.globalPosition().toPoint()
            delta = current_pos - self.oldPos
            self.move(self.x() + delta.x(), self.y() + delta.y())
            self.oldPos = current_pos
            self.save_settings()
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self.oldPos = None
        super().mouseReleaseEvent(e)


