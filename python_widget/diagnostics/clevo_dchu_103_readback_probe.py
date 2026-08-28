"""Read-only probe for a possible command-103 hardware-state buffer.

The probe attaches only to the exact allowlisted OEM LED process and calls only
GetDCHU_Data_Buffer with the fixed lighting command 103.  It never resolves a
write export and accepts no command number or payload from input.
"""

from __future__ import annotations

import json
import threading

import frida

from clevo_dchu_call_trace import _verify_target


SOURCE = r"""
'use strict';
const module = Process.getModuleByName('InsydeDCHU.dll');
const getBuffer = new NativeFunction(
    module.getExportByName('GetDCHU_Data_Buffer'),
    'int',
    ['int', 'pointer']
);
const buffer = Memory.alloc(256);
buffer.writeByteArray(new Array(256).fill(0));
const result = getBuffer(103, buffer);
send({
    event: 'DCHU_103_READBACK',
    command: 103,
    result: result,
    bytes: Array.from(new Uint8Array(buffer.readByteArray(256)))
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
            raise RuntimeError("The read-only command-103 probe timed out")
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
