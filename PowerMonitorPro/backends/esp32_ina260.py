import time
import serial
import serial.tools.list_ports

from config import ESP32_BAUD, ESP32_VIDS, RP2040_VID
from models import Sample, DeviceCapabilities, DeviceInfo
from backends.base import MonitorBackendBase


class ESP32INA260Backend(MonitorBackendBase):
    """ESP32 INA260 serial backend.

    Native firmware format kept for compatibility with Board Revs / Lab Meter App:
        Voltage:9.256
        Current:1.458

    Power Monitor Pro calculates power internally as V * A.
    """

    source_key = "esp32"
    source_label = "ESP32 INA260"

    def __init__(self, baud=ESP32_BAUD, vids=ESP32_VIDS):
        self.baud = baud
        self.vids = tuple(vids)
        self.ser = None
        self.port_name = None
        self.port_desc = ""
        self._buffer = b""
        self.fail_count = 0
        self.last_line = ""
        self.lines_seen = 0
        self.lines_rejected = 0
        self.latest_voltage = None
        self.latest_current = None
        self.last_voltage_time = 0.0
        self.last_current_time = 0.0
        self.samples_returned = 0

    def capabilities(self):
        return DeviceCapabilities(
            supports_temp=False,
            supports_dpdm=False,
            supports_protocol=False,
        )

    def protocol_text(self, sample: Sample) -> str:
        return "ESP32 INA260"

    def is_connected(self):
        return self.ser is not None and self.ser.is_open

    def current_device_name(self):
        if self.port_name:
            return f"ESP32 INA260 ({self.port_name})"
        return "ESP32 INA260"

    def disconnect(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.port_name = None
        self.port_desc = ""
        self._buffer = b""
        self.fail_count = 0
        self.last_line = ""
        self.latest_voltage = None
        self.latest_current = None
        self.last_voltage_time = 0.0
        self.last_current_time = 0.0

    def debug_info(self):
        return {
            "port_name": self.port_name,
            "port_desc": self.port_desc,
            "baud": self.baud,
            "vid_filters": ", ".join(f"0x{v:04X}" for v in self.vids),
            "fail_count": self.fail_count,
            "buffer_len": len(self._buffer),
            "lines_seen": self.lines_seen,
            "lines_rejected": self.lines_rejected,
            "latest_voltage": self.latest_voltage,
            "latest_current": self.latest_current,
            "samples_returned": self.samples_returned,
            "last_line": self.last_line,
        }

    def _candidate_ports(self):
        ports = []
        try:
            for p in serial.tools.list_ports.comports():
                # Avoid taking the RP2040 port. ESP32 WROOM dev boards usually use
                # CP210x/CH340/FTDI USB-serial bridges, or Espressif native USB.
                if p.vid is None:
                    continue
                if p.vid == RP2040_VID:
                    continue
                if p.vid in self.vids:
                    ports.append(p)
        except Exception:
            pass
        return ports

    def list_devices(self) -> list[DeviceInfo]:
        result = []
        for port in self._candidate_ports():
            desc = port.description or "Serial"
            result.append(
                DeviceInfo(
                    device_id=f"esp32::{port.device}",
                    label=f"ESP32 INA260 - {port.device} ({desc})",
                    source_key=self.source_key,
                )
            )
        return result

    def connect(self):
        if self.is_connected():
            return True

        for port in self._candidate_ports():
            if self._open_port(port.device, getattr(port, "description", "")):
                # Probe briefly so Auto mode does not claim random USB serial devices.
                deadline = time.time() + 1.2
                while time.time() < deadline:
                    sample = self.read_sample()
                    if sample is not None:
                        return True
                    time.sleep(0.02)
                self.disconnect()

        return False

    def connect_to(self, device_id: str):
        if self.is_connected():
            self.disconnect()

        prefix = "esp32::"
        if not device_id.startswith(prefix):
            return False

        wanted_port = device_id[len(prefix):]
        desc = ""
        for p in self._candidate_ports():
            if p.device == wanted_port:
                desc = p.description or ""
                break

        return self._open_port(wanted_port, desc)

    def _open_port(self, port_name: str, desc: str = ""):
        try:
            ser = serial.Serial(port_name, self.baud, timeout=0)
            try:
                ser.reset_input_buffer()
            except Exception:
                pass
            self.ser = ser
            self.port_name = port_name
            self.port_desc = desc
            self._buffer = b""
            self.fail_count = 0
            self.last_line = ""
            self.latest_voltage = None
            self.latest_current = None
            self.last_voltage_time = 0.0
            self.last_current_time = 0.0
            return True
        except Exception:
            self.disconnect()
            return False

    def _parse_line(self, text: str):
        lower = text.lower()

        try:
            if lower.startswith("voltage:"):
                value = text.split(":", 1)[1].strip()
                self.latest_voltage = float(value)
                self.last_voltage_time = time.time()
                return None

            if lower.startswith("current:"):
                value = text.split(":", 1)[1].strip()
                self.latest_current = float(value)
                self.last_current_time = time.time()
                return None
        except Exception:
            self.lines_rejected += 1
            return None

        # Ignore normal boot/status lines, but count unknown measurement-looking lines.
        if text and (":" in text or "=" in text):
            self.lines_rejected += 1
        return None

    def read_sample(self):
        if not self.is_connected():
            return None

        try:
            waiting = self.ser.in_waiting

            if waiting <= 0:
                self.fail_count += 1
                if self.fail_count < 120:
                    return None
                self.disconnect()
                return None

            self._buffer += self.ser.read(waiting)

            if b"\n" not in self._buffer:
                self.fail_count += 1
                if self.fail_count < 120:
                    return None
                self.disconnect()
                return None

            lines = self._buffer.split(b"\n")
            self._buffer = lines.pop()

            for raw in lines:
                try:
                    text = raw.decode(errors="ignore").strip()
                except Exception:
                    continue

                self.last_line = text
                if text:
                    self.lines_seen += 1
                    self._parse_line(text)

            # Need both values from the same recent stream. ESP32 sends Voltage and
            # Current on separate lines; power is calculated here.
            now = time.time()
            if (
                self.latest_voltage is not None and
                self.latest_current is not None and
                (now - self.last_voltage_time) < 2.0 and
                (now - self.last_current_time) < 2.0
            ):
                v = float(self.latest_voltage)
                c = float(self.latest_current)
                p = v * c

                if not (0.0 <= v <= 100.0 and 0.0 <= c <= 30.0 and 0.0 <= p <= 3000.0):
                    self.lines_rejected += 1
                    return None

                self.fail_count = 0
                self.samples_returned += 1
                return Sample(
                    voltage_v=v,
                    current_a=c,
                    power_w=p,
                    dp_v=0.0,
                    dn_v=0.0,
                    temp_c=0.0,
                    timestamp=time.time(),
                )

            self.fail_count += 1
            return None

        except Exception:
            self.disconnect()
            return None
