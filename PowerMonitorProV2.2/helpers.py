import math
import time
from models import Sample

# =========================================================
# HELPERS
# =========================================================

def coulombs_to_mah(coulombs: float) -> float:
    return coulombs / 3.6


def joules_to_wh(joules: float) -> float:
    return joules / 3600.0


def guess_protocol(dp_v: float, dn_v: float, voltage_v: float) -> str:
    if voltage_v >= 8.5:
        if 0.4 <= dp_v <= 0.8 and 0.4 <= dn_v <= 0.8:
            return "USB PD / High Voltage"
        if 3.0 <= dp_v <= 3.6 and 0.4 <= dn_v <= 0.8:
            return "QC likely"
        if 0.4 <= dp_v <= 0.8 and 3.0 <= dn_v <= 3.6:
            return "QC likely"
        return "Fast Charge Active"

    if 1.8 <= dp_v <= 2.8 and 1.8 <= dn_v <= 2.8:
        return "Apple / BC / Proprietary"
    if 0.4 <= dp_v <= 0.9 and 0.4 <= dn_v <= 0.9:
        return "USB Data / Default"
    if dp_v < 0.2 and dn_v < 0.2:
        return "No data lines / Unknown"
    return "Unknown"


def clamp_min_zero(v: float) -> float:
    return max(0.0, v)


def parse_bool(val, default=False):
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("1", "true", "yes", "on")
    return default




def average_samples(samples):
    if not samples:
        return None
    n = len(samples)
    return Sample(
        voltage_v=sum(s.voltage_v for s in samples) / n,
        current_a=sum(s.current_a for s in samples) / n,
        power_w=sum(s.power_w for s in samples) / n,
        dp_v=sum(s.dp_v for s in samples) / n,
        dn_v=sum(s.dn_v for s in samples) / n,
        temp_c=sum(s.temp_c for s in samples) / n,
        timestamp=max(s.timestamp for s in samples),
    )


