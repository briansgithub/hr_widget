"""Ten no-stress packaged-broker lifecycle cycles through the real controller."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from keyboard_sync import ClevoIpcBackend, KeyboardSyncController, StaticColor


CYCLE_COUNT = 10
COLORS = (StaticColor(255, 0, 0), StaticColor(0, 0, 255))


def read_cpu_sentinel() -> int:
    executable = shutil.which("hrclevobroker-exp.exe")
    if not executable:
        raise RuntimeError("The experimental broker alias is unavailable")
    completed = subprocess.run(
        [executable, "--probe"],
        capture_output=True,
        text=True,
        timeout=2.0,
        check=False,
    )
    payload = json.loads(completed.stdout)
    if completed.returncode != 0 or payload.get("status") != "ok":
        raise RuntimeError(payload.get("reason") or payload.get("error") or "Probe failed")
    return payload["state"]["cpu_profile"]["Bytes"][0]


async def main() -> int:
    results: list[dict[str, object]] = []
    initial_profile = read_cpu_sentinel()
    for index in range(CYCLE_COUNT):
        color = COLORS[index % len(COLORS)]
        controller = KeyboardSyncController(ClevoIpcBackend(enable_real_broker=True))
        enabled = await controller.enable()
        accepted = await controller.request_static_color(color) if enabled else False
        if accepted:
            await controller.wait_until_idle(timeout=3.0)
            await asyncio.sleep(0.25)
        stopped = await controller.disable()
        result = {
            "cycle": index + 1,
            "requested_rgb": color.as_dict(),
            "enabled": enabled,
            "accepted": accepted,
            "stopped": stopped,
            "final_state": controller.state.value,
            "broker_stop_confirmed": controller.broker_stop_confirmed,
            "profile_after": read_cpu_sentinel(),
            "fault": None if controller.fault is None else controller.fault.__dict__,
        }
        results.append(result)
        if not all((enabled, accepted, stopped, controller.broker_stop_confirmed)):
            break
        if result["profile_after"] != initial_profile:
            break

    passed = (
        len(results) == CYCLE_COUNT
        and all(result["fault"] is None for result in results)
        and all(result["profile_after"] == initial_profile for result in results)
    )
    print(
        json.dumps(
            {
                "status": "passed" if passed else "failed",
                "initial_profile": initial_profile,
                "cycles": results,
            },
            indent=2,
        )
    )
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
