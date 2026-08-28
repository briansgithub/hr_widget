"""Observe native DCHU calls made by the exact v1.14 Clevo LED application.

The injected observer attaches only to LedKeyboardSetting.exe and records
arguments and return values.  It never replaces a function, changes an
argument, writes device state, or attaches to FnKey.exe.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sys
import time


EXPECTED_EXE = Path(
    r"C:\Program Files\WindowsApps\CLEVOCO.LedKeyboardSetting_1.14.0.0_x64__6h6z29zh29qx0\CC\LedKeyboardSetting.exe"
)
EXPECTED_SHA256 = "301F88912D1BD137C4D4572BA5EFE9D37825D949932155B62A9A6C6FCBC08F14"
EXPECTED_PROCESS_NAME = "LedKeyboardSetting.exe"


OBSERVER_SOURCE = r"""
'use strict';

const moduleName = 'InsydeDCHU.dll';
const module = Process.getModuleByName(moduleName);
let nextCallId = 1;

function bytesAt(pointer, requestedLength) {
    const safeLength = Math.max(0, Math.min(requestedLength, 512));
    if (pointer.isNull() || safeLength === 0) {
        return [];
    }
    return Array.from(new Uint8Array(pointer.readByteArray(safeLength)));
}

function hookSetDchu() {
    const target = module.getExportByName('SetDCHU_Data');
    Interceptor.attach(target, {
        onEnter(args) {
            this.callId = nextCallId++;
            this.command = args[0].toInt32();
            this.length = args[2].toInt32();
            this.payload = bytesAt(args[1], this.length);
        },
        onLeave(retval) {
            send({
                event: 'SetDCHU_Data',
                call_id: this.callId,
                thread_id: this.threadId,
                command: this.command,
                length: this.length,
                payload: this.payload,
                result: retval.toInt32()
            });
        }
    });
    return target.toString();
}

function hookWriteSettings() {
    const target = module.getExportByName('WriteAppSettings');
    Interceptor.attach(target, {
        onEnter(args) {
            this.callId = nextCallId++;
            this.page = args[0].toInt32();
            this.offset = args[1].toInt32();
            this.length = args[2].toInt32();
            this.payload = bytesAt(args[3], this.length);
        },
        onLeave(retval) {
            send({
                event: 'WriteAppSettings',
                call_id: this.callId,
                thread_id: this.threadId,
                page: this.page,
                offset: this.offset,
                length: this.length,
                payload: this.payload,
                result: retval.toInt32()
            });
        }
    });
    return target.toString();
}

const hooks = {
    SetDCHU_Data: hookSetDchu(),
    WriteAppSettings: hookWriteSettings()
};
send({event: 'READY', module: module.path, hooks: hooks});
"""


def _process_path(pid: int) -> Path:
    if os.name != "nt":
        raise RuntimeError("This trace is Windows-only")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    open_process.restype = ctypes.c_void_p
    query_path = kernel32.QueryFullProcessImageNameW
    query_path.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_wchar),
        ctypes.POINTER(ctypes.c_ulong),
    ]
    query_path.restype = ctypes.c_bool
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_bool

    handle = open_process(0x1000, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = ctypes.c_ulong(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not query_path(handle, 0, buffer, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return Path(buffer.value)
    finally:
        close_handle(handle)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _find_exact_pid() -> int:
    import frida

    matches: list[int] = []
    for process in frida.get_local_device().enumerate_processes():
        if process.name.casefold() != EXPECTED_PROCESS_NAME.casefold():
            continue
        try:
            actual_path = _process_path(process.pid)
        except OSError:
            continue
        if str(actual_path).casefold() == str(EXPECTED_EXE).casefold():
            matches.append(process.pid)
    if len(matches) != 1:
        raise RuntimeError(f"Expected one exact LED process; found {len(matches)}")
    return matches[0]


def _verify_target() -> int:
    if not EXPECTED_EXE.is_file():
        raise RuntimeError("The exact allowlisted v1.14 LED executable is missing")
    actual_hash = _sha256(EXPECTED_EXE)
    if actual_hash != EXPECTED_SHA256:
        raise RuntimeError(f"LED executable hash mismatch: {actual_hash}")
    pid = _find_exact_pid()
    print(
        json.dumps(
            {
                "status": "verified",
                "pid": pid,
                "path": str(EXPECTED_EXE),
                "sha256": actual_hash,
                "attaches_to_fnkey": False,
            }
        ),
        flush=True,
    )
    return pid


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    pid = _verify_target()
    if args.verify_only:
        return 0

    import frida

    output = args.output or Path(os.environ.get("TEMP", ".")) / (
        f"ClevoDchuTrace_{time.strftime('%Y%m%d_%H%M%S')}.ndjson"
    )
    session = frida.attach(pid)
    script = session.create_script(OBSERVER_SOURCE)
    ready = False

    with output.open("x", encoding="utf-8") as trace_file:
        def on_message(message: dict[str, object], data: bytes | None) -> None:
            nonlocal ready
            record: dict[str, object] = {
                "captured_at_ns": time.time_ns(),
                "message": message,
            }
            if data is not None:
                record["data_hex"] = data.hex()
            trace_file.write(json.dumps(record, separators=(",", ":")) + "\n")
            trace_file.flush()
            payload = message.get("payload")
            if isinstance(payload, dict) and payload.get("event") == "READY":
                ready = True
                print(f"READY {output}", flush=True)
            elif message.get("type") == "send":
                print(json.dumps(payload, separators=(",", ":")), flush=True)
            else:
                print(json.dumps(message, separators=(",", ":")), flush=True)

        script.on("message", on_message)
        try:
            script.load()
            deadline = time.monotonic() + 3.0
            while not ready and time.monotonic() < deadline:
                time.sleep(0.02)
            if not ready:
                raise RuntimeError("The DCHU observer did not become ready")
            print("Press Enter to stop after one OEM color click.", flush=True)
            input()
        finally:
            try:
                script.unload()
            finally:
                session.detach()
    print(f"STOPPED {output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
