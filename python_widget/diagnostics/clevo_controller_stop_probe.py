from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from keyboard_sync import ClevoIpcBackend, KeyboardSyncController, StaticColor


async def main() -> int:
    controller = KeyboardSyncController(ClevoIpcBackend(enable_real_broker=True))
    started_at = time.monotonic()
    enabled = await controller.enable()
    enabled_at = time.monotonic()
    accepted = False
    applied_cycles = 0
    if enabled:
        colors = (StaticColor(0, 0, 255), StaticColor(0, 255, 0))
        for index in range(8):
            accepted = await controller.request_static_color(colors[index % 2])
            await controller.wait_until_idle(timeout=3.0)
            if not accepted or not controller.enabled:
                break
            applied_cycles += 1
            await asyncio.sleep(1.05)
    applied_at = time.monotonic()
    stopped = await controller.disable()
    stopped_at = time.monotonic()
    fault = controller.fault
    print(
        json.dumps(
            {
                "enabled": enabled,
                "accepted": accepted,
                "applied_cycles": applied_cycles,
                "stopped": stopped,
                "state": controller.state.value,
                "broker_stop_confirmed": controller.broker_stop_confirmed,
                "fault": None if fault is None else fault.__dict__,
                "timing_ms": {
                    "enable": round((enabled_at - started_at) * 1000, 3),
                    "apply_and_idle": round((applied_at - enabled_at) * 1000, 3),
                    "disable": round((stopped_at - applied_at) * 1000, 3),
                },
            },
            indent=2,
        )
    )
    return 0 if stopped else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
