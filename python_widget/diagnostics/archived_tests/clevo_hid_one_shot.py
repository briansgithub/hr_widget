"""Manually gated one-key HID qualification probe for this PowerSpec 1520.

This is intentionally not a general keyboard controller.  The only supported
write is one physical-blue request to the ordinary A key.  It never changes
brightness, effects, firmware state, Clevo processes, services, or packages.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict

from clevo_hid_probe import (
    probe_clevo_hid_identity,
    probe_clevo_hid_report_capabilities,
)
from clevo_hid_protocol import (
    MATRIX_COLUMNS,
    MATRIX_ROWS,
    QUALIFICATION_KEY_IDS,
    encode_key_id,
    encode_static_key_color,
    format_report,
)


QUALIFICATION_REPORT = encode_static_key_color(
    QUALIFICATION_KEY_IDS["A"], (0, 0, 255)
)
QUALIFICATION_REPORT_HEX = "CC 01 62 00 00 FF 00"
WORKER_TIMEOUT_SECONDS = 2.0
MATRIX_WORKER_TIMEOUT_SECONDS = 5.0
MATRIX_REPORT_DELAY_SECONDS = 0.002
SUCCESS_MARKER = "QUALIFIED_SET_FEATURE_SUBMITTED"
MATRIX_SUCCESS_MARKER = "QUALIFIED_WHITE_MATRIX_SUBMITTED"
READ_SUCCESS_MARKER = "QUALIFIED_FEATURE_READ"


WHITE_MATRIX_REPORTS = tuple(
    encode_static_key_color(encode_key_id(row, column), (255, 255, 255))
    for row in range(MATRIX_ROWS)
    for column in range(MATRIX_COLUMNS)
)


def _qualified_interface() -> tuple[str | None, dict[str, object]]:
    identity = probe_clevo_hid_identity()
    capabilities = probe_clevo_hid_report_capabilities(identity)
    details: dict[str, object] = {
        "identity": asdict(identity),
        "capabilities": asdict(capabilities),
        "report": format_report(QUALIFICATION_REPORT),
        "hardware_write_attempted": False,
    }
    if not identity.identity_matched:
        details["blocked_reason"] = identity.reason
        return None, details
    if not capabilities.descriptor_matched:
        details["blocked_reason"] = capabilities.reason
        return None, details
    if capabilities.feature_report_bytes != len(QUALIFICATION_REPORT):
        details["blocked_reason"] = "The live descriptor is not exactly seven bytes."
        return None, details
    if not capabilities.interface_path:
        details["blocked_reason"] = "The exact COL02 interface path is unavailable."
        return None, details
    if format_report(QUALIFICATION_REPORT) != QUALIFICATION_REPORT_HEX:
        details["blocked_reason"] = "The compiled qualification payload changed."
        return None, details
    return capabilities.interface_path, details


def _set_exact_feature_reports(interface_path: str, reports: tuple[bytes, ...]) -> None:
    if os.name != "nt":
        raise RuntimeError("The qualification probe is Windows-only")
    lowered = interface_path.casefold()
    if "vid_048d&pid_8910&col02" not in lowered:
        raise RuntimeError("Worker rejected a non-allowlisted HID path")
    allowed_single = reports == (QUALIFICATION_REPORT,)
    allowed_matrix = reports == WHITE_MATRIX_REPORTS and len(reports) == 120
    if not allowed_single and not allowed_matrix:
        raise RuntimeError("Worker rejected a payload outside the qualification allowlist")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    hid = ctypes.WinDLL("hid", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    hid.HidD_SetFeature.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    hid.HidD_SetFeature.restype = ctypes.c_bool

    generic_write = 0x40000000
    file_share_read_write = 0x00000001 | 0x00000002
    open_existing = 3
    handle = kernel32.CreateFileW(
        interface_path,
        generic_write,
        file_share_read_write,
        None,
        open_existing,
        0,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        for index, report in enumerate(reports):
            report_buffer = (ctypes.c_ubyte * len(report)).from_buffer_copy(report)
            if not hid.HidD_SetFeature(
                handle, ctypes.byref(report_buffer), len(report)
            ):
                raise RuntimeError(
                    f"HidD_SetFeature failed at report {index}: "
                    f"{ctypes.WinError(ctypes.get_last_error())}"
                )
            if len(reports) > 1:
                time.sleep(MATRIX_REPORT_DELAY_SECONDS)
    finally:
        kernel32.CloseHandle(handle)


def _get_feature_report(interface_path: str) -> bytes:
    """Request report 0xCC without changing device state."""

    if os.name != "nt":
        raise RuntimeError("The qualification probe is Windows-only")
    if "vid_048d&pid_8910&col02" not in interface_path.casefold():
        raise RuntimeError("Worker rejected a non-allowlisted HID path")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    hid = ctypes.WinDLL("hid", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    hid.HidD_GetFeature.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    hid.HidD_GetFeature.restype = ctypes.c_bool

    generic_read_write = 0x80000000 | 0x40000000
    handle = kernel32.CreateFileW(
        interface_path,
        generic_read_write,
        0x00000001 | 0x00000002,
        None,
        3,
        0,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        buffer = (ctypes.c_ubyte * 7)()
        buffer[0] = 0xCC
        if not hid.HidD_GetFeature(handle, ctypes.byref(buffer), len(buffer)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bytes(buffer)
    finally:
        kernel32.CloseHandle(handle)


def _run_read_parent() -> int:
    interface_path, details = _qualified_interface()
    if interface_path is None:
        print(json.dumps(details, indent=2))
        return 2
    details["hardware_write_attempted"] = False
    details["feature_read_attempted"] = True
    try:
        completed = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--worker-read", interface_path],
            capture_output=True,
            text=True,
            timeout=WORKER_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        details["status"] = "uncertain: feature read exceeded the two-second deadline"
        print(json.dumps(details, indent=2))
        return 3
    if completed.returncode != 0 or READ_SUCCESS_MARKER not in completed.stdout:
        details["status"] = "feature read unsupported or failed"
        details["worker_exit_code"] = completed.returncode
        details["worker_error"] = completed.stderr.strip()
        print(json.dumps(details, indent=2))
        return 4
    details["status"] = "feature read returned"
    details["readback"] = completed.stdout.split(READ_SUCCESS_MARKER, 1)[1].strip()
    print(json.dumps(details, indent=2))
    return 0


def _run_parent(send: bool, send_all_white: bool) -> int:
    interface_path, details = _qualified_interface()
    if interface_path is None:
        print(json.dumps(details, indent=2))
        return 2
    if not send and not send_all_white:
        details["status"] = (
            "dry-run only; pass --send-a-blue or --send-all-white to transmit"
        )
        print(json.dumps(details, indent=2))
        return 0

    details["hardware_write_attempted"] = True
    worker_flag = "--worker-all-white" if send_all_white else "--worker-a-blue"
    expected_marker = MATRIX_SUCCESS_MARKER if send_all_white else SUCCESS_MARKER
    timeout = MATRIX_WORKER_TIMEOUT_SECONDS if send_all_white else WORKER_TIMEOUT_SECONDS
    if send_all_white:
        details["report"] = "120 x CC 01 KeyID FF FF FF 00"
        details["report_count"] = len(WHITE_MATRIX_REPORTS)
    try:
        completed = subprocess.run(
            [sys.executable, os.path.abspath(__file__), worker_flag, interface_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        details["status"] = f"uncertain: worker exceeded the {timeout:g}-second deadline"
        print(json.dumps(details, indent=2))
        return 3

    if completed.returncode != 0 or expected_marker not in completed.stdout:
        details["status"] = "failed or uncertain: Windows did not confirm submission"
        details["worker_exit_code"] = completed.returncode
        details["worker_error"] = completed.stderr.strip()
        print(json.dumps(details, indent=2))
        return 4
    details["status"] = (
        "Windows accepted the feature report; visual application is not acknowledged"
    )
    print(json.dumps(details, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--send-a-blue",
        action="store_true",
        help="Transmit the single hard-coded A-key physical-blue qualification frame.",
    )
    parser.add_argument(
        "--send-all-white",
        action="store_true",
        help="Transmit one hard-coded uniform-white 6x20 matrix pass.",
    )
    parser.add_argument("--worker-a-blue", nargs=1, help=argparse.SUPPRESS)
    parser.add_argument("--worker-all-white", nargs=1, help=argparse.SUPPRESS)
    parser.add_argument("--read-feature", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-read", nargs=1, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.send_a_blue and args.send_all_white:
        parser.error("choose exactly one send operation")
    if args.worker_a_blue:
        _set_exact_feature_reports(args.worker_a_blue[0], (QUALIFICATION_REPORT,))
        print(SUCCESS_MARKER)
        return 0
    if args.worker_all_white:
        _set_exact_feature_reports(args.worker_all_white[0], WHITE_MATRIX_REPORTS)
        print(MATRIX_SUCCESS_MARKER)
        return 0
    if args.worker_read:
        print(READ_SUCCESS_MARKER, format_report(_get_feature_report(args.worker_read[0])))
        return 0
    if args.read_feature:
        return _run_read_parent()
    return _run_parent(args.send_a_blue, args.send_all_white)


if __name__ == "__main__":
    raise SystemExit(main())
