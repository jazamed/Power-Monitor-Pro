Power Monitor Pro 

<img width="1206" height="1012" alt="Main Window" src="https://github.com/user-attachments/assets/77b19ca6-8ea4-4f84-94c1-711c7ba699dc" />

<img width="717" height="385" alt="Mini Window" src="https://github.com/user-attachments/assets/96ddb18e-f462-48b3-81cc-edca3cfb6291" />


About the Project

Power Monitor Pro was developed to provide a single monitoring application for a wide range of USB power meters and custom-built instruments. Rather than relying on manufacturer-specific software, the goal is to offer a unified, extensible platform that combines live monitoring, diagnostics, logging and protocol analysis in one place.

Power Monitor Pro is a desktop application designed to monitor, analyse and log data from a growing range of USB power meters, bench power monitors and custom hardware.

The project began as a simple monitor for the FNIRSI FNB58 but has evolved into a modular platform capable of supporting multiple devices through independent backends. The long-term aim is to provide a single application capable of communicating with commercial USB testers as well as custom-built hardware, with future support for network-connected instruments.

Features
Multi-device support

Currently supported devices include:

FNIRSI USB Power Meters
ATORCH T3
RP2040 INA260 Power Monitor
ESP32 INA260 Power Monitor

The modular backend architecture makes it straightforward to add support for additional devices in the future.

Live Monitoring

Display live:

Voltage
Current
Power
Temperature (where available)
D+
D−
Additional device-specific parameters
High-Speed Graphing

Real-time scrolling graphs with smooth updates.

Features include:

Voltage graph
Current graph
Power graph
Automatic scaling
Peak value tracking
Adjustable history length
Mini Window

A compact always-on-top floating display suitable for:

OBS recording
Livestream overlays
Small desktop monitoring
Device Diagnostics

Integrated diagnostics window providing:

Connection status
Backend information
Sample rate (Hz)
Packet statistics
Raw device data
Scrollable raw packet log
Pause/resume capture
Copy raw data for protocol analysis

This makes reverse engineering and adding support for new hardware significantly easier.

Data Logging

Record live measurements for later analysis.

Future versions will include:

CSV export
Session playback
Long-term logging
Modular Architecture

Each supported device has its own backend.

For example:

backends/

atorch_t3.py

fnirsi.py

rp2040_ina260.py

esp32_ina260.py

Adding a new device generally requires only a new backend without modifying the rest of the application.

ESP32 Power Monitor

Version 2 introduces support for a custom ESP32 + INA260 power monitor.

Features include:

Live voltage/current monitoring
Integrated OLED display
Up to approximately 17 Hz update rate
Compatible with existing Lab Meter software
Designed for future Wi-Fi support


Project Goals

Power Monitor Pro aims to become a universal monitoring application for:

USB power meters
Bench power supplies
Electronic loads
Digital multimeters
Custom microcontroller-based instruments

while maintaining a consistent user interface regardless of the connected hardware.

Planned Features
Wi-Fi connected ESP32 devices
OTA firmware updates
Automatic device discovery
Multiple simultaneous device monitoring
Additional FNIRSI device support
Oscilloscope-style graphing
CSV import/export
Session playback
Theme support
Plug-in backend system
Built With
Python
PySide6
PyQtGraph
PySerial
Current Status

Power Monitor Pro v2.2 is considered stable for everyday use.

Development continues with a focus on expanding hardware support while maintaining compatibility with existing devices.

Contributing

Contributions, protocol information and testing on additional devices are always welcome.

If you own hardware that is not currently supported, please feel free to submit raw packet captures or open an issue.

License

To be determined.

