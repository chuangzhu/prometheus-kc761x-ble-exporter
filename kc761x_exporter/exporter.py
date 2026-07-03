from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from concurrent.futures import TimeoutError as FutureTimeoutError

from bleak import BleakClient, BleakScanner
from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, HistogramMetricFamily, InfoMetricFamily, Metric

from . import protocol


LOG = logging.getLogger("kc761x_exporter")


@dataclass
class ScrapeResult:
    up: int = 0
    status: protocol.StatusPacket | None = None
    device_info: protocol.DeviceInfoPacket | None = None
    calibration: protocol.CalibrationPacket | None = None
    spectra: list[protocol.SpectrumPacket] = field(default_factory=list)
    decode_errors: int = 0
    error: str = ""
    duration_seconds: float = 0.0


class KC761xCollector:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self._sync = 0
        self._lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="kc761x-ble-loop", daemon=True)
        self._thread.start()
        self._client = KC761xBleClient(
            address=self.args.address,
            name=self.args.name,
            discovery_timeout=self.args.discovery_timeout,
            command_timeout=self.args.command_timeout,
            spectrum_idle_timeout=self.args.spectrum_idle_timeout,
            mtu=self.args.mtu,
            reconnect_interval=self.args.reconnect_interval,
            next_sync=self._next_sync,
        )
        self._connection_task = asyncio.run_coroutine_threadsafe(self._client.run_connection_loop(), self._loop)

    def collect(self) -> Iterable[Metric]:
        with self._lock:
            result = self._scrape()
        yield from self._metric_families(result)

    def close(self) -> None:
        future = asyncio.run_coroutine_threadsafe(self._client.close(), self._loop)
        try:
            future.result(timeout=5)
        except Exception:
            LOG.exception("failed to close KC761x BLE client")
        self._connection_task.cancel()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()
        self._loop.close()

    def _scrape(self) -> ScrapeResult:
        started = time.monotonic()
        result = ScrapeResult()
        if not self._client.is_connected:
            result.error = "KC761x BLE device is not connected"
            result.duration_seconds = time.monotonic() - started
            return result

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._client.scrape(self.args.enable_spectrum, self.args.spectrum_source),
                self._loop,
            )
            result = future.result(timeout=self.args.scrape_timeout)
            result.up = 1
        except FutureTimeoutError:
            future.cancel()
            LOG.warning("KC761x scrape exceeded %.1f seconds", self.args.scrape_timeout)
            result.error = f"scrape exceeded {self.args.scrape_timeout:.1f} seconds"
        except Exception as exc:
            LOG.warning("KC761x scrape failed: %s", exc)
            result.error = str(exc)
        result.duration_seconds = time.monotonic() - started
        return result

    def _metric_families(self, result: ScrapeResult) -> Iterable[Metric]:
        up = GaugeMetricFamily("kc761x_up", "Whether the KC761x BLE scrape succeeded")
        up.add_metric([], result.up)
        yield up

        duration = GaugeMetricFamily("kc761x_scrape_duration_seconds", "Duration of the KC761x BLE scrape")
        duration.add_metric([], result.duration_seconds)
        yield duration

        decode_errors = GaugeMetricFamily(
            "kc761x_scrape_decode_errors",
            "Number of KC761x notification decode errors during this scrape",
        )
        decode_errors.add_metric([], result.decode_errors)
        yield decode_errors

        if result.error:
            error = InfoMetricFamily("kc761x_scrape_error", "Last KC761x scrape error")
            error.add_metric([], {"message": result.error})
            yield error

        if result.status is not None:
            yield from self._status_metrics(result.status)

        if result.device_info is not None:
            yield from self._device_info_metrics(result.device_info)

        if self.args.enable_spectrum:
            yield from self._spectrum_metrics(result.spectra, result.calibration)

    def _status_metrics(self, packet: protocol.StatusPacket) -> Iterable[Metric]:
        battery = GaugeMetricFamily("kc761x_battery_ratio", "Battery charge ratio from the KC761x")
        battery.add_metric([], packet.battery_percent / 100.0)
        yield battery

        air_pressure = GaugeMetricFamily("kc761x_air_pressure_hpa", "Air pressure in hPa from the KC761x")
        air_pressure.add_metric([], packet.air_pressure_hpa)
        yield air_pressure

        temperature = GaugeMetricFamily("kc761x_device_temperature_celsius", "Device temperature from the KC761x")
        temperature.add_metric([], packet.device_temperature_c)
        yield temperature

        device_time = GaugeMetricFamily(
            "kc761x_device_time_seconds",
            "KC761x device clock as Unix time in seconds; this is a device value, not a Prometheus sample timestamp",
        )
        device_time.add_metric([], packet.device_time_seconds)
        yield device_time

        auto_upload = GaugeMetricFamily("kc761x_auto_upload_enabled", "Whether KC761x automatic upload is enabled")
        auto_upload.add_metric([], packet.auto_upload_status)
        yield auto_upload

        selected_sensor = GaugeMetricFamily("kc761x_sensor_selected", "Selected sensor index: 0 gamma, 1 neutron, 2 pin")
        selected_sensor.add_metric([], packet.selected_sensor)
        yield selected_sensor

        accumulating = GaugeMetricFamily(
            "kc761x_sensor_accumulating",
            "Whether sensor spectrum accumulation is enabled",
            labels=["slot", "sensor"],
        )
        raw_cps = GaugeMetricFamily("kc761x_sensor_raw_cps", "Previous-second count rate from the KC761x", labels=["slot", "sensor"])
        avg_cps = GaugeMetricFamily("kc761x_sensor_avg_cps", "Smoothed count rate from the KC761x", labels=["slot", "sensor"])
        raw_dose = GaugeMetricFamily(
            "kc761x_sensor_raw_dose_rate_milligrays_per_hour",
            "Previous-second dose rate from the KC761x",
            labels=["slot", "sensor"],
        )
        avg_dose = GaugeMetricFamily(
            "kc761x_sensor_avg_dose_rate_milligrays_per_hour",
            "Smoothed dose rate from the KC761x",
            labels=["slot", "sensor"],
        )
        raw_dose_eq = GaugeMetricFamily(
            "kc761x_sensor_raw_dose_equivalent_rate_millisieverts_per_hour",
            "Previous-second dose equivalent rate from the KC761x",
            labels=["slot", "sensor"],
        )
        avg_dose_eq = GaugeMetricFamily(
            "kc761x_sensor_avg_dose_equivalent_rate_millisieverts_per_hour",
            "Smoothed dose equivalent rate from the KC761x",
            labels=["slot", "sensor"],
        )

        for slot, sensor_status in enumerate(packet.sensors):
            labels = [str(slot), protocol.sensor_name(slot)]
            accumulating.add_metric(labels, int(packet.sensor_accumulating(slot)))
            _add_nonnegative(raw_cps, labels, sensor_status.raw_cps)
            _add_nonnegative(avg_cps, labels, sensor_status.avg_cps)
            _add_nonnegative(raw_dose, labels, sensor_status.raw_dose_rate_mgy_h)
            _add_nonnegative(avg_dose, labels, sensor_status.avg_dose_rate_mgy_h)
            _add_nonnegative(raw_dose_eq, labels, sensor_status.raw_dose_equiv_rate_msv_h)
            _add_nonnegative(avg_dose_eq, labels, sensor_status.avg_dose_equiv_rate_msv_h)

        yield accumulating
        yield raw_cps
        yield avg_cps
        yield raw_dose
        yield avg_dose
        yield raw_dose_eq
        yield avg_dose_eq

    def _device_info_metrics(self, packet: protocol.DeviceInfoPacket) -> Iterable[Metric]:
        info = InfoMetricFamily("kc761x_device", "KC761x device information")
        info.add_metric(
            [],
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
            },
        )
        yield info

        labels = ["slot", "sensor"]
        mc_runtime = CounterMetricFamily(
            "kc761x_sensor_multichannel_runtime_seconds_total",
            "Total spectrum accumulation runtime reported by the KC761x",
            labels=labels,
        )
        dose_runtime = CounterMetricFamily(
            "kc761x_sensor_dose_runtime_seconds_total",
            "Total dose accumulation runtime reported by the KC761x",
            labels=labels,
        )
        dose = CounterMetricFamily(
            "kc761x_sensor_dose_micrograys_total",
            "Total absorbed dose reported by the KC761x",
            labels=labels,
        )
        dose_eq = CounterMetricFamily(
            "kc761x_sensor_dose_equivalent_microsieverts_total",
            "Total dose equivalent reported by the KC761x",
            labels=labels,
        )
        for slot in range(3):
            metric_labels = [str(slot), protocol.sensor_name(slot)]
            _add_nonnegative(mc_runtime, metric_labels, packet.multichannel_runtime_seconds[slot])
            _add_nonnegative(dose_runtime, metric_labels, packet.dose_runtime_seconds[slot])
            _add_nonnegative(dose, metric_labels, packet.dose_micrograys_total[slot])
            _add_nonnegative(dose_eq, metric_labels, packet.dose_equivalent_microsieverts_total[slot])
        yield mc_runtime
        yield dose_runtime
        yield dose
        yield dose_eq

    def _spectrum_metrics(
        self,
        spectra: Sequence[protocol.SpectrumPacket],
        calibration: protocol.CalibrationPacket | None,
    ) -> Iterable[Metric]:
        spectrum = HistogramMetricFamily(
            "kc761x_spectrum_electronvolts",
            "Spectrum event distribution by calibrated energy. Disabled by default because it creates thousands of buckets.",
            labels=["source"],
        )
        if calibration is None:
            yield spectrum
            return

        source_buckets: dict[int, dict[int, int]] = {}
        for packet in spectra:
            buckets = source_buckets.setdefault(packet.source, {})
            for channel, value in protocol.iter_spectrum_points(packet, self.args.max_spectrum_channels):
                energy = calibration.energy_kiloelectronvolts(packet.source, channel)
                energy_electronvolts = round(energy * 1000)
                buckets[energy_electronvolts] = buckets.get(energy_electronvolts, 0) + value

        for source_id, bucket_counts in source_buckets.items():
            cumulative = 0
            buckets: list[tuple[str, float]] = []
            weighted_sum = 0.0
            for energy_electronvolts, count in sorted(bucket_counts.items()):
                cumulative += count
                weighted_sum += energy_electronvolts * count
                buckets.append((str(energy_electronvolts), float(cumulative)))
            buckets.append(("+Inf", float(cumulative)))
            spectrum.add_metric([protocol.sensor_name(source_id)], buckets, weighted_sum)
        yield spectrum

    def _next_sync(self) -> int:
        self._sync = (self._sync + 1) & 0xFF
        return self._sync


class KC761xBleClient:
    def __init__(
        self,
        address: str | None,
        name: str | None,
        discovery_timeout: float,
        command_timeout: float,
        spectrum_idle_timeout: float,
        mtu: int | None,
        reconnect_interval: float,
        next_sync: Callable[[], int],
    ) -> None:
        self.address = address
        self.name = name
        self.discovery_timeout = discovery_timeout
        self.command_timeout = command_timeout
        self.spectrum_idle_timeout = spectrum_idle_timeout
        self.mtu = mtu
        self.reconnect_interval = reconnect_interval
        self.next_sync = next_sync
        self.decode_errors = 0
        self._client: BleakClient | None = None
        self._resolved_address: str | None = address
        self._queue: asyncio.Queue[protocol.Packet] = asyncio.Queue()
        self._closing = False

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def run_connection_loop(self) -> None:
        while not self._closing:
            if not self.is_connected:
                try:
                    await self._connect()
                except Exception as exc:
                    LOG.warning("KC761x BLE connection attempt failed: %s", exc)
                    await self._disconnect()
            await asyncio.sleep(self.reconnect_interval)

    async def scrape(self, enable_spectrum: bool, spectrum_sources: Sequence[int]) -> ScrapeResult:
        result = ScrapeResult()
        self.decode_errors = 0
        if self._client is None:
            raise RuntimeError("KC761x BLE device is not connected")

        try:
            self._drain_queue()
            status_sync = self.next_sync()
            await self._write(self._client, protocol.command_get_status(status_sync))
            result.status = await self._wait_for(
                lambda packet: isinstance(packet, protocol.StatusPacket) and packet.sync == status_sync and not packet.auto,
                self.command_timeout,
            )

            info_sync = self.next_sync()
            await self._write(self._client, protocol.command_get_device_info(info_sync))
            result.device_info = await self._wait_for(
                lambda packet: isinstance(packet, protocol.DeviceInfoPacket) and packet.sync == info_sync,
                self.command_timeout,
            )

            if enable_spectrum:
                calibration_sync = self.next_sync()
                await self._write(self._client, protocol.command_get_calibration(calibration_sync))
                result.calibration = await self._wait_for(
                    lambda packet: isinstance(packet, protocol.CalibrationPacket) and packet.sync == calibration_sync,
                    self.command_timeout,
                )

                for source in spectrum_sources:
                    spectrum_sync = self.next_sync()
                    await self._write(self._client, protocol.command_get_spectrum(spectrum_sync, source))
                    result.spectra.extend(await self._collect_spectrum(spectrum_sync, source))
        except Exception:
            await self._disconnect()
            raise

        result.decode_errors = self.decode_errors
        return result

    async def close(self) -> None:
        self._closing = True
        await self._disconnect()

    async def _connect(self) -> None:
        await self._disconnect()
        address = self._resolved_address or await self._discover_address()
        LOG.info("connecting to %s", address)
        client = BleakClient(address)
        await client.connect()
        self._client = client
        self._resolved_address = address
        if self.mtu:
            await self._request_mtu(client)
        await client.start_notify(protocol.TX_CHAR_UUID, self._on_notify)

    async def _disconnect(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            if client.is_connected:
                await client.stop_notify(protocol.TX_CHAR_UUID)
                await client.disconnect()
        except Exception:
            LOG.exception("failed to disconnect KC761x BLE client")

    async def _discover_address(self) -> str:
        if not self.name:
            raise RuntimeError("either address or name is required")
        LOG.info("scanning for BLE device with name containing %r", self.name)
        devices = await BleakScanner.discover(timeout=self.discovery_timeout)
        for device in devices:
            if device.name and self.name.lower() in device.name.lower():
                LOG.info("found %s at %s", device.name, device.address)
                return device.address
        raise RuntimeError(f"no BLE device found with name containing {self.name!r}")

    async def _request_mtu(self, client: BleakClient) -> None:
        request = getattr(client, "request_mtu", None)
        if request is None:
            LOG.info("BLE backend does not expose request_mtu(); continuing with default MTU")
            return
        negotiated = await request(self.mtu)
        LOG.debug("requested MTU %s, negotiated %s", self.mtu, negotiated)

    async def _write(self, client: BleakClient, payload: bytes) -> None:
        await client.write_gatt_char(protocol.RX_CHAR_UUID, payload, response=True)

    async def _wait_for(self, predicate: Callable[[protocol.Packet], bool], timeout: float) -> protocol.Packet:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for KC761x response")
            packet = await asyncio.wait_for(self._queue.get(), timeout=remaining)
            if predicate(packet):
                return packet

    def _drain_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def _collect_spectrum(self, sync: int, source: int) -> list[protocol.SpectrumPacket]:
        packets: list[protocol.SpectrumPacket] = []
        deadline = time.monotonic() + self.command_timeout
        while True:
            timeout = self.spectrum_idle_timeout if packets else max(0.0, deadline - time.monotonic())
            if timeout <= 0:
                if packets:
                    return packets
                raise TimeoutError("timed out waiting for KC761x spectrum response")
            try:
                packet = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            except TimeoutError:
                return packets
            if isinstance(packet, protocol.SpectrumPacket) and packet.sync == sync and packet.source == source and not packet.auto:
                packets.append(packet)

    def _on_notify(self, _sender: object, data: bytearray) -> None:
        try:
            for packet in protocol.parse_packets(bytes(data)):
                self._queue.put_nowait(packet)
        except Exception:
            self.decode_errors += 1
            LOG.exception("failed to decode notification: %s", bytes(data).hex(" "))


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    configure_logging(args.verbose)

    collector = KC761xCollector(args)
    REGISTRY.register(collector)

    host, port = parse_listen(args.listen)
    start_http_server(port, addr=host)
    LOG.info("metrics listening on http://%s:%d/metrics", host, port)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda _signum, _frame: stop.set())
    signal.signal(signal.SIGTERM, lambda _signum, _frame: stop.set())
    try:
        stop.wait()
    finally:
        collector.close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prometheus exporter for KC761x radiation meters over BLE")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--address", help="BLE address of the KC761x")
    target.add_argument("--name", help="Substring of advertised BLE device name to discover")
    parser.add_argument("--listen", default="0.0.0.0:9108", help="Metrics listen address, default: 0.0.0.0:9108")
    parser.add_argument("--scrape-timeout", type=float, default=9.0, help="Exporter-side maximum seconds for a scrape")
    parser.add_argument("--command-timeout", type=float, default=8.0, help="Seconds to wait for each KC761x command response")
    parser.add_argument("--discovery-timeout", type=float, default=5.0, help="BLE discovery timeout in seconds")
    parser.add_argument("--reconnect-interval", type=float, default=5.0, help="Seconds between background BLE reconnect attempts")
    parser.add_argument("--mtu", type=int, default=517, help="Requested BLE MTU, if backend supports it")
    parser.add_argument("--enable-spectrum", action="store_true", help="Expose calibrated spectrum energy gauges; disabled by default")
    parser.add_argument("--max-spectrum-channels", type=int, default=2048, help="Maximum spectrum channels to expose")
    parser.add_argument(
        "--spectrum-idle-timeout",
        type=float,
        default=0.5,
        help="When spectrum is enabled, stop waiting after this many seconds without another spectrum packet",
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
    if args.enable_spectrum and not args.spectrum_source:
        args.spectrum_source = [0]
    return args


def parse_listen(value: str) -> tuple[str, int]:
    if ":" not in value:
        return "0.0.0.0", int(value)
    host, port = value.rsplit(":", 1)
    return host, int(port)


def _add_nonnegative(metric: Metric, labels: list[str], value: float) -> None:
    if value >= 0:
        metric.add_metric(labels, value)


def configure_logging(verbose: int) -> None:
    level = logging.WARNING
    if verbose == 1:
        level = logging.INFO
    elif verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


if __name__ == "__main__":
    main()
