from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from keyboard_sync import JsonLineProtocol


ALIAS = "hrclevobroker-exp.exe"
BACKEND_ID = "clevo-dchu-103-static-experimental"
BACKEND_VERSION = "0.2.6"
HARDWARE_IDENTITY = "Micro Electronics Inc|1520|PB50EF-G"
LEVELS = (0, 1, 2, 3, 4, 3, 2, 1, 0)


async def read_message(process: asyncio.subprocess.Process) -> dict[str, object]:
    if process.stdout is None:
        raise RuntimeError("Broker output is unavailable")
    line = await asyncio.wait_for(process.stdout.readline(), timeout=2.0)
    if not line:
        raise RuntimeError("Broker exited before responding")
    return JsonLineProtocol.decode(line)


async def write_message(process: asyncio.subprocess.Process, message: dict[str, object]) -> None:
    if process.stdin is None:
        raise RuntimeError("Broker input is unavailable")
    process.stdin.write(JsonLineProtocol.encode(message))
    await process.stdin.drain()


async def main() -> int:
    executable = shutil.which(ALIAS)
    if executable is None:
        raise RuntimeError("The experimental broker alias is unavailable")
    lease_id = f"brightness-sweep-{uuid.uuid4()}"
    process = await asyncio.create_subprocess_exec(
        executable,
        "--serve",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=0x08000000,
    )
    results: list[dict[str, object]] = []
    try:
        hello = await read_message(process)
        expected_hello = {
            "type": "HELLO",
            "protocol": 1,
            "backend_id": BACKEND_ID,
            "backend_version": BACKEND_VERSION,
        }
        if hello != expected_hello:
            raise RuntimeError(f"Unexpected broker identity: {hello}")
        await write_message(
            process,
            {
                "type": "CONFIG",
                "lease_id": lease_id,
                "hardware_identity": HARDWARE_IDENTITY,
                "min_interval_ms": 100,
            },
        )
        if await read_message(process) != {"type": "READY", "lease_id": lease_id}:
            raise RuntimeError("Broker did not accept the brightness lease")

        for sequence, level in enumerate(LEVELS, start=1):
            if sequence > 1:
                await asyncio.sleep(1.05)
            started = time.monotonic()
            await write_message(
                process,
                {
                    "type": "SET_BRIGHTNESS",
                    "lease_id": lease_id,
                    "sequence": sequence,
                    "level": level,
                },
            )
            response = await read_message(process)
            expected = {
                "type": "BRIGHTNESS_ACK",
                "lease_id": lease_id,
                "sequence": sequence,
                "confirmed_level": level,
                "confirmation": "synchronous_vendor_return_103",
            }
            if any(response.get(key) != value for key, value in expected.items()):
                raise RuntimeError(f"Brightness acknowledgement mismatch: {response}")
            results.append(
                {
                    "level": level,
                    "vendor_duration_ms": response["duration_ms"],
                    "round_trip_ms": round((time.monotonic() - started) * 1000, 3),
                }
            )

        await asyncio.sleep(1.05)
        await write_message(process, {"type": "STOP", "lease_id": lease_id})
        if await read_message(process) != {"type": "STOPPED", "lease_id": lease_id}:
            raise RuntimeError("Broker did not acknowledge stop")
        exit_code = await asyncio.wait_for(process.wait(), timeout=2.0)
        if exit_code != 0:
            raise RuntimeError(f"Broker exited with code {exit_code}")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()

    print(json.dumps({"status": "passed", "levels": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
