"""Pure, non-I/O helpers for the PowerSpec 1520 Clevo keyboard protocol.

This module deliberately has no device-opening or HID-writing capability.  It
only validates values and encodes the single lighting command that has been
independently associated with VID 048D / PID 8910 / usage FF89:00CC.
"""

from __future__ import annotations

from collections.abc import Sequence


REPORT_ID = 0xCC
SET_KEY_COLOR_COMMAND = 0x01
FEATURE_REPORT_LENGTH = 7
REPORT_TRAILER = 0x00
MATRIX_ROWS = 6
MATRIX_COLUMNS = 20

# Values documented by the merged OpenRGB ITE 829x keyboard map.  Keeping the
# first probe to one ordinary key avoids a full-keyboard write during protocol
# qualification.
QUALIFICATION_KEY_IDS = {
    "A": 0x62,
}


class ClevoHidProtocolError(ValueError):
    """Raised when a value cannot be represented by the narrow protocol."""


def _byte(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ClevoHidProtocolError(f"{name} must be an integer byte")
    if not 0 <= value <= 0xFF:
        raise ClevoHidProtocolError(f"{name} must be between 0 and 255")
    return value


def encode_key_id(row: int, column: int) -> int:
    """Encode the controller's six-row by twenty-column LED matrix address."""

    if isinstance(row, bool) or not isinstance(row, int) or not 0 <= row < MATRIX_ROWS:
        raise ClevoHidProtocolError(f"row must be between 0 and {MATRIX_ROWS - 1}")
    if (
        isinstance(column, bool)
        or not isinstance(column, int)
        or not 0 <= column < MATRIX_COLUMNS
    ):
        raise ClevoHidProtocolError(
            f"column must be between 0 and {MATRIX_COLUMNS - 1}"
        )
    return ((row & 0x07) << 5) | (column & 0x1F)


def physical_rgb_to_wire(rgb: Sequence[int]) -> tuple[int, int, int]:
    """Compensate for this laptop's observed physical red/green channel swap."""

    if len(rgb) != 3:
        raise ClevoHidProtocolError("rgb must contain exactly three components")
    red, green, blue = (_byte(value, "rgb component") for value in rgb)
    return green, red, blue


def encode_static_key_color(key_id: int, physical_rgb: Sequence[int]) -> bytes:
    """Build one inferred SetFeature frame without opening or writing a device.

    The final zero is intentional.  Windows reports a seven-byte feature-report
    length for this laptop.  HIDAPI pads OpenRGB's six-byte logical command to
    that length with zero before calling HidD_SetFeature.
    """

    key_id = _byte(key_id, "key_id")
    wire_red, wire_green, wire_blue = physical_rgb_to_wire(physical_rgb)
    report = bytes(
        (
            REPORT_ID,
            SET_KEY_COLOR_COMMAND,
            key_id,
            wire_red,
            wire_green,
            wire_blue,
            REPORT_TRAILER,
        )
    )
    if len(report) != FEATURE_REPORT_LENGTH:  # Defensive invariant.
        raise AssertionError("Clevo feature report length changed unexpectedly")
    return report


def format_report(report: bytes) -> str:
    """Return a human-checkable hexadecimal representation of a dry-run frame."""

    if len(report) != FEATURE_REPORT_LENGTH:
        raise ClevoHidProtocolError(
            f"report must be exactly {FEATURE_REPORT_LENGTH} bytes"
        )
    return " ".join(f"{value:02X}" for value in report)


if __name__ == "__main__":
    # Dry-run example only: ordinary A key, desired physical blue.  There is no
    # HID import, device handle, or write operation anywhere in this module.
    print(
        format_report(
            encode_static_key_color(QUALIFICATION_KEY_IDS["A"], (0, 0, 255))
        )
    )
