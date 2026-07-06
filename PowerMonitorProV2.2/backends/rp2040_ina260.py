import time
import serial
import serial.tools.list_ports

from config import RP2040_BAUD, RP2040_VID
from models import Sample, DeviceCapabilities, DeviceInfo
from backends.base import MonitorBackendBase

# =========================================================
# RP2040 INA260 BACKEND
# =========================================================

class RP2040INA260Backend(MonitorBackendBase):
    source_key = "rp2040"
    source_label = "RP2040 INA260"

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

    def capabilities(self):
        return DeviceCapabilities(
            supports_temp=False,
            supports_dpdm=False,
            supports_protocol=False,
        )

    def protocol_text(self, sample: Sample) -> str:
        return "INA260 Monitor"

    def is_connected(self):
        return self.ser is not None and self.ser.is_open

    def current_device_name(self):
        if self.port_name:
            return f"RP2040 INA260 ({self.port_name})"
        return "RP2040 INA260"

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

    def debug_info(self):
        return {
            "port_name": self.port_name,
            "baud": self.baud,
            "vid_filter": f"0x{self.vid:04X}",
            "fail_count": self.fail_count,
            "buffer_len": len(self._buffer),
            "lines_seen": self.lines_seen,
            "lines_rejected": self.lines_rejected,
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
                    device_id=f"rp2040::{port.device}",
                    label=f"RP2040 INA260 - {port.device}",
                    source_key=self.source_key,
                )
            )
        return result

    def connect(self):
        if self.is_connected():
            return True

        for port in self._candidate_ports():
            try:
                ser = serial.Serial(port.device, self.baud, timeout=0)
                self.ser = ser
                self.port_name = port.device
                self._buffer = b""
                self.fail_count = 0
                return True
            except Exception:
                continue

        return False

    def connect_to(self, device_id: str):
        if self.is_connected():
            self.disconnect()

        prefix = "rp2040::"
        if not device_id.startswith(prefix):
            return False

        wanted_port = device_id[len(prefix):]

        try:
            ser = serial.Serial(wanted_port, self.baud, timeout=0)
            self.ser = ser
            self.port_name = wanted_port
            self._buffer = b""
            self.fail_count = 0
            return True
        except Exception:
            return False

    def read_sample(self):
        if not self.is_connected():
            return None

        try:
            waiting = self.ser.in_waiting

            if waiting <= 0:
                self.fail_count += 1

                # tolerate brief serial gaps
                if self.fail_count < 80:
                    return None

                self.disconnect()
                return None

            self._buffer += self.ser.read(waiting)

            if b"\n" not in self._buffer:
                self.fail_count += 1

                # tolerate partial lines
                if self.fail_count < 80:
                    return None

                self.disconnect()
                return None

            lines = self._buffer.split(b"\n")
            self._buffer = lines.pop()

            latest_valid = None

            for raw in reversed(lines):
                try:
                    text = raw.decode(errors="ignore").strip()
                    self.last_line = text
                    if text:
                        self.lines_seen += 1

                    if not text:
                        continue

                    # Expected format:
                    # 5.123,0.456,2.337
                    # Format 1:
                    # 5.123,0.456,2.337
                    if "," in text:
                        parts = text.split(",")

                        if len(parts) != 3:
                            self.lines_rejected += 1
                            continue

                        v, c, p = map(float, parts)

                    # Format 2:
                    # V=5.123V  I=0.456A  P=2.337W
                    else:
                        cleaned = (
                            text.replace("V=", "")
                                .replace("I=", "")
                                .replace("P=", "")
                                .replace("V", "")
                                .replace("A", "")
                                .replace("W", "")
                        )

                        parts = cleaned.split()

                        if len(parts) != 3:
                            self.lines_rejected += 1
                            continue

                        v, c, p = map(float, parts)

                    if not (
                        0.0 <= v <= 100.0 and
                        0.0 <= c <= 20.0 and
                        0.0 <= p <= 2000.0
                    ):
                        self.lines_rejected += 1
                        continue

                    latest_valid = Sample(
                        voltage_v=v,
                        current_a=c,
                        power_w=p,
                        dp_v=0.0,
                        dn_v=0.0,
                        temp_c=0.0,
                        timestamp=time.time()
                    )
                    break

                except Exception:
                    continue

            if latest_valid is not None:
                self.fail_count = 0
                return latest_valid

            self.fail_count += 1
            return None

        except Exception:
            self.disconnect()
            return None


