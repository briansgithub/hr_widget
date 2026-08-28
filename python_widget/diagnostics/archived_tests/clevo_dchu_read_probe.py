"""Read-only standalone probe for the exact restored Clevo v1.14 package.

Only the fixed ReadAppSettings export is bound.  This module does not resolve,
bind, or call SetDCHU_Data or WriteAppSettings and cannot accept arbitrary page,
offset, command, or payload input.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


PACKAGE_DIR = Path(
    r"C:\Program Files\WindowsApps\CLEVOCO.LedKeyboardSetting_1.14.0.0_x64__6h6z29zh29qx0\CC"
)
DCHU_DLL = PACKAGE_DIR / "InsydeDCHU.dll"
DCHU_SHA256 = "98AF9793B911AB57FCBD09FD75441A120A19A529F4D18D03D83BE42DE7020A47"
WORKER_TIMEOUT_SECONDS = 2.0
SUCCESS_MARKER = "CLEVO_READ_ONLY_STATE"

# Fixed fields required for preflight and eventual reversible LED restoration.
READ_FIELDS = (
    ("cpu_profile", 1, 1, 1),
    ("led_status", 2, 80, 1),
    ("led_rgb", 2, 81, 3),
    ("led_brightness", 2, 35, 1),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def verify_exact_dll() -> dict[str, object]:
    if os.name != "nt":
        raise RuntimeError("The Clevo read-only probe is Windows-only")
    if not DCHU_DLL.is_file():
        raise RuntimeError("The exact v1.14 InsydeDCHU.dll is missing")
    actual_hash = _sha256(DCHU_DLL)
    if actual_hash != DCHU_SHA256:
        raise RuntimeError(f"InsydeDCHU.dll hash mismatch: {actual_hash}")
    return {"dll": str(DCHU_DLL), "sha256": actual_hash}


def _read_fixed_state() -> dict[str, object]:
    identity = verify_exact_dll()
    with os.add_dll_directory(str(PACKAGE_DIR)):
        dchu = ctypes.WinDLL(str(DCHU_DLL), use_last_error=True)
        read_settings = dchu.ReadAppSettings
        read_settings.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte),
        ]
        read_settings.restype = ctypes.c_int

        values: dict[str, object] = {}
        for name, page, offset, length in READ_FIELDS:
            buffer = (ctypes.c_ubyte * length)()
            result = read_settings(page, offset, length, buffer)
            values[name] = {
                "page": page,
                "offset": offset,
                "bytes": list(buffer),
                "result": result,
            }
    return {**identity, "state": values}


def _run_parent() -> int:
    identity = verify_exact_dll()
    try:
        completed = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--worker-read"],
            capture_output=True,
            text=True,
            timeout=WORKER_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(json.dumps({**identity, "status": "read timed out; result uncertain"}, indent=2))
        return 3
    if completed.returncode != 0 or SUCCESS_MARKER not in completed.stdout:
        print(
            json.dumps(
                {
                    **identity,
                    "status": "read-only worker failed",
                    "worker_exit_code": completed.returncode,
                    "worker_error": completed.stderr.strip(),
                },
                indent=2,
            )
        )
        return 4
    payload = completed.stdout.split(SUCCESS_MARKER, 1)[1].strip()
    print(payload)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-read", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_read:
        print(SUCCESS_MARKER)
        print(json.dumps(_read_fixed_state(), indent=2))
        return 0
    return _run_parent()


if __name__ == "__main__":
    raise SystemExit(main())
