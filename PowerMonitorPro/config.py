from pathlib import Path

# =========================================================
# CONFIG
# =========================================================

APP_ORG = "JazFix"
APP_NAME = "Power Monitor Pro"

PLOT_SECONDS = 30
UI_INTERVAL_MS = 50
RENDER_INTERVAL_MS = 16  # smooth graph pan/render timer (~60 FPS)
CURRENT_SAMPLE_RATE_HZ = max(1, int(1000 / UI_INTERVAL_MS))
PLOT_SIZE = PLOT_SECONDS * CURRENT_SAMPLE_RATE_HZ

MINI_SECONDS = 20
MINI_PLOT_SIZE = MINI_SECONDS * CURRENT_SAMPLE_RATE_HZ

READ_LEN = 64
REPORT_ID = 0x00
SAMPLE_SIZE = 15
NUM_SAMPLES = 4

ALERT_CURRENT_SPIKE_A = 0.30
ALERT_VOLTAGE_MAX = 25.0
ALERT_CURRENT_MAX = 6.0
ALERT_POWER_MAX = 100.0
ALERT_TEMP_MAX = 75.0

AUTO_SCALE_MIN_RANGE = 0.10
AUTO_SCALE_MARGIN = 0.10

ENABLE_KEEPALIVE = True
KEEPALIVE_INTERVAL_S = 1.0

DISPLAY_AVG_SECONDS = 8.0
DISPLAY_PEAK_SECONDS = 8.0

LIVE_LOG_FLUSH_EVERY = 10

RP2040_BAUD = 115200
RP2040_VID = 0x2E8A

ESP32_BAUD = 115200
# Common USB-serial bridge VID/PIDs used by ESP32 dev boards: CP210x, CH340, FTDI, native Espressif USB.
ESP32_VIDS = (0x10C4, 0x1A86, 0x0403, 0x303A)

AUTO_DEVICE_ID = "__auto__"

RECONNECT_INTERVAL_S = 2.0

LOG_FOLDER_NAME = "logs"
PLAYBACK_SPEEDS = ["0.25x", "0.5x", "1x", "2x", "5x", "10x"]


# =========================================================
# STYLE / COLORS
# =========================================================

class UIColors:
    BG_MAIN = "#121212"
    BG_MINI = "#000000"

    VOLTAGE = "#00FF7F"
    CURRENT = "#00BFFF"
    POWER = "#FFD700"
    PEAK = "#FF4500"

    STATUS_OK = "#00FF7F"
    STATUS_WARN = "#FFA500"
    STATUS_ERR = "#FF4500"
    ALERT_IDLE = "#FFD700"
    ALERT_ACTIVE = "#FF4500"


