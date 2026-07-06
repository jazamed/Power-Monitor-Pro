import time
import hid

from config import READ_LEN, REPORT_ID, SAMPLE_SIZE, NUM_SAMPLES, UI_INTERVAL_MS, ENABLE_KEEPALIVE, KEEPALIVE_INTERVAL_S
from models import Sample, DeviceCapabilities, DeviceInfo
from helpers import guess_protocol, average_samples
from backends.base import MonitorBackendBase

# =========================================================
# FNIRSI DEVICE PROFILES
# =========================================================

class FNIRSIProfile:
    name = "Unknown FNIRSI"
    vid_pids = []
    preferred_interfaces = (0, 1)
    use_init = True
    use_keepalive = True
    poll_interval_ms = UI_INTERVAL_MS

    def __init__(self):
        payload_aa81 = bytes([0xAA, 0x81] + [0x00] * 61 + [0x8E])
        payload_aa82 = bytes([0xAA, 0x82] + [0x00] * 61 + [0x96])
        payload_aa83 = bytes([0xAA, 0x83] + [0x00] * 61 + [0x9E])

        self.init_cmd = [REPORT_ID] + list(payload_aa81)
        self.poll_cmd = [REPORT_ID] + list(payload_aa82)
        self.keepalive_cmd = [REPORT_ID] + list(payload_aa83)

    def decode_packet(self, pkt: bytes):
        samples = []

        for i in range(NUM_SAMPLES):
            off = 2 + i * SAMPLE_SIZE
            if off + SAMPLE_SIZE > len(pkt):
                break

            try:
                v_raw = int.from_bytes(pkt[off + 0:off + 4], "little")
                c_raw = int.from_bytes(pkt[off + 4:off + 8], "little")
                dp_raw = int.from_bytes(pkt[off + 8:off + 10], "little")
                dn_raw = int.from_bytes(pkt[off + 10:off + 12], "little")
                t_raw = int.from_bytes(pkt[off + 13:off + 15], "little")
            except Exception:
                continue

            voltage_v = v_raw * 1e-5
            current_a = c_raw * 1e-5
            power_w = voltage_v * current_a
            dp_v = dp_raw * 1e-3
            dn_v = dn_raw * 1e-3
            temp_c = t_raw * 0.1

            samples.append(Sample(
                voltage_v=voltage_v,
                current_a=current_a,
                power_w=power_w,
                dp_v=dp_v,
                dn_v=dn_v,
                temp_c=temp_c,
                timestamp=time.time()
            ))

        return samples

    def is_valid_sample(self, sample: Sample) -> bool:
        return (
            0.0 <= sample.voltage_v <= 100.0 and
            0.0 <= sample.current_a <= 20.0 and
            0.0 <= sample.dp_v <= 5.5 and
            0.0 <= sample.dn_v <= 5.5 and
            -50.0 <= sample.temp_c <= 200.0
        )


class FNB58Profile(FNIRSIProfile):
    name = "FNB58"
    vid_pids = [
        (0x2E3C, 0x5558),
        (0x2E3C, 0x5555),
    ]


class FNIRSIC2Profile(FNIRSIProfile):
    # C2 currently uses the same aa04 sample decoder as the FNB58 family.
    # It is separated as its own selectable profile so we can add CC-specific
    # decoding later without affecting the FNB58 backend.
    name = "C2"
    vid_pids = [
        (0x2E3C, 0x5558),
        (0x2E3C, 0x5555),
    ]


class FNB48Profile(FNIRSIProfile):
    name = "FNB48"
    vid_pids = [
        (0x0483, 0x003A),
    ]


class FNB48PSProfile(FNIRSIProfile):
    name = "FNB48P/S"
    vid_pids = [
        (0x2E3C, 0x0049),
    ]


SUPPORTED_PROFILES = [
    FNB58Profile(),
    FNB48Profile(),
    FNB48PSProfile(),
]


# =========================================================
# FNIRSI BACKEND
# =========================================================

class FNIRSIBackend(MonitorBackendBase):
    source_key = "fnirsi"
    source_label = "FNIRSI"

    def __init__(self, profiles):
        self.profiles = profiles
        self.dev = None
        self.path = None
        self.profile = None
        self.fail_count = 0
        self.last_ok = 0.0
        self.last_keepalive = 0.0
        self.connected_label = "FNIRSI"
        self.last_packet_hex = ""
        self.last_sample_count = 0

    def capabilities(self):
        return DeviceCapabilities(
            supports_temp=True,
            supports_dpdm=True,
            supports_protocol=True,
        )

    def protocol_text(self, sample: Sample) -> str:
        return guess_protocol(sample.dp_v, sample.dn_v, sample.voltage_v)

    def is_connected(self):
        return self.dev is not None and self.profile is not None

    def current_device_name(self):
        return self.connected_label if self.profile else "FNIRSI"

    def disconnect(self):
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:
                pass
        self.dev = None
        self.path = None
        self.profile = None
        self.fail_count = 0
        self.last_ok = 0.0
        self.last_keepalive = 0.0
        self.connected_label = "FNIRSI"
        self.last_packet_hex = ""
        self.last_sample_count = 0

    def enumerate_candidates(self):
        candidates = []
        seen_paths = set()

        for profile in self.profiles:
            for vid, pid in profile.vid_pids:
                try:
                    devices = hid.enumerate(vid, pid)
                except Exception:
                    continue

                for info in devices:
                    path = info.get("path")
                    if not path:
                        continue

                    path_key = path.hex() if isinstance(path, (bytes, bytearray)) else str(path)

                    # Several FNIRSI meters can share the same VID/PID and HID packet
                    # format. Deduplicate by HID path so each physical meter appears
                    # once only, even if multiple FNIRSI model profiles could match it.
                    if path_key in seen_paths:
                        continue
                    seen_paths.add(path_key)

                    iface = info.get("interface_number", -1)
                    manufacturer = str(info.get("manufacturer_string") or "")
                    product = str(info.get("product_string") or "")
                    serial = str(info.get("serial_number") or "")

                    candidates.append({
                        "profile": profile,
                        "info": info,
                        "device_id": f"fnirsi::{path_key}",
                        "label": "FNIRSI Device",
                        "iface": iface,
                        "manufacturer": manufacturer,
                        "product": product,
                        "serial": serial,
                    })

        def sort_key(item):
            profile = item["profile"]
            iface = item["iface"]
            preferred_rank = 0 if iface in profile.preferred_interfaces else 1
            return (preferred_rank, iface, item["device_id"])

        candidates.sort(key=sort_key)

        for idx, item in enumerate(candidates, start=1):
            item["label"] = f"FNIRSI Device #{idx}"

        return candidates

    def list_devices(self) -> list[DeviceInfo]:
        return [
            DeviceInfo(
                device_id=item["device_id"],
                label=item["label"],
                source_key=self.source_key,
            )
            for item in self.enumerate_candidates()
        ]

    def _try_open_and_probe(self, profile, info):
        path = info.get("path")
        if not path:
            return None, None

        d = None
        try:
            d = hid.device()
            d.open_path(path)
            d.set_nonblocking(False)

            if profile.use_init:
                try:
                    d.write(profile.init_cmd)
                    time.sleep(0.05)
                except Exception:
                    pass

            time.sleep(0.10)

            for _ in range(3):
                try:
                    d.write(profile.poll_cmd)
                    time.sleep(0.015)
                    data = d.read(READ_LEN, timeout_ms=120)
                except Exception:
                    continue

                if not data:
                    continue

                pkt = bytes(data)
                if len(pkt) >= 2 and pkt[0] == 0xAA and pkt[1] == 0x04:
                    samples = profile.decode_packet(pkt)
                    valid = [s for s in samples if profile.is_valid_sample(s)]
                    if valid:
                        return d, average_samples(valid)

            d.close()
            return None, None

        except Exception:
            if d is not None:
                try:
                    d.close()
                except Exception:
                    pass
            return None, None

    def connect(self):
        if self.is_connected():
            return True

        for item in self.enumerate_candidates():
            d, _sample = self._try_open_and_probe(item["profile"], item["info"])
            if d is not None:
                self.dev = d
                self.path = item["info"].get("path")
                self.profile = item["profile"]
                self.fail_count = 0
                self.last_ok = time.time()
                self.last_keepalive = 0.0
                self.connected_label = item["label"]
                return True

        return False

    def connect_to(self, device_id: str):
        if self.is_connected():
            self.disconnect()

        for item in self.enumerate_candidates():
            if item["device_id"] != device_id:
                continue

            d, _sample = self._try_open_and_probe(item["profile"], item["info"])
            if d is not None:
                self.dev = d
                self.path = item["info"].get("path")
                self.profile = item["profile"]
                self.fail_count = 0
                self.last_ok = time.time()
                self.last_keepalive = 0.0
                self.connected_label = item["label"]
                return True

        return False

    def read_sample(self):
        if not self.is_connected():
            return None

        try:
            now = time.time()

            if self.profile.use_keepalive and ENABLE_KEEPALIVE:
                if (now - self.last_keepalive) >= KEEPALIVE_INTERVAL_S:
                    try:
                        self.dev.write(self.profile.keepalive_cmd)
                        self.last_keepalive = now
                        time.sleep(0.01)
                    except Exception:
                        pass

            self.dev.write(self.profile.poll_cmd)
            time.sleep(0.015)

            data = self.dev.read(READ_LEN, timeout_ms=120)
            if not data:
                self.fail_count += 1
                return None

            pkt = bytes(data)
            self.last_packet_hex = pkt.hex()
            if len(pkt) >= 2 and pkt[0] == 0xAA and pkt[1] == 0x04:
                samples = self.profile.decode_packet(pkt)
                valid = [s for s in samples if self.profile.is_valid_sample(s)]
                if valid:
                    self.fail_count = 0
                    self.last_ok = time.time()
                    self.last_sample_count = len(valid)
                    return average_samples(valid)

            self.fail_count += 1
            return None

        except Exception:
            self.disconnect()
            return None

