"""Bounded 12-worker CPU-profile measurement using Windows counters."""

from __future__ import annotations

import argparse
import json
import multiprocessing
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time


WORKERS = 12
RAMP_SECONDS = 2.0
SAMPLE_COUNT = 6
KNOWN_GOOD_PERFORMANCE_MHZ = 3350.0
MINIMUM_PERFORMANCE_MHZ = 2500.0

COUNTER_STREAM = Path(__file__).with_name("cpu_counter_stream.ps1")


def _busy_worker(stop: multiprocessing.synchronize.Event) -> None:
    value = 1.000001
    while not stop.is_set():
        for _ in range(100_000):
            value = (value * 1.0000001 + 0.0000003) % 97.0


def _cpu_sentinel() -> int:
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


def _line_reader(stream, output: queue.Queue[str | None]) -> None:
    try:
        for line in stream:
            output.put(line)
    finally:
        output.put(None)


def _start_counter_stream() -> tuple[subprocess.Popen[str], queue.Queue[str | None]]:
    process = subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(COUNTER_STREAM),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    output: queue.Queue[str | None] = queue.Queue()
    threading.Thread(target=_line_reader, args=(process.stdout, output), daemon=True).start()
    return process, output


def _next_counter_sample(output: queue.Queue[str | None]) -> dict[str, float]:
    try:
        line = output.get(timeout=3.0)
    except queue.Empty as exc:
        raise TimeoutError("The continuous counter stream stopped responding") from exc
    if not line:
        raise RuntimeError("The continuous counter stream ended early")
    value = json.loads(line)
    required = {
        "frequency_mhz",
        "processor_performance_percent",
        "processor_utility_percent",
        "performance_limit_percent",
        "performance_limit_flags",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise RuntimeError("The continuous counter stream returned an invalid sample")
    return {name: float(number) for name, number in value.items()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--starting-temperature-c", type=float, required=True)
    args = parser.parse_args()
    if args.starting_temperature_c > 65.0:
        raise RuntimeError("Starting CPU temperature exceeded the 65 C gate")

    initial_profile = _cpu_sentinel()
    if initial_profile != 2:
        raise RuntimeError(f"Performance sentinel 2 was not selected: {initial_profile}")

    stop = multiprocessing.Event()
    workers = [multiprocessing.Process(target=_busy_worker, args=(stop,)) for _ in range(WORKERS)]
    samples: list[dict[str, float]] = []
    aborted = False
    counter_process, counter_output = _start_counter_stream()
    try:
        # Prime the counter process before saturating every logical processor.
        # Otherwise its first PowerShell sample can be starved past the timeout.
        _next_counter_sample(counter_output)
        for worker in workers:
            worker.start()
        print("LOAD_STARTED", flush=True)
        for _ in range(round(RAMP_SECONDS)):
            _next_counter_sample(counter_output)
        print("SAMPLE_STARTED", flush=True)
        for _ in range(SAMPLE_COUNT):
            sample = _next_counter_sample(counter_output)
            samples.append(sample)
            print(json.dumps({"sample": len(samples), **sample}), flush=True)
            if sample["performance_limit_flags"] != 0:
                aborted = True
                break
    finally:
        stop.set()
        for worker in workers:
            worker.join(timeout=1.0)
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=1.0)
        if counter_process.poll() is None:
            counter_process.terminate()
        try:
            counter_process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            counter_process.kill()
            counter_process.wait(timeout=1.0)

    final_profile = _cpu_sentinel()
    mean_frequency = sum(sample["frequency_mhz"] for sample in samples) / len(samples)
    result = {
        "status": "aborted" if aborted else "completed",
        "starting_temperature_c": args.starting_temperature_c,
        "temperature_monitor": "external Clevo Control Center snapshots",
        "workers": WORKERS,
        "ramp_seconds": RAMP_SECONDS,
        "sample_count": len(samples),
        "initial_profile": initial_profile,
        "final_profile": final_profile,
        "mean_frequency_mhz": mean_frequency,
        "minimum_frequency_mhz": min(sample["frequency_mhz"] for sample in samples),
        "maximum_frequency_mhz": max(sample["frequency_mhz"] for sample in samples),
        "mean_utility_percent": sum(sample["processor_utility_percent"] for sample in samples) / len(samples),
        "maximum_performance_limit_flags": max(sample["performance_limit_flags"] for sample in samples),
        "minimum_required_mhz": MINIMUM_PERFORMANCE_MHZ,
        "known_good_85_percent_mhz": KNOWN_GOOD_PERFORMANCE_MHZ * 0.85,
        "samples": samples,
    }
    passed = (
        not aborted
        and len(samples) == SAMPLE_COUNT
        and final_profile == initial_profile == 2
        and mean_frequency >= MINIMUM_PERFORMANCE_MHZ
        and mean_frequency >= KNOWN_GOOD_PERFORMANCE_MHZ * 0.85
    )
    result["acceptance"] = "passed" if passed else "failed"
    print(json.dumps(result, indent=2), flush=True)
    return 0 if passed else 2


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
