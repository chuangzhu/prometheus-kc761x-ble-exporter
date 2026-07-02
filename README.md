# KC761x BLE Prometheus Exporter

Prometheus exporter for KC761A/B/C/CN radiation meters using the KC761x BLE UART-like protocol.

The device exposes a Nordic-UART-style GATT service:

- RX write characteristic: `6E400002-B5A3-F393-E0A9-E50E24DCCA9E`
- TX notify characteristic: `6E400003-B5A3-F393-E0A9-E50E24DCCA9E`

## Install

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

On Linux, make sure the user running the exporter can access Bluetooth through BlueZ.

## Run

Connect by BLE address:

```sh
kc761x-ble-exporter --address AA:BB:CC:DD:EE:FF --listen 0.0.0.0:9108
```

Or discover by advertised name:

```sh
kc761x-ble-exporter --name KC761 --listen 0.0.0.0:9108
```

Then scrape:

```sh
curl http://127.0.0.1:9108/metrics
```

The exporter is synchronous from Prometheus' point of view: it reads status/device info only when Prometheus scrapes `/metrics`. A separate background loop establishes and maintains the BLE connection. If the device is not connected when Prometheus scrapes, the exporter returns `kc761x_up=0` immediately instead of blocking on reconnect. It does not poll the device on its own timer and it does not attach Prometheus sample timestamps.

Useful options:

- `--scrape-timeout 9`: exporter-side maximum seconds for a scrape.
- `--command-timeout 8`: seconds to wait for each KC761x command response.
- `--discovery-timeout 5`: BLE discovery timeout when using `--name`.
- `--reconnect-interval 5`: seconds between background BLE reconnect attempts.
- `--enable-spectrum`: expose per-channel spectrum gauges as `kc761x_spectrum_counts{source,channel}`. This can create thousands of time series.
- `--spectrum-source 0`: spectrum source to request when spectrum export is enabled. Repeat for multiple sources.
- `--mtu 517`: request a large BLE MTU when the platform/backend supports it.

Prometheus' default scrape timeout is 10 seconds. Startup and reconnect discovery happen outside the scrape path; use `--address` for more predictable connection setup. If `--enable-spectrum` is used, a connected scrape can exceed 10 seconds depending on MTU, packet loss, and selected sources. In that case set an explicit Prometheus `scrape_timeout` greater than the exporter `--scrape-timeout`, or leave spectrum export disabled.

## Metrics

The exporter exposes:

- `kc761x_up`: KC761x BLE device is connected and the scrape succeeded.
- `kc761x_scrape_duration_seconds`
- `kc761x_scrape_decode_errors`
- `kc761x_battery_ratio`
- `kc761x_air_pressure_hpa`
- `kc761x_device_temperature_celsius`
- `kc761x_device_time_seconds`: device clock value, not a Prometheus sample timestamp.
- `kc761x_auto_upload_enabled`
- `kc761x_sensor_selected`
- `kc761x_sensor_accumulating{slot}`
- `kc761x_sensor_raw_cps{slot,sensor}`
- `kc761x_sensor_avg_cps{slot,sensor}`
- `kc761x_sensor_raw_dose_rate_milligrays_per_hour{slot,sensor}`
- `kc761x_sensor_avg_dose_rate_milligrays_per_hour{slot,sensor}`
- `kc761x_sensor_raw_dose_equivalent_rate_millisieverts_per_hour{slot,sensor}`
- `kc761x_sensor_avg_dose_equivalent_rate_millisieverts_per_hour{slot,sensor}`
- `kc761x_device_info{...} 1`
- `kc761x_sensor_dose_micrograys_total{slot,sensor}`
- `kc761x_sensor_dose_equivalent_microsieverts_total{slot,sensor}`
- `kc761x_sensor_multichannel_runtime_seconds_total{slot,sensor}`
- `kc761x_sensor_dose_runtime_seconds_total{slot,sensor}`
- `kc761x_spectrum_counts{source,channel}` when `--enable-spectrum` is set.

Disabled sensors report `-1` in the KC761x protocol. The exporter omits negative sensor samples instead of exporting them.

## Prometheus scrape config

```yaml
scrape_configs:
  - job_name: kc761x
    scrape_timeout: 10s
    static_configs:
      - targets: ["kc761x-host:9108"]
```

## Grafana

Import [dashboards/kc761x-dashboard.json](./dashboards/kc761x-dashboard.json) and select your Prometheus datasource. The dashboard includes an optional spectrum panel that only shows data when the exporter is started with `--enable-spectrum`.
