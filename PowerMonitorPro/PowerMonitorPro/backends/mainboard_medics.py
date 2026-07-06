import time
import serial
import serial.tools.list_ports

from config import RP2040_BAUD, RP2040_VID
from models import Sample, DeviceCapabilities, DeviceInfo
from backends.base import MonitorBackendBase


class MainboardMedicsBackend(MonitorBackendBase):
    """Mainboard Medics RP2040/INA260 meter backend.

    Native firmware output is German text blocks, for example:
        ---- Messwerte ----
        Adresse: 0x0
        Spannung: 5.123 V
        Strom: 0.456 A
        Leistung: 2.337 W
        -------------------
    """

    source_key = "mainboard_medics"
    source_label = "Mainboard Medics Meter"

    def __init__(self, baud=RP2040_BAUD, vid=RP2040_VID):
        self.baud = baud
        self.vid = vid
        self.ser = None
        self.port_name = None
        self._buffer = b""
        self.fail_count = 0
        self.last_line = ""
        self.lines_seen = 0
        self.lines_rejected = 0
        self.latest_voltage = None
        self.latest_current = None
        self.latest_power = None
        self.last_voltage_time = 0.0
        self.last_current_time = 0.0
        self.last_power_time = 0.0
        self.address = ""
        self.samples_returned = 0

    def capabilities(self):
        return DeviceCapabilities(
            supports_temp=False,
            supports_dpdm=False,
            supports_protocol=False,
        )

    def protocol_text(self, sample: Sample) -> str:
        return "Mainboard Medics"

    def is_connected(self):
        return self.ser is not None and self.ser.is_open

    def current_device_name(self):
        if self.port_name:
            return f"Mainboard Medics Meter ({self.port_name})"
        return "Mainboard Medics Meter"

    def disconnect(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.port_name = None
        self._buffer = b""
        self.fail_count = 0
        self.last_line = ""
        self.latest_voltage = None
        self.latest_current = None
        self.latest_power = None
        self.last_voltage_time = 0.0
        self.last_current_time = 0.0
        self.last_power_time = 0.0
        self.address = ""

    def debug_info(self):
        return {
            "port_name": self.port_name,
            "baud": self.baud,
            "vid_filter": f"0x{self.vid:04X}",
            "fail_count": self.fail_count,
            "buffer_len": len(self._buffer),
            "lines_seen": self.lines_seen,
            "lines_rejected": self.lines_rejected,
            "address": self.address,
            "latest_voltage": self.latest_voltage,
            "latest_current": self.latest_current,
            "latest_power": self.latest_power,
            "samples_returned": self.samples_returned,
            "last_line": self.last_line,
        }

    def _candidate_ports(self):
        ports = []
        try:
            for p in serial.tools.list_ports.comports():
                if p.vid == self.vid:
                    ports.append(p)
        except Exception:
            pass
        return ports

    def list_devices(self) -> list[DeviceInfo]:
        result = []
        for port in self._candidate_ports():
            result.append(
                DeviceInfo(
                    device_id=f"mainboard_medics::{port.device}",
                    label=f"Mainboard Medics Meter - {port.device}",
                    source_key=self.source_key,
                )
            )
        return result

    def connect(self):
        if self.is_connected():
            return True

        for port in self._candidate_ports():
            if self._open_port(port.device):
                # Probe briefly so Auto mode only claims this port when it sees
                # the German Mainboard Medics output.
                deadline = time.time() + 1.5
                saw_medics_format = False
                while time.time() < deadline:
                    self.read_sample()
                    if (
                        "Messwerte" in self.last_line or
                        "Spannung" in self.last_line or
                        "Strom" in self.last_line or
                        "Leistung" in self.last_line
                    ):
                        saw_medics_format = True
                        break
                    time.sleep(0.02)
                if saw_medics_format:
                    return True
                self.disconnect()

        return False

    def connect_to(self, device_id: str):
        if self.is_connected():
            self.disconnect()

        prefix = "mainboard_medics::"
        if not device_id.startswith(prefix):
            return False

        wanted_port = device_id[len(prefix):]
        return self._open_port(wanted_port)

    def _open_port(self, port_name: str):
        try:
            ser = serial.Serial(port_name, self.baud, timeout=0)
            try:
                ser.reset_input_buffer()
            except Exception:
                pass
            self.ser = ser
            self.port_name = port_name
            self._buffer = b""
            self.fail_count = 0
            self.last_line = ""
            self.latest_voltage = None
            self.latest_current = None
            self.latest_power = None
            self.last_voltage_time = 0.0
            self.last_current_time = 0.0
            self.last_power_time = 0.0
            self.address = ""
            return True
        except Exception:
            self.disconnect()
            return False

    def _extract_float(self, text: str):
        # Accept formats like "Spannung: 5.123 V".
        try:
            value = text.split(":", 1)[1].strip()
            value = value.replace("V", "").replace("A", "").replace("W", "").strip()
            value = value.replace(",", ".")
            return float(value.split()[0])
        except Exception:
            self.lines_rejected += 1
            return None

    def _parse_line(self, text: str):
        lower = text.lower()
        now = time.time()

        if lower.startswith("adresse:"):
            self.address = text.split(":", 1)[1].strip()
            return

        if lower.startswith("spannung:"):
            value = self._extract_float(text)
            if value is not None:
                self.latest_voltage = value
                self.last_voltage_time = now
            return

        if lower.startswith("strom:"):
            value = self._extract_float(text)
            if value is not None:
                self.latest_current = value
                self.last_current_time = now
            return

        if lower.startswith("leistung:"):
            value = self._extract_float(text)
            if value is not None:
                self.latest_power = value
                self.last_power_time = now
            return

        # Ignore separators/header lines.
        if text and not ("messwerte" in lower or set(text) <= set("- ")):
            # Count unexpected labelled lines only.
            if ":" in text or "=" in text:
                self.lines_rejected += 1

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

            now = time.time()
            if (
                self.latest_voltage is not None and
                self.latest_current is not None and
                self.latest_power is not None and
                (now - self.last_voltage_time) < 2.0 and
                (now - self.last_current_time) < 2.0 and
                (now - self.last_power_time) < 2.0
            ):
                v = float(self.latest_voltage)
                c = float(self.latest_current)
                p = float(self.latest_power)

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
