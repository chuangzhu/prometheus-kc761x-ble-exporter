import struct

from kc761x_exporter import protocol


def test_parse_status_packet() -> None:
    data = bytearray(81)
    struct.pack_into("<BBH", data, 0, 0x12, protocol.FLAG_STATUS_RESPONSE, 81)
    struct.pack_into("<BBBBB", data, 4, 0b00000101, 0, 0, 1, 87)
    struct.pack_into("<HhI", data, 9, 1013, 234, 1_735_689_600)
    offset = 33
    struct.pack_into("<ieefee", data, offset, 42, 0.125, 0.25, 43.5, 0.5, 1.0)
    struct.pack_into("<ieefee", data, offset + 16, -1, -1.0, -1.0, -1.0, -1.0, -1.0)
    struct.pack_into("<ieefee", data, offset + 32, 7, 2.0, 3.0, 8.5, 4.0, 5.0)

    packet = protocol.parse_status_packet(bytes(data))

    assert packet.sync == 0x12
    assert packet.battery_percent == 87
    assert packet.air_pressure_hpa == 1013
    assert packet.device_temperature_c == 23.4
    assert packet.selected_sensor == 1
    assert packet.sensor_accumulating(0) is True
    assert packet.sensors[0].raw_cps == 42
    assert packet.sensors[0].avg_cps == 43.5


def test_parse_device_info_packet() -> None:
    data = bytearray(100)
    struct.pack_into("<BBH", data, 0, 0x34, protocol.FLAG_DEVICE_INFO, 100)
    struct.pack_into("<BBBBBBB", data, 4, 12, 12, 180, 101, 2, 8, 5)
    data[36:52] = b"7601-0000-000001"
    struct.pack_into("<IIff", data, 52, 10, 20, 1.5, 2.5)
    struct.pack_into("<IIff", data, 68, 30, 40, 3.5, 4.5)
    struct.pack_into("<IIff", data, 84, 50, 60, 5.5, 6.5)

    packet = protocol.parse_device_info_packet(bytes(data))

    assert packet.device_model == 12
    assert packet.hardware_version == 1.2
    assert packet.firmware_version == 1.8
    assert packet.device_id == "7601-0000-000001"
    assert packet.sensor_types == (2, 8, 5)
    assert packet.multichannel_runtime_seconds == (10, 30, 50)
    assert packet.dose_equivalent_microsieverts_total == (2.5, 4.5, 6.5)


def test_parse_spectrum_and_stream_packets() -> None:
    spectrum = struct.pack(
        "<BBHBHHHHH",
        1,
        protocol.FLAG_MC_RESPONSE,
        15,
        0,
        10,
        2,
        3,
        4,
        0xFFFF,
    )
    stream = struct.pack("<BBHHH", 2, protocol.FLAG_STREAM, 8, 0x000A, 0x400B)

    packets = protocol.parse_packets(spectrum + stream)

    assert isinstance(packets[0], protocol.SpectrumPacket)
    assert packets[0].offset == 10
    assert packets[0].counts == (6, 8, 0xFFFF)
    assert isinstance(packets[1], protocol.StreamPacket)
    assert packets[1].pulses == ((0, 10), (1, 11))


def test_parse_calibration_packet_honors_rad0_energy_selection() -> None:
    data = bytearray(150)
    struct.pack_into("<BBH", data, 0, 0x56, protocol.FLAG_CAL_DATA, 150)
    struct.pack_into("<BB", data, 4, 2, 1)
    struct.pack_into("<ffffff", data, 6, 2.0, 10.0, 1.0, 0.0, 1.0, 0.0)
    struct.pack_into("<ffff", data, 50, 0.0, 0.0, 3.0, 0.0)
    struct.pack_into("<ffff", data, 66, 0.0, 0.0, 4.0, 0.0)
    struct.pack_into("<ffff", data, 82, 0.0, 0.0, 5.0, 0.0)
    struct.pack_into("<ffff", data, 98, 0.0, 0.0, 10.0, 0.0)
    struct.pack_into("<ffff", data, 114, 0.0, 0.0, 20.0, 0.0)
    struct.pack_into("<ffff", data, 130, 0.0, 0.0, 30.0, 0.0)
    struct.pack_into("<HH", data, 146, 100, 200)

    packet = protocol.parse_calibration_packet(bytes(data))

    assert packet.rad0_energy_calibration_select == 1
    assert packet.energy_kiloelectronvolts(0, 7) == 52.0
    assert packet.energy_kiloelectronvolts(1, 7) == 28.0


def test_parse_calibration_packet_uses_rad0_factory_segments() -> None:
    data = bytearray(150)
    struct.pack_into("<BBH", data, 0, 0x57, protocol.FLAG_CAL_DATA, 150)
    struct.pack_into("<BB", data, 4, 2, 0)
    struct.pack_into("<ffffff", data, 6, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0)
    struct.pack_into("<ffff", data, 98, 0.0, 0.0, 10.0, 0.0)
    struct.pack_into("<ffff", data, 114, 0.0, 0.0, 20.0, 0.0)
    struct.pack_into("<ffff", data, 130, 0.0, 0.0, 30.0, 0.0)
    struct.pack_into("<HH", data, 146, 100, 200)

    packet = protocol.parse_calibration_packet(bytes(data))

    assert packet.energy_kiloelectronvolts(0, 99) == 990.0
    assert packet.energy_kiloelectronvolts(0, 100) == 2000.0
    assert packet.energy_kiloelectronvolts(0, 201) == 6030.0


def test_commands() -> None:
    assert protocol.command_get_status(0x44) == bytes.fromhex("00 53 44 00")
    assert protocol.command_get_device_info(0x45) == bytes.fromhex("00 54 45 00")
    assert protocol.command_get_calibration(0x46) == bytes.fromhex("00 55 46 00")
    assert protocol.command_set_auto_upload(0x46, True) == bytes.fromhex("00 62 46 ff ff ff 01 00")
    assert protocol.command_get_spectrum(0x47, 2) == bytes.fromhex("00 52 47 02 00")
