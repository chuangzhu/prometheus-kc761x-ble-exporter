from types import SimpleNamespace

from kc761x_exporter import protocol
from kc761x_exporter.exporter import KC761xCollector


def test_spectrum_metrics_emit_electronvolt_histogram() -> None:
    collector = KC761xCollector.__new__(KC761xCollector)
    collector.args = SimpleNamespace(spectrum_channels=2)
    calibration = protocol.CalibrationPacket(
        sync=1,
        packet_len=150,
        factory_calibration_version=0,
        rad0_energy_calibration_select=0,
        energy_zoom=(1.0, 1.0, 1.0),
        energy_offset_kiloelectronvolts=(0.0, 0.0, 0.0),
        rad0_custom_energy_calibration=(0.0, 0.0, 1.0, 0.0),
        rad1_factory_energy_calibration=(0.0, 0.0, 1.0, 0.0),
        rad2_factory_energy_calibration=(0.0, 0.0, 1.0, 0.0),
        rad0_factory_energy_calibration_low=(0.0, 0.0, 1.0, 0.0),
        rad0_factory_energy_calibration_mid=(0.0, 0.0, 1.0, 0.0),
        rad0_factory_energy_calibration_high=(0.0, 0.0, 1.0, 0.0),
        rad0_factory_energy_calibration_node1=0,
        rad0_factory_energy_calibration_node2=0,
    )
    spectrum = protocol.SpectrumPacket(
        sync=1,
        auto=False,
        packet_len=13,
        source=0,
        offset=0,
        ratio=1,
        counts=(2, 3),
    )

    metric = list(collector._spectrum_metrics([spectrum], calibration))[0]
    samples = {(sample.name, sample.labels.get("le")): sample.value for sample in metric.samples}

    assert samples[("kc761x_spectrum_electronvolts_bucket", "0")] == 2.0
    assert samples[("kc761x_spectrum_electronvolts_bucket", "1000")] == 5.0
    assert samples[("kc761x_spectrum_electronvolts_bucket", "+Inf")] == 5.0
    assert samples[("kc761x_spectrum_electronvolts_count", None)] == 5.0
    assert samples[("kc761x_spectrum_electronvolts_sum", None)] == 3000.0


def test_spectrum_metrics_omits_incomplete_spectrum() -> None:
    collector = KC761xCollector.__new__(KC761xCollector)
    collector.args = SimpleNamespace(spectrum_channels=3)
    calibration = protocol.CalibrationPacket(
        sync=1,
        packet_len=150,
        factory_calibration_version=0,
        rad0_energy_calibration_select=0,
        energy_zoom=(1.0, 1.0, 1.0),
        energy_offset_kiloelectronvolts=(0.0, 0.0, 0.0),
        rad0_custom_energy_calibration=(0.0, 0.0, 1.0, 0.0),
        rad1_factory_energy_calibration=(0.0, 0.0, 1.0, 0.0),
        rad2_factory_energy_calibration=(0.0, 0.0, 1.0, 0.0),
        rad0_factory_energy_calibration_low=(0.0, 0.0, 1.0, 0.0),
        rad0_factory_energy_calibration_mid=(0.0, 0.0, 1.0, 0.0),
        rad0_factory_energy_calibration_high=(0.0, 0.0, 1.0, 0.0),
        rad0_factory_energy_calibration_node1=0,
        rad0_factory_energy_calibration_node2=0,
    )
    spectrum = protocol.SpectrumPacket(
        sync=1,
        auto=False,
        packet_len=13,
        source=0,
        offset=0,
        ratio=1,
        counts=(2, 3),
    )

    metric = list(collector._spectrum_metrics([spectrum], calibration))[0]

    assert metric.samples == []
