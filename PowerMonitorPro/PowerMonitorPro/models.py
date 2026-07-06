from dataclasses import dataclass

# =========================================================
# DATA MODELS
# =========================================================

@dataclass
class Sample:
    voltage_v: float
    current_a: float
    power_w: float
    dp_v: float
    dn_v: float
    temp_c: float
    timestamp: float


@dataclass
class DeviceCapabilities:
    supports_temp: bool = False
    supports_dpdm: bool = False
    supports_protocol: bool = False


@dataclass
class DeviceInfo:
    device_id: str
    label: str
    source_key: str


