# Power Monitor Pro v2.1

Built directly from the uploaded working `IntegratedV1.6.py`.

Run:

```powershell
python main.py
```

Device backends are in:

- `backends/fnirsi.py`
- `backends/atorch_t3.py`
- `backends/rp2040_ina260.py`

To add a new device later, create a new backend file that implements `MonitorBackendBase`, then register it in `ui/main_window.py`.


## v1.8 changes
- Added separate smooth render timer for graph panning.
- Full and mini graphs now float/scroll between data samples while device polling remains stable.


## v1.8 Hz Counter Update

Added live diagnostics in the info panel:
- Rate: live samples per second over the last 1 second
- Samples: accepted samples
- Dropped: update ticks without a valid sample
- Quality: accepted / total update attempts

Run with `python main.py`.


## v2.1 changes
- Added ESP32 INA260 serial backend for firmware output using `Voltage:` / `Current:` lines.
- Added selectable FNIRSI C2 profile using the existing FNIRSI aa04 decoder for now.
- Kept FNB58 decoding unchanged; C2 CC decoding can be added later once the extra packet/command is identified.
