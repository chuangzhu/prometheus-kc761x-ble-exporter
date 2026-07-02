from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import time
from collections.abc import Sequence

from bleak import BleakClient, BleakScanner
from prometheus_client import Counter, Gauge, Info, start_http_server

from . import protocol


LOG = logging.getLogger("kc761x_exporter")


class KC761xMetrics:
    def __init__(self, enable_spectrum: bool, max_spectrum_channels: int | None) -> None:
        self.enable_spectrum = enable_spectrum
        self.max_spectrum_channels = max_spectrum_channels

        self.up = Gauge("kc761x_up", "Whether the BLE session is connected")
        self.last_packet = Gauge("kc761x_last_packet_timestamp_seconds", "Unix timestamp of the last parsed KC761x packet")
        self.decode_errors = Counter("kc761x_decode_errors_total", "Number of KC761x notification decode errors")

        self.battery = Gauge("kc761x_battery_percent", "Battery percentage")
        self.air_pressure = Gauge("kc761x_air_pressure_hpa", "Air pressure in hPa")
        self.temperature = Gauge("kc761x_device_temperature_celsius", "Device temperature")
        self.device_time = Gauge("kc761x_device_time_seconds", "Device clock as Unix timestamp")
        self.auto_upload = Gauge("kc761x_auto_upload_enabled", "Whether automatic upload is enabled")
        self.selected_sensor = Gauge("kc761x_sensor_selected", "Selected sensor index: 0 gamma, 1 neutron, 2 pin")
        self.sensor_accumulating = Gauge("kc761x_sensor_accumulating", "Whether sensor spectrum accumulation is enabled", ["slot", "sensor"])

        labels = ["slot", "sensor"]
        self.raw_cps = Gauge("kc761x_sensor_raw_cps", "Previous-second count rate", labels)
        self.avg_cps = Gauge("kc761x_sensor_avg_cps", "Smoothed count rate", labels)
        self.raw_dose = Gauge("kc761x_sensor_raw_dose_rate_mgy_per_hour", "Previous-second dose rate", labels)
        self.avg_dose = Gauge("kc761x_sensor_avg_dose_rate_mgy_per_hour", "Smoothed dose rate", labels)
        self.raw_dose_eq = Gauge(
            "kc761x_sensor_raw_dose_equivalent_rate_msv_per_hour",
            "Previous-second dose equivalent rate",
            labels,
        )
        self.avg_dose_eq = Gauge(
            "kc761x_sensor_avg_dose_equivalent_rate_msv_per_hour",
            "Smoothed dose equivalent rate",
            labels,
        )

        self.device_info = Info("kc761x_device", "KC761x device information")
        self.mc_runtime = Gauge("kc761x_sensor_multichannel_runtime_seconds", "Spectrum accumulation runtime", labels)
        self.dose_runtime = Gauge("kc761x_sensor_dose_runtime_seconds", "Dose accumulation runtime", labels)
        self.accumulated_dose = Gauge("kc761x_sensor_accumulated_dose_ugy", "Accumulated dose", labels)
        self.accumulated_dose_eq = Gauge("kc761x_sensor_accumulated_dose_equivalent_usv", "Accumulated dose equivalent", labels)

        self.spectrum = None
        self.pulse_total = None
        if enable_spectrum:
            self.spectrum = Gauge("kc761x_spectrum_counts", "Spectrum counts by source and channel", ["source", "channel"])
            self.pulse_total = Counter("kc761x_stream_pulses_total", "Stream-mode pulses observed by source", ["source"])

    def set_status(self, packet: protocol.StatusPacket) -> None:
        self.last_packet.set(time.time())
        self.battery.set(packet.battery_percent)
        self.air_pressure.set(packet.air_pressure_hpa)
        self.temperature.set(packet.device_temperature_c)
        self.device_time.set(packet.device_time_seconds)
        self.auto_upload.set(packet.auto_upload_status)
        self.selected_sensor.set(packet.selected_sensor)

        for slot, sensor_status in enumerate(packet.sensors):
            labels = (str(slot), protocol.sensor_name(slot))
            self.sensor_accumulating.labels(*labels).set(int(packet.sensor_accumulating(slot)))
            self.raw_cps.labels(*labels).set(sensor_status.raw_cps)
            self.raw_dose.labels(*labels).set(sensor_status.raw_dose_rate_mgy_h)
            self.raw_dose_eq.labels(*labels).set(sensor_status.raw_dose_equiv_rate_msv_h)
            self.avg_cps.labels(*labels).set(sensor_status.avg_cps)
            self.avg_dose.labels(*labels).set(sensor_status.avg_dose_rate_mgy_h)
            self.avg_dose_eq.labels(*labels).set(sensor_status.avg_dose_equiv_rate_msv_h)

    def set_device_info(self, packet: protocol.DeviceInfoPacket) -> None:
        self.last_packet.set(time.time())
        self.device_info.info(
            {
                "device_id": packet.device_id,
                "device_model": protocol.device_model_name(packet.device_model),
                "device_model_code": str(packet.device_model),
                "hardware_version": f"{packet.hardware_version:.1f}",
                "firmware_version": f"{packet.firmware_version:.2f}",
                "coprocessor_firmware_version": f"{packet.coprocessor_firmware_version:.2f}",
                "sensor0_type": protocol.sensor_type_name(packet.sensor_types[0]),
                "sensor1_type": protocol.sensor_type_name(packet.sensor_types[1]),
                "sensor2_type": protocol.sensor_type_name(packet.sensor_types[2]),
            }
        )
        for slot in range(3):
            labels = (str(slot), protocol.sensor_name(slot))
            self.mc_runtime.labels(*labels).set(packet.multichannel_runtime_seconds[slot])
            self.dose_runtime.labels(*labels).set(packet.dose_runtime_seconds[slot])
            self.accumulated_dose.labels(*labels).set(packet.accumulated_dose_ugy[slot])
            self.accumulated_dose_eq.labels(*labels).set(packet.accumulated_dose_equiv_usv[slot])

    def set_spectrum(self, packet: protocol.SpectrumPacket) -> None:
        self.last_packet.set(time.time())
        if self.spectrum is None:
            return
        source = protocol.sensor_name(packet.source)
        for channel, value in protocol.iter_spectrum_points(packet, self.max_spectrum_channels):
            self.spectrum.labels(source, str(channel)).set(value)

    def add_stream(self, packet: protocol.StreamPacket) -> None:
        self.last_packet.set(time.time())
        if self.pulse_total is None:
            return
        counts: dict[int, int] = {}
        for source, _channel in packet.pulses:
            counts[source] = counts.get(source, 0) + 1
        for source, count in counts.items():
            self.pulse_total.labels(protocol.sensor_name(source)).inc(count)


class KC761xExporter:
    def __init__(self, args: argparse.Namespace, metrics: KC761xMetrics) -> None:
        self.args = args
        self.metrics = metrics
        self._sync = 0

    async def run(self) -> None:
        while True:
            try:
                address = self.args.address or await self._discover_address(self.args.name, self.args.discovery_timeout)
                await self._run_session(address)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("BLE session failed")
                self.metrics.up.set(0)
                await asyncio.sleep(self.args.reconnect_delay)

    async def _discover_address(self, name: str, timeout: float) -> str:
        LOG.info("scanning for BLE device with name containing %r", name)
        devices = await BleakScanner.discover(timeout=timeout)
        for device in devices:
            if device.name and name.lower() in device.name.lower():
                LOG.info("found %s at %s", device.name, device.address)
                return device.address
        raise RuntimeError(f"no BLE device found with name containing {name!r}")

    async def _run_session(self, address: str) -> None:
        LOG.info("connecting to %s", address)
        async with BleakClient(address) as client:
            self.metrics.up.set(1)
            if self.args.mtu:
                await self._request_mtu(client, self.args.mtu)

            await client.start_notify(protocol.TX_CHAR_UUID, self._on_notify)

            if not self.args.disable_auto_upload:
                await self._write(client, protocol.command_set_auto_upload(self._next_sync(), True))

            await self._poll_loop(client)

    async def _poll_loop(self, client: BleakClient) -> None:
        last_device_info = 0.0
        last_spectrum = 0.0
        while client.is_connected:
            now = time.monotonic()
            await self._write(client, protocol.command_get_status(self._next_sync()))
            if now - last_device_info >= self.args.device_info_interval:
                await self._write(client, protocol.command_get_device_info(self._next_sync()))
                last_device_info = now
            if (
                self.args.enable_spectrum
                and self.args.request_spectrum_interval > 0
                and now - last_spectrum >= self.args.request_spectrum_interval
            ):
                for source in self.args.spectrum_source:
                    await self._write(client, protocol.command_get_spectrum(self._next_sync(), source))
                last_spectrum = now
            await asyncio.sleep(self.args.poll_interval)
        self.metrics.up.set(0)

    async def _request_mtu(self, client: BleakClient, mtu: int) -> None:
        request = getattr(client, "request_mtu", None)
        if request is None:
            LOG.info("BLE backend does not expose request_mtu(); continuing with default MTU")
            return
        try:
            negotiated = await request(mtu)
            LOG.info("requested MTU %s, negotiated %s", mtu, negotiated)
        except Exception:
            LOG.exception("MTU request failed; continuing")

    async def _write(self, client: BleakClient, payload: bytes) -> None:
        await client.write_gatt_char(protocol.RX_CHAR_UUID, payload, response=True)

    def _on_notify(self, _sender: object, data: bytearray) -> None:
        try:
            for packet in protocol.parse_packets(bytes(data)):
                self._handle_packet(packet)
        except Exception:
            self.metrics.decode_errors.inc()
            LOG.exception("failed to decode notification: %s", bytes(data).hex(" "))

    def _handle_packet(self, packet: protocol.Packet) -> None:
        if isinstance(packet, protocol.StatusPacket):
            self.metrics.set_status(packet)
        elif isinstance(packet, protocol.DeviceInfoPacket):
            self.metrics.set_device_info(packet)
        elif isinstance(packet, protocol.SpectrumPacket):
            self.metrics.set_spectrum(packet)
        elif isinstance(packet, protocol.StreamPacket):
            self.metrics.add_stream(packet)
        elif isinstance(packet, protocol.AckPacket) and not packet.ok:
            LOG.warning("device rejected command 0x%02x", packet.command)

    def _next_sync(self) -> int:
        self._sync = (self._sync + 1) & 0xFF
        return self._sync


async def async_main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    configure_logging(args.verbose)

    host, port = parse_listen(args.listen)
    start_http_server(port, addr=host)
    LOG.info("metrics listening on http://%s:%d/metrics", host, port)

    metrics = KC761xMetrics(args.enable_spectrum, args.max_spectrum_channels)
    exporter = KC761xExporter(args, metrics)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)

    task = asyncio.create_task(exporter.run())
    await stop.wait()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prometheus exporter for KC761x radiation meters over BLE")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--address", help="BLE address of the KC761x")
    target.add_argument("--name", help="Substring of advertised BLE device name to discover")
    parser.add_argument("--listen", default="0.0.0.0:9108", help="Metrics listen address, default: 0.0.0.0:9108")
    parser.add_argument("--poll-interval", type=float, default=10.0, help="Seconds between active status polls")
    parser.add_argument("--device-info-interval", type=float, default=60.0, help="Seconds between device info polls")
    parser.add_argument("--discovery-timeout", type=float, default=10.0, help="BLE discovery timeout in seconds")
    parser.add_argument("--reconnect-delay", type=float, default=5.0, help="Seconds before reconnect after failure")
    parser.add_argument("--mtu", type=int, default=517, help="Requested BLE MTU, if backend supports it")
    parser.add_argument("--disable-auto-upload", action="store_true", help="Do not enable KC761x automatic upload")
    parser.add_argument("--enable-spectrum", action="store_true", help="Expose spectrum channel gauges")
    parser.add_argument("--max-spectrum-channels", type=int, default=2048, help="Maximum spectrum channels to expose")
    parser.add_argument(
        "--request-spectrum-interval",
        type=float,
        default=0.0,
        help="Request full spectra each poll when > 0. Automatic spectrum uploads are still parsed.",
    )
    parser.add_argument(
        "--spectrum-source",
        type=int,
        action="append",
        choices=(0, 1, 2),
        default=[],
        help="Spectrum source to request: 0 gamma, 1 neutron, 2 pin. May be repeated.",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase log verbosity")
    args = parser.parse_args(argv)
    if args.request_spectrum_interval > 0 and not args.spectrum_source:
        args.spectrum_source = [0]
    return args


def parse_listen(value: str) -> tuple[str, int]:
    if ":" not in value:
        return "0.0.0.0", int(value)
    host, port = value.rsplit(":", 1)
    return host, int(port)


def configure_logging(verbose: int) -> None:
    level = logging.WARNING
    if verbose == 1:
        level = logging.INFO
    elif verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main(argv: Sequence[str] | None = None) -> None:
    asyncio.run(async_main(argv))


if __name__ == "__main__":
    main()
