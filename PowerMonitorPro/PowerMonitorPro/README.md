# Power Monitor Pro v2.2

Power Monitor Pro is a modular Windows desktop application for live monitoring, graphing, diagnostics, playback and CSV logging from USB power meters and custom INA260-based bench meters.

## Supported devices

- FNIRSI USB power meters using the shared FNIRSI backend
  - listed generically as `FNIRSI Device #1`, `FNIRSI Device #2`, etc.
- ATORCH T3
- RP2040 INA260 Power Monitor
- ESP32 INA260 Power Monitor
- Mainboard Medics Meter

## v2.2 changes

### Mainboard Medics Meter backend

Added a dedicated backend for the Mainboard Medics RP2040/INA260 meter firmware.

The backend parses the German serial output:

```text
---- Messwerte ----
Adresse: 0x0
Spannung: 5.123 V
Strom: 0.456 A
Leistung: 2.337 W
-------------------
```

Mapped values:

- `Spannung` → Voltage
- `Strom` → Current
- `Leistung` → Power

The `Adresse` line is currently ignored for measurements.

### CSV logging workflow

Renamed `Live CSV` to `Enable CSV Logging`.

Pressing `START LOG` now automatically:

1. Enables CSV logging if it is not already enabled.
2. Opens the save-location dialog.
3. Starts writing directly to the selected CSV file.

This removes the need to manually tick CSV logging before starting a log.

### Device list cleanup

The app keeps FNIRSI devices generic and numbered to avoid duplicate model names when multiple FNIRSI devices are connected.

Example:

```text
FNIRSI Device #1
FNIRSI Device #2
RP2040 Power Monitor
ESP32 Power Monitor
Mainboard Medics Meter
ATORCH Device
```

## Key features

- Live voltage, current and power display
- Smooth scrolling graphs
- Mini floating window
- CSV logging
- CSV playback controls
- Device diagnostics window
- Scrollable raw data capture
- Pause, clear and copy raw data
- Modular backend structure for adding new devices

## Project structure

```text
backends/
  atorch_t3.py
  esp32_ina260.py
  fnirsi.py
  mainboard_medics.py
  rp2040_ina260.py

ui/
  main_window.py

config.py
helpers.py
main.py
models.py
```

## Building an EXE

From the project folder:

```powershell
python -m PyInstaller --clean --noconfirm --onefile --windowed --name "Power Monitor Pro v2.2" main.py
```

The executable will be created in the `dist` folder.

## Notes

Power Monitor Pro is designed to keep device protocols separate. Each supported device has its own backend, so adding or changing one device should not break existing devices.
