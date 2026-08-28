"""One fixed blue-and-restore qualification cycle through the packaged broker."""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import threading
import time
import uuid


ALIAS = "hrclevobroker-exp.exe"
HARDWARE_IDENTITY = "Micro Electronics Inc|1520|PB50EF-G"
EXPECTED_BACKEND = "clevo-dchu-103-static-experimental"
EXPECTED_VERSION = "0.2.6"
TIMEOUT_SECONDS = 2.0


def _reader(stream, output: queue.Queue[str | None]) -> None:
    try:
        output.put(stream.readline())
    finally:
        output.put(None)


def _read_line(process: subprocess.Popen[str]) -> dict[str, object]:
    output: queue.Queue[str | None] = queue.Queue()
    threading.Thread(target=_reader, args=(process.stdout, output), daemon=True).start()
    try:
        line = output.get(timeout=TIMEOUT_SECONDS)
    except queue.Empty as exc:
        raise TimeoutError("Broker response timed out; no retry was attempted") from exc
    if not line:
        raise RuntimeError("Broker closed its output unexpectedly")
    if len(line.encode("utf-8")) > 4096 or not line.endswith("\n"):
        raise RuntimeError("Broker response was malformed or oversized")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise RuntimeError("Broker response root was not an object")
    return value


def _send(process: subprocess.Popen[str], value: dict[str, object]) -> None:
    line = json.dumps(value, separators=(",", ":")) + "\n"
    if len(line.encode("utf-8")) > 4096:
        raise RuntimeError("Client message exceeded the protocol limit")
    process.stdin.write(line)
    process.stdin.flush()


def main() -> int:
    executable = shutil.which(ALIAS)
    if not executable:
        raise RuntimeError("The experimental broker execution alias is unavailable")

    lease_id = str(uuid.uuid4())
    process = subprocess.Popen(
        [executable, "--serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    transcript: list[dict[str, object]] = []
    try:
        hello = _read_line(process)
        transcript.append(hello)
        if hello != {
            "type": "HELLO",
            "protocol": 1,
            "backend_id": EXPECTED_BACKEND,
            "backend_version": EXPECTED_VERSION,
        }:
            raise RuntimeError("Unexpected broker identity or protocol")

        _send(
            process,
            {
                "type": "CONFIG",
                "lease_id": lease_id,
                "hardware_identity": HARDWARE_IDENTITY,
                "min_interval_ms": 1000,
            },
        )
        ready = _read_line(process)
        transcript.append(ready)
        if ready != {"type": "READY", "lease_id": lease_id}:
            raise RuntimeError("Broker did not confirm the exact lease")

        _send(
            process,
            {
                "type": "SET_COLOR",
                "lease_id": lease_id,
                "sequence": 1,
                "rgb": {"r": 0, "g": 0, "b": 255},
            },
        )
        ack = _read_line(process)
        transcript.append(ack)
        expected_ack = {
            "type": "ACK",
            "lease_id": lease_id,
            "sequence": 1,
            "confirmed_rgb": {"r": 0, "g": 0, "b": 255},
            "confirmation": "synchronous_vendor_return_103",
        }
        for key, value in expected_ack.items():
            if ack.get(key) != value:
                raise RuntimeError("Broker did not synchronously acknowledge the fixed blue command")

        time.sleep(2.0)
        _send(process, {"type": "STOP", "lease_id": lease_id})
        stopped = _read_line(process)
        transcript.append(stopped)
        if stopped != {"type": "STOPPED", "lease_id": lease_id}:
            raise RuntimeError("Broker did not confirm restoration and stop")
        if process.wait(timeout=TIMEOUT_SECONDS) != 0:
            raise RuntimeError("Broker exited unsuccessfully after STOPPED")
    except Exception:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=TIMEOUT_SECONDS)
        raise

    print(json.dumps({"status": "completed", "transcript": transcript}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
