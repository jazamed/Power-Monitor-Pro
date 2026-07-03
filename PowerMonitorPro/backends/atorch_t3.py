import time
import hid

from models import Sample, DeviceCapabilities, DeviceInfo
from helpers import guess_protocol
from backends.base import MonitorBackendBase

# =========================================================
# Atorch T3 BACKEND
# =========================================================

class AtorchT3Backend(MonitorBackendBase):
    source_key = "atorch"
    source_label = "ATORCH T3"

    VID = 0x0483
    PID = 0x5750

    POLL_CMD_64 = bytes([
        0x55, 0x05, 0x22, 0x05, 0x0B, 0x00, 0x8C, 0xEE, 0xFF
    ] + [0x00] * 55)

    def __init__(self):
        self.dev = None
        self.connected = False
        self.fail_count = 0
        self.last_sample = None
        self.last_poll = 0.0
        self.last_good_data_time = 0.0
        self.connected_since = 0.0
        self.last_packet_hex = ""
        self.last_decoded = {}
        self.poll_writes = 0
        self.reads_ok = 0

    def capabilities(self):
        return DeviceCapabilities(
            supports_temp=True,
            supports_dpdm=True,
            supports_protocol=True,
        )

    def list_devices(self) -> list[DeviceInfo]:
        try:
            if hid.enumerate(self.VID, self.PID):
                return [
                    DeviceInfo(
                        device_id="atorch::default",
                        label="ATORCH T3",
                        source_key=self.source_key,
                    )
                ]
        except Exception as e:
            print("ATORCH list error:", e)

        return []

    def connect(self):
        return self.connect_to("atorch::default")

    def connect_to(self, device_id: str):
        try:
            self.disconnect()

            self.dev = hid.device()
            self.dev.open(self.VID, self.PID)
            self.dev.set_nonblocking(True)

            self.connected = True
            self.fail_count = 0
            self.last_sample = None
            self.last_poll = 0.0
            self.last_good_data_time = 0.0
            self.connected_since = time.time()

            print("ATORCH CONNECTED")
            return True

        except Exception as e:
            print("ATORCH connect error:", e)
            self.dev = None
            self.connected = False
            return False

    def disconnect(self):
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:
                pass

        self.dev = None
        self.connected = False
        self.fail_count = 0
        self.last_sample = None
        self.last_packet_hex = ""
        self.last_decoded = {}

    def debug_info(self):
        return {
            "vid_pid": f"{self.VID:04X}:{self.PID:04X}",
            "connected": self.connected,
            "fail_count": self.fail_count,
            "poll_writes": self.poll_writes,
            "reads_ok": self.reads_ok,
            "last_poll_age_s": round(time.time() - self.last_poll, 3) if self.last_poll else None,
            "last_good_age_s": round(time.time() - self.last_good_data_time, 3) if self.last_good_data_time else None,
            "connected_age_s": round(time.time() - self.connected_since, 3) if self.connected_since else None,
            "last_decoded": self.last_decoded,
            "last_packet_hex": self.last_packet_hex,
        }

    def is_connected(self):
        return self.connected and self.dev is not None

    def current_device_name(self):
        return "ATORCH T3"

    def protocol_text(self, sample: Sample) -> str:
        return guess_protocol(sample.dp_v, sample.dn_v, sample.voltage_v)

    def _send_poll(self):
        if self.dev is None:
            return

        now = time.time()
        if now - self.last_poll < 0.10:
            return

        self.last_poll = now

        try:
            # hidapi on Windows usually expects report ID first.
            self.dev.write([0x00] + list(self.POLL_CMD_64))
        except Exception as e:
            print("ATORCH poll write error:", e)

    def read_sample(self):
        if not self.dev:
            return None

        try:
            now = time.time()

            if now - self.last_poll > 0.03:
                try:
                    self.dev.write([0x00] + list(self.POLL_CMD_64))
                    self.poll_writes += 1
                except Exception as e:
                    print("ATORCH poll write error:", e)
                    self.disconnect()
                    return None

                self.last_poll = now

            data = self.dev.read(64)

            if not data:
                self.fail_count += 1

                # Give ATORCH time to respond after initial connection
                if now - self.connected_since < 3.0:
                    return self.last_sample

                # Keep last value briefly during normal packet gaps
                if self.last_good_data_time and (now - self.last_good_data_time) < 2.0:
                    return self.last_sample

                print("ATORCH LOST → disconnecting")
                self.disconnect()
                return None

            b = bytes(data)
            self.last_packet_hex = b.hex()
            self.reads_ok += 1
            
            if len(b) < 28 or b[0] != 0xAA:
                return self.last_sample

            voltage_v = int.from_bytes(b[8:12], "little") / 1_000_000.0
            current_a = int.from_bytes(b[12:16], "little") / 1_000_000.0
            power_w = int.from_bytes(b[16:20], "little") / 1_000_000.0

            # NEW fields
            dp_v = int.from_bytes(b[20:24], "little") / 1_000_000.0
            dn_v = int.from_bytes(b[24:28], "little") / 1_000_000.0
            temp_c = int.from_bytes(b[36:40], "little") / 1_000_000.0

            self.fail_count = 0
            self.last_good_data_time = time.time()
            self.last_decoded = {
                "voltage_v": round(voltage_v, 6),
                "current_a": round(current_a, 6),
                "power_w": round(power_w, 6),
                "dp_v": round(dp_v, 6),
                "dn_v": round(dn_v, 6),
                "temp_c": round(temp_c, 3),
            }

            if self.last_sample:
                voltage_v = (voltage_v + self.last_sample.voltage_v) / 2
                current_a = (current_a + self.last_sample.current_a) / 2
                power_w = (power_w + self.last_sample.power_w) / 2

            sample = Sample(
                voltage_v=voltage_v,
                current_a=current_a,
                power_w=power_w,
                dp_v=dp_v,
                dn_v=dn_v,
                temp_c=temp_c,
                timestamp=time.time()
            )

            self.last_sample = sample
            return sample

        except Exception as e:
            print("ATORCH read error:", e)
            self.disconnect()
            return None
    


