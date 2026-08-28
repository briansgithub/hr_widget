"""Read fixed Clevo state inside the exact signed LED process; no writes."""

from __future__ import annotations

import json
import threading

import frida

from clevo_dchu_call_trace import _verify_target


SOURCE = r"""
'use strict';
const module = Process.getModuleByName('InsydeDCHU.dll');
const readSettings = new NativeFunction(
    module.getExportByName('ReadAppSettings'),
    'int',
    ['int', 'int', 'int', 'pointer']
);

function readField(name, page, offset, length) {
    const buffer = Memory.alloc(length);
    buffer.writeByteArray(new Array(length).fill(0));
    const result = readSettings(page, offset, length, buffer);
    return {
        name: name,
        page: page,
        offset: offset,
        bytes: Array.from(new Uint8Array(buffer.readByteArray(length))),
        result: result
    };
}

send({
    event: 'SNAPSHOT',
    fields: [
        readField('cpu_profile', 1, 1, 1),
        readField('led_status', 2, 80, 1),
        readField('led_rgb', 2, 81, 3),
        readField('led_brightness', 2, 35, 1)
    ]
});
"""


def main() -> int:
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
        if not complete.wait(3.0):
            raise RuntimeError("The read-only in-process snapshot timed out")
    finally:
        try:
            script.unload()
        finally:
            session.detach()
    print(json.dumps(result, indent=2))
    message = result.get("message")
    return 0 if isinstance(message, dict) and message.get("type") == "send" else 2


if __name__ == "__main__":
    raise SystemExit(main())
