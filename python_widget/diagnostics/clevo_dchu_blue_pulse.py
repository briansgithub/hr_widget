"""Manually gated, auto-restoring command-103 color qualification pulse.

This diagnostic runs inside the exact signed OEM LED process because Windows
denies execute mapping of its DCHU DLL to unpackaged processes.  It sends only
three observed RGB segment frames, changes no AppSettings value, waits two
seconds, and restores the color read immediately before the pulse.
"""

from __future__ import annotations

import argparse
import json
import threading

import frida

try:
    from .clevo_dchu_call_trace import _verify_target
except ImportError:  # Direct script execution from the diagnostics directory.
    from clevo_dchu_call_trace import _verify_target


PULSE_MILLISECONDS = 2000
EXPECTED_DCHU_COMMAND = 103
EXPECTED_SEGMENTS = (0xF0, 0xF1, 0xF2)


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

function readBytes(page, offset, length) {{
    const buffer = Memory.alloc(length);
    buffer.writeByteArray(new Array(length).fill(0));
    const result = readSettings(page, offset, length, buffer);
    return {{
        bytes: Array.from(new Uint8Array(buffer.readByteArray(length))),
        result: result
    }};
}}

function sendRgb(rgb) {{
    const results = [];
    for (const segment of [0xF0, 0xF1, 0xF2]) {{
        const payload = [rgb[1], rgb[0], rgb[2], segment];
        const buffer = Memory.alloc(4);
        buffer.writeByteArray(payload);
        results.push({{
            command: 103,
            payload: payload,
            result: setDchu(103, buffer, 4)
        }});
    }}
    return results;
}}

const profileBefore = readBytes(1, 1, 1);
const statusBefore = readBytes(2, 80, 1);
const brightnessBefore = readBytes(2, 35, 1);
const colorBefore = readBytes(2, 81, 3);

if (statusBefore.bytes[0] !== 1) {{
    throw new Error('LED status is not on; pulse blocked');
}}
if (brightnessBefore.bytes[0] < 1 || brightnessBefore.bytes[0] > 4) {{
    throw new Error('LED brightness is outside the qualified range; pulse blocked');
}}
if (profileBefore.bytes[0] < 0 || profileBefore.bytes[0] > 3) {{
    throw new Error('CPU profile sentinel is outside the qualified range; pulse blocked');
}}

let applyResults = [];
let restoreResults = [];
try {{
    applyResults = sendRgb([0, 0, 255]);
    sleep({PULSE_MILLISECONDS});
}} finally {{
    restoreResults = sendRgb(colorBefore.bytes);
}}

const profileAfter = readBytes(1, 1, 1);
const colorAfter = readBytes(2, 81, 3);
send({{
    event: 'BLUE_PULSE_COMPLETE',
    pulse_ms: {PULSE_MILLISECONDS},
    profile_before: profileBefore,
    profile_after: profileAfter,
    stored_color_before: colorBefore,
    stored_color_after: colorAfter,
    status_before: statusBefore,
    brightness_before: brightnessBefore,
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
            raise RuntimeError("Blue pulse did not complete within five seconds")
    finally:
        try:
            script.unload()
        finally:
            session.detach()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--send-blue-pulse",
        action="store_true",
        help="Send the hard-coded two-second blue pulse and automatic restore.",
    )
    args = parser.parse_args()
    pid = _verify_target()
    if not args.send_blue_pulse:
        print(
            json.dumps(
                {
                    "status": "dry-run only",
                    "pid": pid,
                    "pulse_ms": PULSE_MILLISECONDS,
                    "command": EXPECTED_DCHU_COMMAND,
                    "segments": list(EXPECTED_SEGMENTS),
                    "target_physical_rgb": [0, 0, 255],
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
