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

Useful options:

- `--poll-interval 10`: actively request real-time status every 10 seconds.
- `--device-info-interval 60`: refresh static/statistical device info every 60 seconds.
- `--disable-auto-upload`: do not enable the device's 1 Hz automatic upload mode.
- `--enable-spectrum`: expose per-channel spectrum gauges as `kc761x_spectrum_counts{source,channel}`. This can create thousands of time series.
- `--mtu 517`: request a large BLE MTU when the platform/backend supports it.

## Metrics

The exporter exposes:

- `kc761x_up`: BLE session is connected.
- `kc761x_last_packet_timestamp_seconds`: last parsed KC761x packet time.
- `kc761x_battery_percent`
- `kc761x_air_pressure_hpa`
- `kc761x_device_temperature_celsius`
- `kc761x_device_time_seconds`
- `kc761x_auto_upload_enabled`
- `kc761x_sensor_selected`
- `kc761x_sensor_accumulating{slot}`
- `kc761x_sensor_raw_cps{slot,sensor}`
- `kc761x_sensor_avg_cps{slot,sensor}`
- `kc761x_sensor_raw_dose_rate_mgy_per_hour{slot,sensor}`
- `kc761x_sensor_avg_dose_rate_mgy_per_hour{slot,sensor}`
- `kc761x_sensor_raw_dose_equivalent_rate_msv_per_hour{slot,sensor}`
- `kc761x_sensor_avg_dose_equivalent_rate_msv_per_hour{slot,sensor}`
- `kc761x_device_info{...} 1`
- `kc761x_sensor_accumulated_dose_ugy{slot,sensor}`
- `kc761x_sensor_accumulated_dose_equivalent_usv{slot,sensor}`
- `kc761x_sensor_multichannel_runtime_seconds{slot,sensor}`
- `kc761x_sensor_dose_runtime_seconds{slot,sensor}`

Disabled sensors report `-1` in the KC761x protocol. The exporter preserves that value.

## Prometheus scrape config

```yaml
scrape_configs:
  - job_name: kc761x
    static_configs:
      - targets: ["kc761x-host:9108"]
```

