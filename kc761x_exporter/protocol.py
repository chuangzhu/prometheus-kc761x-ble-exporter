from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Iterable


RX_CHAR_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
TX_CHAR_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"

CMD_GET_MC_DATA = 0x52
CMD_GET_STATUS = 0x53
CMD_GET_DEVICE_INFO = 0x54
CMD_SET_STATUS = 0x62

FLAG_MC_RESPONSE = 0xA0
FLAG_MC_AUTO = 0xA1
FLAG_STATUS_RESPONSE = 0xA2
FLAG_STATUS_AUTO = 0xA3
FLAG_STREAM = 0xA4
FLAG_DEVICE_INFO = 0xA5
FLAG_ACK = 0xAA

SENSOR_NAMES = {
    0: "gamma",
    1: "neutron",
    2: "pin",
}

DEVICE_MODEL_NAMES = {
    10: "KC761 beta",
    11: "KC761",
    12: "KC761A/B",
    13: "KC761C",
    14: "KC761CN",
}

SENSOR_TYPE_NAMES = {
    0x00: "none",
    0x01: "KC7601.21 CsI",
    0x02: "KC7601.24 CsI",
    0x03: "KC7601.25 CsI",
    0x04: "KC7601.26 CsI",
    0x05: "PIN",
    0x06: "PIN",
    0x07: "PIN",
    0x08: "KC7601.31 6Li",
}


@dataclass(frozen=True)
class SensorStatus:
    raw_cps: int
    raw_dose_rate_mgy_h: float
    raw_dose_equiv_rate_msv_h: float
    avg_cps: float
    avg_dose_rate_mgy_h: float
    avg_dose_equiv_rate_msv_h: float


@dataclass(frozen=True)
class StatusPacket:
    sync: int
    auto: bool
    packet_len: int
    radiation_sensor_status: int
    volume_status: int
    led_screen_status: int
    auto_upload_status: int
    battery_percent: int
    air_pressure_hpa: int
    device_temperature_c: float
    device_time_seconds: int
    sensors: tuple[SensorStatus, SensorStatus, SensorStatus]

    @property
    def selected_sensor(self) -> int:
        return self.radiation_sensor_status & 0x03

    def sensor_accumulating(self, slot: int) -> bool:
        return bool(self.radiation_sensor_status & (1 << (slot + 2)))


@dataclass(frozen=True)
class SpectrumPacket:
    sync: int
    auto: bool
    packet_len: int
    source: int
    offset: int
    ratio: int
    counts: tuple[int, ...]


@dataclass(frozen=True)
class StreamPacket:
    sync: int
    packet_len: int
    pulses: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class DeviceInfoPacket:
    sync: int
    packet_len: int
    device_model: int
    hardware_version: float
    firmware_version: float
    coprocessor_firmware_version: float
    sensor_types: tuple[int, int, int]
    device_id: str
    multichannel_runtime_seconds: tuple[int, int, int]
    dose_runtime_seconds: tuple[int, int, int]
    accumulated_dose_ugy: tuple[float, float, float]
    accumulated_dose_equiv_usv: tuple[float, float, float]


@dataclass(frozen=True)
class AckPacket:
    sync: int
    packet_len: int
    ok: bool
    command: int


Packet = StatusPacket | SpectrumPacket | StreamPacket | DeviceInfoPacket | AckPacket


def command_get_status(sync: int) -> bytes:
    return bytes((0x00, CMD_GET_STATUS, sync & 0xFF, 0x00))


def command_get_device_info(sync: int) -> bytes:
    return bytes((0x00, CMD_GET_DEVICE_INFO, sync & 0xFF, 0x00))


def command_set_auto_upload(sync: int, enabled: bool) -> bytes:
    return bytes((0x00, CMD_SET_STATUS, sync & 0xFF, 0xFF, 0xFF, 0xFF, int(enabled), 0x00))


def command_get_spectrum(sync: int, source: int) -> bytes:
    return bytes((0x00, CMD_GET_MC_DATA, sync & 0xFF, source & 0xFF, 0x00))


def parse_packets(data: bytes) -> list[Packet]:
    packets: list[Packet] = []
    pos = 0
    while pos < len(data):
        if len(data) - pos < 4:
            raise ValueError(f"trailing short packet: {len(data) - pos} bytes")

        flag = data[pos + 1]
        if flag in (FLAG_STATUS_RESPONSE, FLAG_STATUS_AUTO):
            packet = parse_status_packet(data[pos : pos + 81])
            pos += packet.packet_len
        elif flag in (FLAG_MC_RESPONSE, FLAG_MC_AUTO):
            packet_len = _u16(data, pos + 2)
            packet = parse_spectrum_packet(data[pos : pos + packet_len])
            pos += packet.packet_len
        elif flag == FLAG_STREAM:
            packet_len = _u16(data, pos + 2)
            packet = parse_stream_packet(data[pos : pos + packet_len])
            pos += packet.packet_len
        elif flag == FLAG_DEVICE_INFO:
            packet = parse_device_info_packet(data[pos : pos + 100])
            pos += packet.packet_len
        elif flag == FLAG_ACK:
            packet = parse_ack_packet(data[pos : pos + 6])
            pos += packet.packet_len
        else:
            raise ValueError(f"unknown KC761x packet flag 0x{flag:02x} at offset {pos}")
        packets.append(packet)

    return packets


def parse_status_packet(data: bytes) -> StatusPacket:
    _require_len(data, 81, "status")
    sync, flag, packet_len = struct.unpack_from("<BBH", data, 0)
    if flag not in (FLAG_STATUS_RESPONSE, FLAG_STATUS_AUTO):
        raise ValueError(f"not a status packet: 0x{flag:02x}")
    if packet_len != 81:
        raise ValueError(f"unexpected status packet length {packet_len}")

    offset = 4
    rad_status, volume_status, led_status, auto_upload, battery = struct.unpack_from("<BBBBB", data, offset)
    offset += 5
    air_pressure, temp_deci_c, device_time = struct.unpack_from("<HhI", data, offset)
    offset += 8 + 16

    sensors = []
    for _slot in range(3):
        raw_cps, raw_dose, raw_dose_eq, avg_cps, avg_dose, avg_dose_eq = struct.unpack_from("<ieefee", data, offset)
        offset += 16
        sensors.append(
            SensorStatus(
                raw_cps=raw_cps,
                raw_dose_rate_mgy_h=float(raw_dose),
                raw_dose_equiv_rate_msv_h=float(raw_dose_eq),
                avg_cps=float(avg_cps),
                avg_dose_rate_mgy_h=float(avg_dose),
                avg_dose_equiv_rate_msv_h=float(avg_dose_eq),
            )
        )

    return StatusPacket(
        sync=sync,
        auto=flag == FLAG_STATUS_AUTO,
        packet_len=packet_len,
        radiation_sensor_status=rad_status,
        volume_status=volume_status,
        led_screen_status=led_status,
        auto_upload_status=auto_upload,
        battery_percent=battery,
        air_pressure_hpa=air_pressure,
        device_temperature_c=temp_deci_c / 10.0,
        device_time_seconds=device_time,
        sensors=tuple(sensors),  # type: ignore[arg-type]
    )


def parse_spectrum_packet(data: bytes) -> SpectrumPacket:
    _require_len(data, 9, "spectrum")
    sync, flag, packet_len, source, offset, ratio = struct.unpack_from("<BBHBHH", data, 0)
    if flag not in (FLAG_MC_RESPONSE, FLAG_MC_AUTO):
        raise ValueError(f"not a spectrum packet: 0x{flag:02x}")
    _require_len(data, packet_len, "spectrum")
    payload_len = packet_len - 9
    if payload_len % 2:
        raise ValueError(f"spectrum payload is not uint16 aligned: {payload_len} bytes")
    values = struct.unpack_from(f"<{payload_len // 2}H", data, 9)
    counts = tuple(0xFFFF if value == 0xFFFF else value * max(ratio, 1) for value in values)
    return SpectrumPacket(
        sync=sync,
        auto=flag == FLAG_MC_AUTO,
        packet_len=packet_len,
        source=source,
        offset=offset,
        ratio=ratio,
        counts=counts,
    )


def parse_stream_packet(data: bytes) -> StreamPacket:
    _require_len(data, 4, "stream")
    sync, flag, packet_len = struct.unpack_from("<BBH", data, 0)
    if flag != FLAG_STREAM:
        raise ValueError(f"not a stream packet: 0x{flag:02x}")
    _require_len(data, packet_len, "stream")
    payload_len = packet_len - 4
    if payload_len % 2:
        raise ValueError(f"stream payload is not uint16 aligned: {payload_len} bytes")
    raw_pulses = struct.unpack_from(f"<{payload_len // 2}H", data, 4)
    pulses = tuple(((pulse >> 14) & 0x03, pulse & 0x3FFF) for pulse in raw_pulses)
    return StreamPacket(sync=sync, packet_len=packet_len, pulses=pulses)


def parse_device_info_packet(data: bytes) -> DeviceInfoPacket:
    _require_len(data, 100, "device info")
    sync, flag, packet_len = struct.unpack_from("<BBH", data, 0)
    if flag != FLAG_DEVICE_INFO:
        raise ValueError(f"not a device info packet: 0x{flag:02x}")
    if packet_len != 100:
        raise ValueError(f"unexpected device info packet length {packet_len}")

    device_model, hw_ver, fw_ver, co_fw_ver, rad0_type, rad1_type, rad2_type = struct.unpack_from("<BBBBBBB", data, 4)
    raw_device_id = data[36:52]
    device_id = raw_device_id.split(b"\x00", 1)[0].decode("ascii", errors="replace")

    offset = 52
    mc_runtime = []
    dose_runtime = []
    sum_dose = []
    sum_dose_eq = []
    for _slot in range(3):
        mc_s, dose_s, dose, dose_eq = struct.unpack_from("<IIff", data, offset)
        offset += 16
        mc_runtime.append(mc_s)
        dose_runtime.append(dose_s)
        sum_dose.append(float(dose))
        sum_dose_eq.append(float(dose_eq))

    return DeviceInfoPacket(
        sync=sync,
        packet_len=packet_len,
        device_model=device_model,
        hardware_version=hw_ver / 10.0,
        firmware_version=fw_ver / 100.0,
        coprocessor_firmware_version=co_fw_ver / 100.0,
        sensor_types=(rad0_type, rad1_type, rad2_type),
        device_id=device_id,
        multichannel_runtime_seconds=tuple(mc_runtime),  # type: ignore[arg-type]
        dose_runtime_seconds=tuple(dose_runtime),  # type: ignore[arg-type]
        accumulated_dose_ugy=tuple(sum_dose),  # type: ignore[arg-type]
        accumulated_dose_equiv_usv=tuple(sum_dose_eq),  # type: ignore[arg-type]
    )


def parse_ack_packet(data: bytes) -> AckPacket:
    _require_len(data, 6, "ack")
    sync, flag, packet_len, status, command = struct.unpack_from("<BBHBB", data, 0)
    if flag != FLAG_ACK:
        raise ValueError(f"not an ack packet: 0x{flag:02x}")
    if packet_len != 6:
        raise ValueError(f"unexpected ack packet length {packet_len}")
    return AckPacket(sync=sync, packet_len=packet_len, ok=status == 0, command=command)


def sensor_name(slot: int) -> str:
    return SENSOR_NAMES.get(slot, f"slot{slot}")


def sensor_type_name(sensor_type: int) -> str:
    return SENSOR_TYPE_NAMES.get(sensor_type, f"unknown_0x{sensor_type:02x}")


def device_model_name(device_model: int) -> str:
    return DEVICE_MODEL_NAMES.get(device_model, f"unknown_{device_model}")


def iter_spectrum_points(packet: SpectrumPacket, max_channel: int | None = None) -> Iterable[tuple[int, int]]:
    for index, value in enumerate(packet.counts):
        channel = packet.offset + index
        if max_channel is not None and channel >= max_channel:
            break
        if value == 0xFFFF:
            continue
        yield channel, value


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _require_len(data: bytes, expected: int, name: str) -> None:
    if len(data) < expected:
        raise ValueError(f"{name} packet too short: got {len(data)}, need {expected}")

