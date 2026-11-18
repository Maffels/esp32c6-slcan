"""No hardware: slcan-monitor 500/250 kbit/s mode parser."""

from slcan_monitor_gui import (
    ADAPTERS,
    BITRATE_250K,
    BITRATE_500K,
    BITRATE_MODE_LABELS,
    BITRATE_MODES,
    parse_bitrate_mode,
    parse_channel,
    parse_ixxat_hwid,
)


def test_parse_bitrate_mode():
    assert BITRATE_500K == 500000
    assert BITRATE_250K == 250000
    assert BITRATE_MODES == (BITRATE_500K, BITRATE_250K)
    assert parse_bitrate_mode("500000") == BITRATE_500K
    assert parse_bitrate_mode("250000") == BITRATE_250K
    assert parse_bitrate_mode(BITRATE_MODE_LABELS[BITRATE_250K]) == BITRATE_250K
    assert parse_bitrate_mode("250 kbit/s") == BITRATE_250K
    try:
        parse_bitrate_mode("125000")
        raise AssertionError("125000 must be rejected")
    except ValueError:
        pass


def test_ixxat_adapter_option():
    assert "ixxat" in ADAPTERS
    assert parse_channel("ixxat", "0") == 0
    assert parse_channel("ixxat", "1") == 1
    assert parse_channel("ixxat", "0:HW441489") == 0
    assert parse_ixxat_hwid("0") is None
    assert parse_ixxat_hwid("0:HW441489") == "HW441489"
    assert parse_channel("kvaser", "0") == 0
    assert parse_channel("slcan", "COM8") == "COM8"
