"""Manually gated, auto-restoring command-103 brightness qualification pulse."""

from __future__ import annotations

import argparse
import json
import threading

import frida

try:
    from .clevo_dchu_call_trace import _verify_target
except ImportError:
    from clevo_dchu_call_trace import _verify_target


PULSE_MILLISECONDS = 2000
EXPECTED_DCHU_COMMAND = 103
TARGET_LEVEL = 2
TARGET_VALUE = 95
EXPECTED_START_LEVEL = 3
EXPECTED_START_VALUE = 143
STATUS_FRAME = (0x01, 0xB0, 0x07, 0xE0)


SOURCE = rf"""
'use strict';
const module = Process.getModuleByName('InsydeDCHU.dll');
const readSettings = new NativeFunction(
    module.getExportByName('ReadAppSettings'),
    'int',
    ['int', 'int', 'int', 'pointer']
);
const setDchu = new NativeFunction(
    module.getExportByName('SetDCHU_Data'),
    'int',
    ['int', 'pointer', 'int']
);
const sleep = new NativeFunction(
    Process.getModuleByName('kernel32.dll').getExportByName('Sleep'),
    'void',
    ['uint']
);

function readByte(page, offset) {{
    const buffer = Memory.alloc(1);
    buffer.writeU8(0);
    const result = readSettings(page, offset, 1, buffer);
    return {{bytes: [buffer.readU8()], result: result}};
}}

function sendFrame(payload) {{
    const buffer = Memory.alloc(4);
    buffer.writeByteArray(payload);
    return {{
        command: 103,
        payload: payload,
        result: setDchu(103, buffer, 4)
    }};
}}

const profileBefore = readByte(1, 1);
const statusBefore = readByte(2, 80);
const brightnessBefore = readByte(2, 35);
if (profileBefore.bytes[0] < 0 || profileBefore.bytes[0] > 3) {{
    throw new Error('CPU profile sentinel is outside the qualified range');
}}
if (statusBefore.bytes[0] !== 1) {{
    throw new Error('LED status is not on');
}}
if (brightnessBefore.bytes[0] !== {EXPECTED_START_LEVEL}) {{
    throw new Error('Brightness is not at the qualified starting level');
}}

let applyResults = [];
let restoreResults = [];
try {{
    applyResults.push(sendFrame([{TARGET_VALUE}, 0, 0, 0xF4]));
    applyResults.push(sendFrame([0x01, 0xB0, 0x07, 0xE0]));
    sleep({PULSE_MILLISECONDS});
}} finally {{
    restoreResults.push(sendFrame([{EXPECTED_START_VALUE}, 0, 0, 0xF4]));
    restoreResults.push(sendFrame([0x01, 0xB0, 0x07, 0xE0]));
}}

send({{
    event: 'BRIGHTNESS_PULSE_COMPLETE',
    pulse_ms: {PULSE_MILLISECONDS},
    profile_before: profileBefore,
    profile_after: readByte(1, 1),
    stored_brightness_before: brightnessBefore,
    stored_brightness_after: readByte(2, 35),
    apply_results: applyResults,
    restore_results: restoreResults
}});
"""


def _send_pulse() -> dict[str, object]:
    pid = _verify_target()
    complete = threading.Event()
    result: dict[str, object] = {}
    session = frida.attach(pid)
    script = session.create_script(SOURCE)

    def on_message(message: dict[str, object], data: bytes | None) -> None:
        result["message"] = message
        if data is not None:
            result["data_hex"] = data.hex()
        complete.set()

    script.on("message", on_message)
    try:
        script.load()
        if not complete.wait(5.0):
            raise RuntimeError("Brightness pulse did not complete within five seconds")
    finally:
        try:
            script.unload()
        finally:
            session.detach()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send-dim-pulse", action="store_true")
    args = parser.parse_args()
    pid = _verify_target()
    if not args.send_dim_pulse:
        print(
            json.dumps(
                {
                    "status": "dry-run only",
                    "pid": pid,
                    "pulse_ms": PULSE_MILLISECONDS,
                    "command": EXPECTED_DCHU_COMMAND,
                    "target_level": TARGET_LEVEL,
                    "target_value": TARGET_VALUE,
                    "restore_level": EXPECTED_START_LEVEL,
                    "restore_value": EXPECTED_START_VALUE,
                    "status_frame": list(STATUS_FRAME),
                    "appsettings_writes": False,
                    "automatic_restore": True,
                },
                indent=2,
            )
        )
        return 0
    result = _send_pulse()
    print(json.dumps(result, indent=2))
    message = result.get("message")
    return 0 if isinstance(message, dict) and message.get("type") == "send" else 2


if __name__ == "__main__":
    raise SystemExit(main())
