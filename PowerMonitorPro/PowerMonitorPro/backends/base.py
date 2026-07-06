from models import DeviceCapabilities, DeviceInfo, Sample

# =========================================================
# BACKEND BASE
# =========================================================

class MonitorBackendBase:
    source_key = "base"
    source_label = "Base"

    def list_devices(self) -> list[DeviceInfo]:
        raise NotImplementedError

    def connect(self):
        raise NotImplementedError

    def connect_to(self, device_id: str):
        raise NotImplementedError

    def disconnect(self):
        raise NotImplementedError

    def is_connected(self):
        raise NotImplementedError

    def read_sample(self):
        raise NotImplementedError

    def current_device_name(self):
        raise NotImplementedError

    def capabilities(self) -> DeviceCapabilities:
        return DeviceCapabilities()

    def protocol_text(self, sample: Sample) -> str:
        return "-"

    def debug_info(self) -> dict:
        return {}


