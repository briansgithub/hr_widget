"""Fail-closed, software-only lifecycle for optional keyboard synchronization.

This module intentionally contains no Clevo process control, package mutation,
vendor assembly loading, or hardware access.  The production Clevo backend is
blocked until a separately qualified, lighting-only IPC contract exists.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import math
import shutil
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping


class KeyboardSyncState(str, Enum):
    OEM_ONLY = "oem_only"
    PROBING = "probing"
    STARTING = "starting"
    ACTIVE_IDLE = "active_idle"
    APPLYING = "applying"
    DRAINING = "draining"
    VERIFYING = "verifying"
    BLOCKED = "blocked"
    QUARANTINED = "quarantined"


class KeyboardSyncError(RuntimeError):
    """Base error for the software-only keyboard-sync subsystem."""


class BackendUnavailableError(KeyboardSyncError):
    """Raised when no qualified lighting-only backend is available."""


class ProtocolValidationError(KeyboardSyncError):
    """Raised when a helper protocol line is malformed or unsafe."""


@dataclass(frozen=True)
class StaticColor:
    r: int
    g: int
    b: int

    def __post_init__(self) -> None:
        for name in ("r", "g", "b"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
                raise ValueError(f"{name} must be an integer from 0 through 255")

    def as_dict(self) -> dict[str, int]:
        return {
            "r": self.r,
            "g": self.g,
            "b": self.b,
        }


@dataclass(frozen=True)
class BackendProbe:
    qualified: bool
    backend_id: str
    backend_version: str
    reason: str = ""


@dataclass(frozen=True)
class BackendStart:
    lease_id: str
    backend_id: str
    backend_version: str
    capabilities: frozenset[str]
    original_color: StaticColor


@dataclass(frozen=True)
class ApplyRequest:
    lease_id: str
    sequence: int
    color: StaticColor
    deadline_monotonic: float

    def __post_init__(self) -> None:
        if not self.lease_id:
            raise ValueError("lease_id is required")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise ValueError("sequence must be a positive integer")
        if not math.isfinite(self.deadline_monotonic):
            raise ValueError("deadline_monotonic must be finite")


@dataclass(frozen=True)
class ApplyAck:
    lease_id: str
    sequence: int
    color: StaticColor
    duration_ms: float

    def __post_init__(self) -> None:
        if not self.lease_id:
            raise ValueError("lease_id is required")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise ValueError("sequence must be a positive integer")
        if not math.isfinite(self.duration_ms) or self.duration_ms < 0:
            raise ValueError("duration_ms must be finite and non-negative")


@dataclass(frozen=True)
class BrightnessRequest:
    lease_id: str
    sequence: int
    level: int
    deadline_monotonic: float

    def __post_init__(self) -> None:
        if not self.lease_id:
            raise ValueError("lease_id is required")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise ValueError("sequence must be a positive integer")
        if isinstance(self.level, bool) or not isinstance(self.level, int) or not 0 <= self.level <= 4:
            raise ValueError("level must be an integer from 0 through 4")
        if not math.isfinite(self.deadline_monotonic):
            raise ValueError("deadline_monotonic must be finite")


@dataclass(frozen=True)
class BrightnessAck:
    lease_id: str
    sequence: int
    level: int
    duration_ms: float

    def __post_init__(self) -> None:
        if not self.lease_id:
            raise ValueError("lease_id is required")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise ValueError("sequence must be a positive integer")
        if isinstance(self.level, bool) or not isinstance(self.level, int) or not 0 <= self.level <= 4:
            raise ValueError("level must be an integer from 0 through 4")
        if not math.isfinite(self.duration_ms) or self.duration_ms < 0:
            raise ValueError("duration_ms must be finite and non-negative")


@dataclass(frozen=True)
class RestoreAck:
    lease_id: str
    confirmed_color: StaticColor

    def __post_init__(self) -> None:
        if not self.lease_id:
            raise ValueError("lease_id is required")
        if not isinstance(self.confirmed_color, StaticColor):
            raise TypeError("confirmed_color must be a StaticColor")


@dataclass(frozen=True)
class BackendVerification:
    """Lease-bound, read-only proof that OEM platform control remains healthy."""

    lease_id: str
    receiver_identity_verified: bool
    fnkey_resident: bool
    fnkey_identity_verified: bool
    required_services_running: bool
    package_identities_unchanged: bool
    cpu_profile_control_available: bool
    oem_lighting_owner_resident: bool

    def __post_init__(self) -> None:
        if not self.lease_id:
            raise ValueError("lease_id is required")
        for field_name in (
            "receiver_identity_verified",
            "fnkey_resident",
            "fnkey_identity_verified",
            "required_services_running",
            "package_identities_unchanged",
            "cpu_profile_control_available",
            "oem_lighting_owner_resident",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"{field_name} must be a boolean")

    @property
    def all_safe(self) -> bool:
        return all(
            (
                self.receiver_identity_verified,
                self.fnkey_resident,
                self.fnkey_identity_verified,
                self.required_services_running,
                self.package_identities_unchanged,
                self.cpu_profile_control_available,
                self.oem_lighting_owner_resident,
            )
        )


@dataclass(frozen=True)
class ControllerFault:
    code: str
    message: str
    unknown_outcome: bool


@dataclass(frozen=True)
class ClevoTarget:
    """Immutable identity of the one machine/software stack observed in testing."""

    system_manufacturer: str
    system_product_name: str
    baseboard_product: str
    publisher_id: str
    fnkey_package_name: str
    fnkey_package_version: str
    fnkey_package_status: str
    fnkey_process_name: str
    fnkey_executable_path: str
    led_package_name: str
    led_package_version: str
    led_package_status: str
    control_center_package_name: str
    control_center_package_version: str
    control_center_package_status: str
    required_services: frozenset[str]


@dataclass(frozen=True)
class ReadOnlySystemSnapshot:
    """Already-collected OS-visible facts supplied to the fail-closed probe."""

    system_manufacturer: str
    system_product_name: str
    baseboard_product: str
    publisher_id: str
    fnkey_package_name: str
    fnkey_package_version: str
    fnkey_package_status: str
    fnkey_pid: int
    fnkey_executable_path: str
    led_package_name: str
    led_package_version: str
    led_package_status: str
    control_center_package_name: str
    control_center_package_version: str
    control_center_package_status: str
    running_processes: frozenset[str]
    running_services: frozenset[str]


CLEVO_TARGET_ALLOWLIST: tuple[ClevoTarget, ...] = (
    ClevoTarget(
        system_manufacturer="Micro Electronics Inc",
        system_product_name="1520",
        baseboard_product="PB50EF-G",
        publisher_id="6h6z29zh29qx0",
        fnkey_package_name="CLEVOCO.FnhotkeysandOSD",
        fnkey_package_version="1.14.0.0",
        fnkey_package_status="Ok",
        fnkey_process_name="FnKey.exe",
        fnkey_executable_path=(
            r"C:\Program Files\WindowsApps\CLEVOCO.FnhotkeysandOSD_"
            r"1.14.0.0_x64__6h6z29zh29qx0\FnKey\FnKey.exe"
        ),
        led_package_name="CLEVOCO.LedKeyboardSetting",
        led_package_version="7.68.0.0",
        led_package_status="Ok",
        control_center_package_name="CLEVOCO.ControlCenter3.0",
        control_center_package_version="1.13.2.0",
        control_center_package_status="Ok",
        required_services=frozenset({"CCDCHUService", "HKClipSvc"}),
    ),
)


class ReadOnlySnapshotProvider(ABC):
    """Injection seam for facts collected outside this hardware-free module."""

    @abstractmethod
    async def snapshot(self) -> ReadOnlySystemSnapshot | None:
        """Return a snapshot without changing processes, services, or packages."""


class StaticSnapshotProvider(ReadOnlySnapshotProvider):
    """Test provider that returns one immutable snapshot and performs no I/O."""

    def __init__(self, value: ReadOnlySystemSnapshot | None) -> None:
        self.value = value
        self.calls = 0

    async def snapshot(self) -> ReadOnlySystemSnapshot | None:
        self.calls += 1
        return self.value


REQUIRED_BACKEND_CAPABILITIES = frozenset(
    {
        "lighting_only",
        "coexists_with_oem",
        "acknowledged_apply",
        "synchronous_vendor_ack",
        "restore_original_lighting",
        "specific_verified_receiver",
        "fnkey_residency_monitor",
        "service_health_monitor",
        "cpu_profile_read_only",
        "unelevated_broker",
        "parent_job_lifetime",
        "widget_instance_mutex",
        "package_identity_monitor",
        "manual_lighting_reclaim",
        "windows_power_session_guard",
        "abort_safe",
        "bounded_cancellation",
    }
)


class KeyboardLightingBackend(ABC):
    """Abstract lighting-only backend contract.

    Implementations must not take ownership of an OEM platform controller.  A
    backend that cannot meet that condition must report ``qualified=False``.
    The ``abort_safe`` capability specifically means that ``stop`` can revoke
    a timed-out operation by terminating the isolated broker, and that no late
    task result can revive the broker or perform another hardware write.  The
    ``bounded_cancellation`` capability means every IPC coroutine has been
    proven to propagate cancellation promptly; otherwise a controller deadline
    deliberately keeps serialization held rather than allowing cleanup to race
    an operation whose outcome remains unknown.
    """

    @abstractmethod
    async def probe(self) -> BackendProbe:
        """Perform read-only capability discovery."""

    @abstractmethod
    async def start(self, lease_id: str) -> BackendStart:
        """Start an isolated lighting-only session without applying a color."""

    @abstractmethod
    async def apply_static_color(self, request: ApplyRequest) -> ApplyAck:
        """Apply one color and acknowledge only after completion."""

    @abstractmethod
    async def apply_brightness(self, request: BrightnessRequest) -> BrightnessAck:
        """Apply one observed OEM brightness level and acknowledge completion."""

    @abstractmethod
    async def restore_original_lighting(self) -> RestoreAck:
        """Restore lighting captured at ``start`` and confirm vendor acceptance.

        Return only after the same lighting-only route synchronously accepts
        the original state, without reading or changing any platform
        performance mode. This firmware has no actual LED color readback.
        """

    @abstractmethod
    async def verify_oem_health(self, lease_id: str) -> BackendVerification:
        """Return typed, lease-bound read-only OEM health evidence."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop only the isolated broker. This method must be idempotent."""


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_longlong),
        ("per_job_user_time_limit", ctypes.c_longlong),
        ("limit_flags", ctypes.c_uint32),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority_class", ctypes.c_uint32),
        ("scheduling_class", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operation_count", ctypes.c_ulonglong),
        ("write_operation_count", ctypes.c_ulonglong),
        ("other_operation_count", ctypes.c_ulonglong),
        ("read_transfer_count", ctypes.c_ulonglong),
        ("write_transfer_count", ctypes.c_ulonglong),
        ("other_transfer_count", ctypes.c_ulonglong),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _JobBasicLimitInformation),
        ("io_info", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


class _WindowsBrokerGuard:
    """Session mutex plus kill-on-close Job object for only our broker."""

    _MUTEX_NAME = "Local\\HRWidget.ClevoBroker.Experimental.Session"
    _ERROR_ALREADY_EXISTS = 183
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100

    def __init__(self) -> None:
        if not hasattr(ctypes, "WinDLL"):
            raise BackendUnavailableError("The packaged Clevo broker is Windows-only.")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel32.CloseHandle.restype = ctypes.c_bool
        self._kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
        self._kernel32.ReleaseMutex.restype = ctypes.c_bool
        self._mutex = 0
        self._job = 0

    def acquire(self) -> None:
        create_mutex = self._kernel32.CreateMutexW
        create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        create_mutex.restype = ctypes.c_void_p
        self._mutex = int(create_mutex(None, True, self._MUTEX_NAME) or 0)
        if not self._mutex:
            raise BackendUnavailableError("Could not acquire the keyboard-sync session mutex.")
        if ctypes.get_last_error() == self._ERROR_ALREADY_EXISTS:
            self.close()
            raise BackendUnavailableError("Another keyboard-sync session is already active.")

        create_job = self._kernel32.CreateJobObjectW
        create_job.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        create_job.restype = ctypes.c_void_p
        self._job = int(create_job(None, None) or 0)
        if not self._job:
            self.close()
            raise BackendUnavailableError("Could not create the broker lifetime guard.")

        information = _JobExtendedLimitInformation()
        information.basic_limit_information.limit_flags = (
            self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        set_information = self._kernel32.SetInformationJobObject
        set_information.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        set_information.restype = ctypes.c_bool
        if not set_information(
            self._job,
            self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            self.close()
            raise BackendUnavailableError("Could not configure broker termination containment.")

    def attach(self, pid: int) -> None:
        open_process = self._kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
        open_process.restype = ctypes.c_void_p
        process = int(
            open_process(
                self._PROCESS_TERMINATE | self._PROCESS_SET_QUOTA,
                False,
                pid,
            )
            or 0
        )
        if not process:
            raise BackendUnavailableError("Could not open the broker for lifetime containment.")
        try:
            assign = self._kernel32.AssignProcessToJobObject
            assign.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            assign.restype = ctypes.c_bool
            if not assign(self._job, process):
                raise BackendUnavailableError("Could not attach the broker to its lifetime guard.")
        finally:
            self._kernel32.CloseHandle(process)

    def close(self) -> None:
        if self._job:
            self._kernel32.CloseHandle(self._job)
            self._job = 0
        if self._mutex:
            self._kernel32.ReleaseMutex(self._mutex)
            self._kernel32.CloseHandle(self._mutex)
            self._mutex = 0


class ClevoIpcBackend(KeyboardLightingBackend):
    """Exact-machine packaged broker using only fixed lighting command 103."""

    _ALIAS = "hrclevobroker-exp.exe"
    _BACKEND_ID = "clevo-dchu-103-static-experimental"
    _BACKEND_VERSION = "0.2.6"
    _PACKAGE_PREFIX = "HRWidget.ClevoBroker.Experimental_0.2.6.0_x64__"
    _HARDWARE_IDENTITY = "Micro Electronics Inc|1520|PB50EF-G"
    _REASON = (
        "Keyboard sync is unavailable unless this session explicitly enables "
        "the installed experimental packaged broker."
    )

    def __init__(
        self,
        snapshot_provider: ReadOnlySnapshotProvider | None = None,
        *,
        enable_real_broker: bool = False,
    ) -> None:
        self._snapshot_provider = snapshot_provider
        self._enable_real_broker = bool(enable_real_broker)
        self.last_snapshot: ReadOnlySystemSnapshot | None = None
        self.matching_target: ClevoTarget | None = None
        self._probe_payload: dict[str, Any] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._guard: _WindowsBrokerGuard | None = None
        self._lease_id: str | None = None
        self._original_color: StaticColor | None = None

    async def probe(self) -> BackendProbe:
        if self._snapshot_provider is not None:
            try:
                self.last_snapshot = await self._snapshot_provider.snapshot()
            except Exception as exc:
                return BackendProbe(
                    qualified=False,
                    backend_id=self._BACKEND_ID,
                    backend_version=self._BACKEND_VERSION,
                    reason=f"Read-only system snapshot failed: {str(exc) or type(exc).__name__}",
                )
            self.matching_target = self._match_snapshot(self.last_snapshot)
            if self.matching_target is None:
                return BackendProbe(
                    qualified=False,
                    backend_id=self._BACKEND_ID,
                    backend_version=self._BACKEND_VERSION,
                    reason="System snapshot does not match the immutable qualified-target allowlist.",
                )

        if not self._enable_real_broker:
            return BackendProbe(
                qualified=False,
                backend_id=self._BACKEND_ID,
                backend_version=self._BACKEND_VERSION,
                reason=self._REASON,
            )

        try:
            payload = await self._run_probe_process()
            self._validate_probe_payload(payload)
        except Exception as exc:
            return BackendProbe(
                qualified=False,
                backend_id=self._BACKEND_ID,
                backend_version=self._BACKEND_VERSION,
                reason=str(exc) or type(exc).__name__,
            )
        self._probe_payload = payload
        return BackendProbe(
            qualified=True,
            backend_id=self._BACKEND_ID,
            backend_version=self._BACKEND_VERSION,
        )

    async def start(self, lease_id: str) -> BackendStart:
        if self._probe_payload is None:
            raise BackendUnavailableError("The packaged broker has not passed its read-only probe.")
        if self._process is not None:
            raise KeyboardSyncError("A packaged broker session is already active.")

        executable = shutil.which(self._ALIAS)
        if not executable:
            raise BackendUnavailableError("The experimental broker execution alias is unavailable.")

        guard = _WindowsBrokerGuard()
        guard.acquire()
        creationflags = 0x08000000
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                "--serve",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
            )
            guard.attach(process.pid)
        except BaseException:
            guard.close()
            raise

        self._guard = guard
        self._process = process
        self._lease_id = lease_id
        try:
            hello = await self._read_message()
            if hello != {
                "type": "HELLO",
                "protocol": 1,
                "backend_id": self._BACKEND_ID,
                "backend_version": self._BACKEND_VERSION,
            }:
                raise ProtocolValidationError("The broker HELLO identity did not match.")
            await self._write_message(
                {
                    "type": "CONFIG",
                    "lease_id": lease_id,
                    "hardware_identity": self._HARDWARE_IDENTITY,
                    "min_interval_ms": 100,
                }
            )
            ready = await self._read_message()
            if ready != {"type": "READY", "lease_id": lease_id}:
                raise ProtocolValidationError("The broker READY lease did not match.")
        except BaseException:
            await self.stop()
            raise

        channels = self._probe_payload["original_physical_rgb"]
        original = StaticColor(*channels)
        self._original_color = original
        return BackendStart(
            lease_id=lease_id,
            backend_id=self._BACKEND_ID,
            backend_version=self._BACKEND_VERSION,
            capabilities=REQUIRED_BACKEND_CAPABILITIES,
            original_color=original,
        )

    async def apply_static_color(self, request: ApplyRequest) -> ApplyAck:
        if not self._enable_real_broker:
            raise BackendUnavailableError(self._REASON)
        self._require_active_lease(request.lease_id)
        await self._write_message(
            {
                "type": "SET_COLOR",
                "lease_id": request.lease_id,
                "sequence": request.sequence,
                "rgb": request.color.as_dict(),
            }
        )
        response = await self._read_message()
        if response.get("type") == "NACK":
            raise KeyboardSyncError(f"Broker rejected lighting command: {response.get('code')}")
        expected = {
            "type": "ACK",
            "lease_id": request.lease_id,
            "sequence": request.sequence,
            "confirmed_rgb": request.color.as_dict(),
            "confirmation": "synchronous_vendor_return_103",
        }
        if any(response.get(key) != value for key, value in expected.items()):
            raise ProtocolValidationError("Broker ACK did not match the in-flight command.")
        return ApplyAck(
            lease_id=request.lease_id,
            sequence=request.sequence,
            color=request.color,
            duration_ms=float(response["duration_ms"]),
        )

    async def apply_brightness(self, request: BrightnessRequest) -> BrightnessAck:
        if not self._enable_real_broker:
            raise BackendUnavailableError(self._REASON)
        self._require_active_lease(request.lease_id)
        await self._write_message(
            {
                "type": "SET_BRIGHTNESS",
                "lease_id": request.lease_id,
                "sequence": request.sequence,
                "level": request.level,
            }
        )
        response = await self._read_message()
        if response.get("type") == "NACK":
            raise KeyboardSyncError(f"Broker rejected brightness command: {response.get('code')}")
        expected = {
            "type": "BRIGHTNESS_ACK",
            "lease_id": request.lease_id,
            "sequence": request.sequence,
            "confirmed_level": request.level,
            "confirmation": "synchronous_vendor_return_103",
        }
        if any(response.get(key) != value for key, value in expected.items()):
            raise ProtocolValidationError("Broker brightness ACK did not match the in-flight command.")
        return BrightnessAck(
            lease_id=request.lease_id,
            sequence=request.sequence,
            level=request.level,
            duration_ms=float(response["duration_ms"]),
        )

    async def restore_original_lighting(self) -> RestoreAck:
        if not self._enable_real_broker:
            raise BackendUnavailableError(self._REASON)
        lease_id = self._lease_id
        original = self._original_color
        self._require_active_lease(lease_id)
        if original is None:
            raise KeyboardSyncError("The broker has no captured original color.")
        await self._write_message({"type": "STOP", "lease_id": lease_id})
        response = await self._read_message()
        if response != {"type": "STOPPED", "lease_id": lease_id}:
            raise ProtocolValidationError("Broker did not confirm restoration and stop.")
        process = self._process
        if process is None or await process.wait() != 0:
            raise KeyboardSyncError("Broker exited unsuccessfully after restoration.")
        self._clear_process_guard()
        return RestoreAck(lease_id=lease_id, confirmed_color=original)

    async def verify_oem_health(self, lease_id: str) -> BackendVerification:
        if not self._enable_real_broker:
            raise BackendUnavailableError(self._REASON)
        if lease_id != self._lease_id:
            raise ProtocolValidationError("OEM health check used the wrong lease.")
        payload = await self._run_probe_process()
        self._validate_probe_payload(payload)
        original = self._probe_payload or {}
        process_healthy = self._process is None or self._process.returncode is None
        same_fnkey = (
            payload.get("fnkey_pid") == original.get("fnkey_pid")
            and payload.get("fnkey_start_time_utc_ticks")
            == original.get("fnkey_start_time_utc_ticks")
        )
        return BackendVerification(
            lease_id=lease_id,
            receiver_identity_verified=process_healthy,
            fnkey_resident=same_fnkey,
            fnkey_identity_verified=same_fnkey,
            required_services_running=True,
            package_identities_unchanged=(
                payload.get("package_full_name") == original.get("package_full_name")
                and payload.get("sha256") == original.get("sha256")
            ),
            cpu_profile_control_available=self._cpu_sentinel(payload) is not None,
            oem_lighting_owner_resident=same_fnkey,
        )

    async def stop(self) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        self._clear_process_guard()

    def _clear_process_guard(self) -> None:
        self._process = None
        if self._guard is not None:
            self._guard.close()
            self._guard = None

    def _require_active_lease(self, lease_id: str | None) -> None:
        if not lease_id or lease_id != self._lease_id:
            raise ProtocolValidationError("Broker operation used the wrong lease.")
        if self._process is None or self._process.returncode is not None:
            raise KeyboardSyncError("The packaged broker is not running.")

    async def _write_message(self, message: Mapping[str, Any]) -> None:
        self._require_active_lease(message.get("lease_id"))
        process = self._process
        if process is None or process.stdin is None:
            raise KeyboardSyncError("Broker input is unavailable.")
        process.stdin.write(JsonLineProtocol.encode(message))
        await process.stdin.drain()

    async def _read_message(self) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdout is None:
            raise KeyboardSyncError("Broker output is unavailable.")
        line = await process.stdout.readline()
        if not line:
            raise KeyboardSyncError("Broker exited before acknowledging the operation.")
        return JsonLineProtocol.decode(line)

    async def _run_probe_process(self) -> dict[str, Any]:
        executable = shutil.which(self._ALIAS)
        if not executable:
            raise BackendUnavailableError("The experimental broker execution alias is unavailable.")
        process = await asyncio.create_subprocess_exec(
            executable,
            "--probe",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=0x08000000,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=2.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise BackendUnavailableError("The broker read-only probe timed out.") from None
        if len(stdout) > JsonLineProtocol.MAX_LINE_BYTES or not stdout:
            raise ProtocolValidationError("The broker probe response was empty or oversized.")
        try:
            payload = json.loads(stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolValidationError("The broker probe response was invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise ProtocolValidationError("The broker probe response root was not an object.")
        payload["_exit_code"] = process.returncode
        if process.returncode not in {0, 4}:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise BackendUnavailableError(payload.get("error") or detail or "Broker probe failed.")
        return payload

    def _validate_probe_payload(self, payload: Mapping[str, Any]) -> None:
        if payload.get("status") != "ok":
            raise BackendUnavailableError(str(payload.get("reason") or "Broker preflight blocked."))
        if payload.get("backend_id") != self._BACKEND_ID or payload.get("backend_version") != self._BACKEND_VERSION:
            raise ProtocolValidationError("Broker probe identity mismatch.")
        if not str(payload.get("package_full_name", "")).startswith(self._PACKAGE_PREFIX):
            raise ProtocolValidationError("Broker package identity mismatch.")
        if payload.get("hardware_identity") != self._HARDWARE_IDENTITY:
            raise ProtocolValidationError("Broker hardware identity mismatch.")
        channels = payload.get("original_physical_rgb")
        if not isinstance(channels, list) or len(channels) != 3:
            raise ProtocolValidationError("Broker original color was invalid.")
        StaticColor(*channels)
        if self._cpu_sentinel(payload) is None:
            raise ProtocolValidationError("Broker CPU-profile sentinel was unavailable.")

    @staticmethod
    def _cpu_sentinel(payload: Mapping[str, Any]) -> int | None:
        try:
            values = payload["state"]["cpu_profile"]["Bytes"]
            value = values[0]
        except (KeyError, IndexError, TypeError):
            return None
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @staticmethod
    def _match_snapshot(snapshot: ReadOnlySystemSnapshot | None) -> ClevoTarget | None:
        if snapshot is None:
            return None

        def normalized(value: str) -> str:
            return value.strip().casefold()

        def normalized_path(value: str) -> str:
            return normalized(value).replace("/", "\\")

        processes = {normalized(name) for name in snapshot.running_processes}
        services = {normalized(name) for name in snapshot.running_services}
        for target in CLEVO_TARGET_ALLOWLIST:
            if (
                normalized(snapshot.system_manufacturer) == normalized(target.system_manufacturer)
                and normalized(snapshot.system_product_name) == normalized(target.system_product_name)
                and normalized(snapshot.baseboard_product) == normalized(target.baseboard_product)
                and normalized(snapshot.publisher_id) == normalized(target.publisher_id)
                and normalized(snapshot.fnkey_package_name) == normalized(target.fnkey_package_name)
                and normalized(snapshot.fnkey_package_version) == normalized(target.fnkey_package_version)
                and normalized(snapshot.fnkey_package_status) == normalized(target.fnkey_package_status)
                and snapshot.fnkey_pid > 0
                and normalized_path(snapshot.fnkey_executable_path)
                == normalized_path(target.fnkey_executable_path)
                and normalized(snapshot.led_package_name) == normalized(target.led_package_name)
                and normalized(snapshot.led_package_version) == normalized(target.led_package_version)
                and normalized(snapshot.led_package_status) == normalized(target.led_package_status)
                and normalized(snapshot.control_center_package_name)
                == normalized(target.control_center_package_name)
                and normalized(snapshot.control_center_package_version)
                == normalized(target.control_center_package_version)
                and normalized(snapshot.control_center_package_status)
                == normalized(target.control_center_package_status)
                and normalized(target.fnkey_process_name) in processes
                and {normalized(name) for name in target.required_services} <= services
            ):
                return target
        return None


class FakeBackend(KeyboardLightingBackend):
    """Deterministic, hardware-free backend for controller and UI tests."""

    def __init__(
        self,
        *,
        qualified: bool = True,
        backend_id: str = "fake-lighting",
        backend_version: str = "1.0",
        capabilities: frozenset[str] = REQUIRED_BACKEND_CAPABILITIES,
        original_color: StaticColor = StaticColor(255, 255, 255),
        apply_gate: asyncio.Event | None = None,
        apply_delay: float = 0.0,
        apply_error: BaseException | Callable[[ApplyRequest], BaseException | None] | None = None,
        ack_sequence_offset: int = 0,
        ack_color: StaticColor | None = None,
        start_error: BaseException | None = None,
        restore_error: BaseException | None = None,
        restore_ack_color: StaticColor | None = None,
        health_ok: bool = True,
        health_error: BaseException | None = None,
        health_gate: asyncio.Event | None = None,
        stop_error: BaseException | None = None,
        stop_gate: asyncio.Event | None = None,
        stop_delay: float = 0.0,
    ) -> None:
        self.qualified = qualified
        self.backend_id = backend_id
        self.backend_version = backend_version
        self.capabilities = frozenset(capabilities)
        self.original_color = original_color
        self.current_color = original_color
        self.apply_gate = apply_gate
        self.apply_delay = apply_delay
        self.apply_error = apply_error
        self.ack_sequence_offset = ack_sequence_offset
        self.ack_color = ack_color
        self.start_error = start_error
        self.restore_error = restore_error
        self.restore_ack_color = restore_ack_color
        self.health_ok = health_ok
        self.health_error = health_error
        self.health_gate = health_gate
        self.stop_error = stop_error
        self.stop_gate = stop_gate
        self.stop_delay = stop_delay

        self.calls: list[str] = []
        self.applied_requests: list[ApplyRequest] = []
        self.applied_brightness_requests: list[BrightnessRequest] = []
        self.probe_count = 0
        self.start_count = 0
        self.restore_count = 0
        self.stop_count = 0
        self.health_count = 0
        self.max_concurrent_applies = 0
        self.apply_started = asyncio.Event()
        self.stop_started = asyncio.Event()

        self._started = False
        self._lease_id: str | None = None
        self._active_applies = 0

    async def probe(self) -> BackendProbe:
        self.calls.append("probe")
        self.probe_count += 1
        return BackendProbe(
            qualified=self.qualified,
            backend_id=self.backend_id,
            backend_version=self.backend_version,
            reason="" if self.qualified else "Fake backend configured as unavailable.",
        )

    async def start(self, lease_id: str) -> BackendStart:
        self.calls.append("start")
        self.start_count += 1
        if self.start_error is not None:
            raise self.start_error
        if not self.qualified:
            raise BackendUnavailableError("Fake backend is not qualified.")
        self._started = True
        self._lease_id = lease_id
        return BackendStart(
            lease_id=lease_id,
            backend_id=self.backend_id,
            backend_version=self.backend_version,
            capabilities=self.capabilities,
            original_color=self.original_color,
        )

    async def apply_static_color(self, request: ApplyRequest) -> ApplyAck:
        self.calls.append("apply")
        if not self._started:
            raise KeyboardSyncError("Fake backend was not started.")

        self.applied_requests.append(request)
        self._active_applies += 1
        self.max_concurrent_applies = max(self.max_concurrent_applies, self._active_applies)
        self.apply_started.set()
        started = time.monotonic()
        try:
            if self.apply_gate is not None:
                await self.apply_gate.wait()
            if self.apply_delay:
                await asyncio.sleep(self.apply_delay)

            error = self.apply_error
            if callable(error):
                error = error(request)
            if error is not None:
                raise error

            applied_color = self.ack_color or request.color
            self.current_color = applied_color
            return ApplyAck(
                lease_id=request.lease_id,
                sequence=request.sequence + self.ack_sequence_offset,
                color=applied_color,
                duration_ms=(time.monotonic() - started) * 1000.0,
            )
        finally:
            self._active_applies -= 1

    async def apply_brightness(self, request: BrightnessRequest) -> BrightnessAck:
        self.calls.append("brightness")
        if not self._started:
            raise KeyboardSyncError("Fake backend was not started.")
        self.applied_brightness_requests.append(request)
        return BrightnessAck(
            lease_id=request.lease_id,
            sequence=request.sequence,
            level=request.level,
            duration_ms=0.0,
        )

    async def restore_original_lighting(self) -> RestoreAck:
        self.calls.append("restore")
        self.restore_count += 1
        if self.restore_error is not None:
            raise self.restore_error
        restored = self.restore_ack_color or self.original_color
        self.current_color = restored
        if self._lease_id is None:
            raise KeyboardSyncError("Fake backend has no active lease.")
        return RestoreAck(lease_id=self._lease_id, confirmed_color=restored)

    async def verify_oem_health(self, lease_id: str) -> BackendVerification:
        self.calls.append("verify")
        self.health_count += 1
        if self.health_gate is not None:
            await self.health_gate.wait()
        if self.health_error is not None:
            raise self.health_error
        return BackendVerification(
            lease_id=lease_id,
            receiver_identity_verified=self.health_ok,
            fnkey_resident=self.health_ok,
            fnkey_identity_verified=self.health_ok,
            required_services_running=self.health_ok,
            package_identities_unchanged=self.health_ok,
            cpu_profile_control_available=self.health_ok,
            oem_lighting_owner_resident=self.health_ok,
        )

    async def stop(self) -> None:
        self.calls.append("stop")
        self.stop_count += 1
        self.stop_started.set()
        if self.stop_gate is not None:
            await self.stop_gate.wait()
        if self.stop_delay:
            await asyncio.sleep(self.stop_delay)
        if self.stop_error is not None:
            raise self.stop_error
        self._started = False


class JsonLineProtocol:
    """Strict newline-delimited JSON validation for a future helper process."""

    MAX_LINE_BYTES = 4096
    PROTOCOL_VERSION = 1
    _MESSAGE_TYPES = frozenset(
        {
            "HELLO", "CONFIG", "READY", "SET_COLOR", "ACK",
            "SET_BRIGHTNESS", "BRIGHTNESS_ACK", "NACK", "STOP", "STOPPED",
        }
    )

    _SCHEMA: dict[str, tuple[frozenset[str], frozenset[str]]] = {
        "HELLO": (
            frozenset({"type", "protocol", "backend_id", "backend_version"}),
            frozenset(),
        ),
        "CONFIG": (
            frozenset({"type", "lease_id", "hardware_identity", "min_interval_ms"}),
            frozenset(),
        ),
        "READY": (frozenset({"type", "lease_id"}), frozenset()),
        "SET_COLOR": (
            frozenset({"type", "lease_id", "sequence", "rgb"}),
            frozenset(),
        ),
        "ACK": (
            frozenset(
                {
                    "type",
                    "lease_id",
                    "sequence",
                    "confirmed_rgb",
                    "confirmation",
                    "duration_ms",
                }
            ),
            frozenset(),
        ),
        "SET_BRIGHTNESS": (
            frozenset({"type", "lease_id", "sequence", "level"}),
            frozenset(),
        ),
        "BRIGHTNESS_ACK": (
            frozenset(
                {
                    "type",
                    "lease_id",
                    "sequence",
                    "confirmed_level",
                    "confirmation",
                    "duration_ms",
                }
            ),
            frozenset(),
        ),
        "NACK": (
            frozenset({"type", "lease_id", "sequence", "phase", "fatal", "code"}),
            frozenset(),
        ),
        "STOP": (frozenset({"type", "lease_id"}), frozenset()),
        "STOPPED": (frozenset({"type", "lease_id"}), frozenset()),
    }

    @classmethod
    def encode(cls, message: Mapping[str, Any]) -> bytes:
        normalized = dict(message)
        cls.validate_message(normalized)
        try:
            payload = json.dumps(
                normalized,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise ProtocolValidationError(f"message is not valid JSON: {exc}") from exc
        if len(payload) > cls.MAX_LINE_BYTES:
            raise ProtocolValidationError("protocol line exceeds 4096 bytes")
        return payload

    @classmethod
    def decode(cls, line: bytes | str) -> dict[str, Any]:
        if isinstance(line, str):
            try:
                raw = line.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ProtocolValidationError("protocol line is not valid UTF-8") from exc
        elif isinstance(line, bytes):
            raw = line
        else:
            raise ProtocolValidationError("protocol line must be bytes or str")

        if not raw or len(raw) > cls.MAX_LINE_BYTES:
            raise ProtocolValidationError("protocol line is empty or exceeds 4096 bytes")
        if not raw.endswith(b"\n"):
            raise ProtocolValidationError("protocol line must end with a newline")

        payload = raw[:-1]
        if b"\n" in payload or b"\r" in payload:
            raise ProtocolValidationError("exactly one JSON line is required")

        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ProtocolValidationError(f"duplicate protocol field: {key}")
                result[key] = value
            return result

        try:
            text = payload.decode("utf-8")
            message = json.loads(text, object_pairs_hook=reject_duplicate_keys)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolValidationError(f"invalid JSON protocol line: {exc}") from exc
        if not isinstance(message, dict):
            raise ProtocolValidationError("protocol message root must be an object")
        cls.validate_message(message)
        return message

    @classmethod
    def validate_message(cls, message: Mapping[str, Any]) -> None:
        if not isinstance(message, Mapping):
            raise ProtocolValidationError("protocol message root must be an object")
        message_type = message.get("type")
        if not isinstance(message_type, str):
            raise ProtocolValidationError("protocol message type must be a string")
        if message_type not in cls._MESSAGE_TYPES:
            raise ProtocolValidationError("unknown or missing protocol message type")
        if not all(isinstance(key, str) for key in message):
            raise ProtocolValidationError("protocol field names must be strings")

        required, optional = cls._SCHEMA[message_type]
        keys = frozenset(message)
        missing = required - keys
        extra = keys - required - optional
        if missing:
            raise ProtocolValidationError(f"missing protocol fields: {', '.join(sorted(missing))}")
        if extra:
            raise ProtocolValidationError(f"unexpected protocol fields: {', '.join(sorted(extra))}")

        if "lease_id" in message:
            cls._validate_nonempty_string(message["lease_id"], "lease_id", maximum=128)
        if "sequence" in message:
            cls._validate_positive_int(message["sequence"], "sequence")
        if "rgb" in message:
            cls._validate_rgb(message["rgb"], "rgb")
        if "confirmed_rgb" in message:
            cls._validate_rgb(message["confirmed_rgb"], "confirmed_rgb")
        if "level" in message:
            cls._validate_brightness_level(message["level"], "level")
        if "confirmed_level" in message:
            cls._validate_brightness_level(message["confirmed_level"], "confirmed_level")

        if message_type == "HELLO":
            if (
                isinstance(message["protocol"], bool)
                or not isinstance(message["protocol"], int)
                or message["protocol"] != cls.PROTOCOL_VERSION
            ):
                raise ProtocolValidationError("unsupported protocol version")
            cls._validate_nonempty_string(message["backend_id"], "backend_id", maximum=128)
            cls._validate_nonempty_string(message["backend_version"], "backend_version", maximum=128)
        elif message_type == "CONFIG":
            cls._validate_nonempty_string(
                message["hardware_identity"],
                "hardware_identity",
                maximum=256,
            )
            interval = message["min_interval_ms"]
            if isinstance(interval, bool) or not isinstance(interval, int) or interval < 0:
                raise ProtocolValidationError("min_interval_ms must be a non-negative integer")
        elif message_type in {"ACK", "BRIGHTNESS_ACK"}:
            cls._validate_finite_number(message["duration_ms"], "duration_ms", minimum=0)
            if message["confirmation"] != "synchronous_vendor_return_103":
                raise ProtocolValidationError("unsupported vendor confirmation kind")
        elif message_type == "NACK":
            phase = message["phase"]
            if not isinstance(phase, str) or phase not in {
                "pre_apply",
                "post_apply",
                "unknown",
            }:
                raise ProtocolValidationError("invalid NACK phase")
            if not isinstance(message["fatal"], bool):
                raise ProtocolValidationError("fatal must be a boolean")
            cls._validate_nonempty_string(message["code"], "code", maximum=128)

    @staticmethod
    def _validate_nonempty_string(value: Any, name: str, *, maximum: int) -> None:
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise ProtocolValidationError(f"{name} must be a non-empty string of at most {maximum} characters")

    @staticmethod
    def _validate_positive_int(value: Any, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ProtocolValidationError(f"{name} must be a positive integer")

    @staticmethod
    def _validate_finite_number(value: Any, name: str, *, minimum: float) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProtocolValidationError(f"{name} must be numeric")
        if not math.isfinite(float(value)) or float(value) < minimum:
            raise ProtocolValidationError(f"{name} must be finite and at least {minimum}")

    @staticmethod
    def _validate_brightness_level(value: Any, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4:
            raise ProtocolValidationError(f"{name} must be an integer from 0 through 4")

    @staticmethod
    def _validate_rgb(value: Any, name: str) -> None:
        if not isinstance(value, Mapping):
            raise ProtocolValidationError(f"{name} must be an object")
        if frozenset(value) != frozenset({"r", "g", "b"}):
            raise ProtocolValidationError(f"{name} must contain exactly r, g, and b")
        for channel in ("r", "g", "b"):
            channel_value = value[channel]
            if (
                isinstance(channel_value, bool)
                or not isinstance(channel_value, int)
                or not 0 <= channel_value <= 255
            ):
                raise ProtocolValidationError(
                    f"{name}.{channel} must be an integer from 0 through 255"
                )


class KeyboardSyncController:
    """Serialized, default-off controller with fail-closed apply semantics."""

    MINIMUM_INTERVAL_SECONDS = 0.1
    HEALTH_INTERVAL_SECONDS = 1.0
    HELLO_TIMEOUT_SECONDS = 2.0
    OPERATION_TIMEOUT_SECONDS = 1.0
    STOP_TIMEOUT_SECONDS = 2.0
    _PACING_SLEEP_FLOOR_SECONDS = 0.000001

    _ALLOWED_TRANSITIONS: dict[KeyboardSyncState, frozenset[KeyboardSyncState]] = {
        KeyboardSyncState.OEM_ONLY: frozenset(
            {KeyboardSyncState.PROBING, KeyboardSyncState.QUARANTINED}
        ),
        KeyboardSyncState.PROBING: frozenset(
            {KeyboardSyncState.STARTING, KeyboardSyncState.BLOCKED, KeyboardSyncState.QUARANTINED}
        ),
        KeyboardSyncState.STARTING: frozenset(
            {KeyboardSyncState.ACTIVE_IDLE, KeyboardSyncState.BLOCKED, KeyboardSyncState.QUARANTINED}
        ),
        KeyboardSyncState.ACTIVE_IDLE: frozenset(
            {KeyboardSyncState.APPLYING, KeyboardSyncState.DRAINING, KeyboardSyncState.QUARANTINED}
        ),
        KeyboardSyncState.APPLYING: frozenset(
            {KeyboardSyncState.ACTIVE_IDLE, KeyboardSyncState.DRAINING, KeyboardSyncState.QUARANTINED}
        ),
        KeyboardSyncState.DRAINING: frozenset(
            {KeyboardSyncState.VERIFYING, KeyboardSyncState.QUARANTINED}
        ),
        KeyboardSyncState.VERIFYING: frozenset(
            {KeyboardSyncState.OEM_ONLY, KeyboardSyncState.QUARANTINED}
        ),
        KeyboardSyncState.BLOCKED: frozenset({KeyboardSyncState.QUARANTINED}),
        KeyboardSyncState.QUARANTINED: frozenset(),
    }

    def __init__(
        self,
        backend: KeyboardLightingBackend | None = None,
        *,
        min_apply_interval: float = MINIMUM_INTERVAL_SECONDS,
        apply_timeout: float = OPERATION_TIMEOUT_SECONDS,
        probe_timeout: float = HELLO_TIMEOUT_SECONDS,
        start_timeout: float = HELLO_TIMEOUT_SECONDS,
        stop_timeout: float = STOP_TIMEOUT_SECONDS,
        health_interval: float = HEALTH_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        health_sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if min_apply_interval < self.MINIMUM_INTERVAL_SECONDS:
            raise ValueError("min_apply_interval must be at least 0.1 second")
        if not math.isfinite(min_apply_interval):
            raise ValueError("min_apply_interval must be finite")
        if not math.isfinite(apply_timeout) or apply_timeout <= 0:
            raise ValueError("apply_timeout must be finite and positive")
        if not math.isfinite(probe_timeout) or probe_timeout <= 0:
            raise ValueError("probe_timeout must be finite and positive")
        if not math.isfinite(start_timeout) or start_timeout <= 0:
            raise ValueError("start_timeout must be finite and positive")
        if not math.isfinite(stop_timeout) or stop_timeout <= 0:
            raise ValueError("stop_timeout must be finite and positive")
        if not math.isfinite(health_interval) or health_interval < self.HEALTH_INTERVAL_SECONDS:
            raise ValueError("health_interval must be at least 1.0 second")

        self._backend = backend or ClevoIpcBackend()
        self._min_apply_interval = float(min_apply_interval)
        self._apply_timeout = float(apply_timeout)
        self._probe_timeout = float(probe_timeout)
        self._start_timeout = float(start_timeout)
        self._stop_timeout = float(stop_timeout)
        self._health_interval = float(health_interval)
        self._clock = clock
        self._sleep = sleeper
        self._health_sleep = health_sleeper

        self._state = KeyboardSyncState.OEM_ONLY
        self._fault: ControllerFault | None = None
        self._probe: BackendProbe | None = None
        self._start: BackendStart | None = None
        self._lease_id: str | None = None
        self._next_sequence = 1
        self._pending_color: StaticColor | None = None
        self._pending_brightness: int | None = None
        self._applied_color: StaticColor | None = None
        self._applied_brightness: int | None = None
        self._inflight: ApplyRequest | BrightnessRequest | None = None
        self._last_apply_started_at: float | None = None
        # This remains true until broker stop is positively acknowledged. A
        # timed-out or cancelled start/stop therefore cannot be mistaken for a
        # clean OEM-only state.
        self._backend_may_be_running = False
        self._stop_task: asyncio.Task[None] | None = None
        self._stop_attempted = False
        self._stop_error: str | None = None
        self._detached_terminal_tasks: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._apply_task: asyncio.Task[None] | None = None
        self._health_task: asyncio.Task[None] | None = None

        self._state_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._backend_operation_lock = asyncio.Lock()
        self._idle_event = asyncio.Event()
        self._idle_event.set()

    @property
    def state(self) -> KeyboardSyncState:
        return self._state

    @property
    def enabled(self) -> bool:
        return self._state in {KeyboardSyncState.ACTIVE_IDLE, KeyboardSyncState.APPLYING}

    @property
    def pending_color(self) -> StaticColor | None:
        return self._pending_color

    @property
    def applied_color(self) -> StaticColor | None:
        return self._applied_color

    @property
    def pending_brightness(self) -> int | None:
        return self._pending_brightness

    @property
    def applied_brightness(self) -> int | None:
        return self._applied_brightness

    @property
    def inflight(self) -> ApplyRequest | BrightnessRequest | None:
        return self._inflight

    @property
    def fault(self) -> ControllerFault | None:
        return self._fault

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def broker_stop_confirmed(self) -> bool:
        """Whether no isolated backend broker can still be running.

        This is deliberately narrower than a successful disable: lighting
        restore or OEM-health verification may quarantine a session even after
        the broker itself has positively stopped.
        """
        return not self._backend_may_be_running

    @staticmethod
    async def _await_backend_deadline(
        operation: Awaitable[Any],
        timeout: float,
    ) -> Any:
        """Reject late results while preserving serialized operation ordering.

        A qualified broker must propagate cancellation within the negotiated
        bound.  ``asyncio.wait_for`` intentionally remains awaited when a
        defective backend suppresses cancellation, so the caller continues to
        own the operation lock and cleanup cannot overtake a still-running
        write.  The post-return clock check prevents that late value from ever
        being accepted as an ACK or STOPPED response.
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        result = await asyncio.wait_for(operation, timeout=timeout)
        if loop.time() >= deadline:
            raise asyncio.TimeoutError
        return result

    def _observe_detached_terminal_task(self, task: asyncio.Task[Any]) -> None:
        """Discard a terminal/read-only result that arrived after timeout."""

        self._detached_terminal_tasks.discard(task)
        try:
            task.result()
        except BaseException:
            pass

    def _track_detached_terminal_task(self, task: asyncio.Task[Any]) -> None:
        self._detached_terminal_tasks.add(task)
        task.add_done_callback(self._observe_detached_terminal_task)

    async def _await_detachable_deadline(
        self,
        operation: Awaitable[Any],
        timeout: float,
    ) -> Any:
        """Enforce a hard deadline only where detachment cannot reorder writes.

        This is restricted to the pre-ownership read-only probe and to the
        terminal stop transaction, whose task owns the operation lock itself.
        A late result is consumed but can never update controller state.
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        operation_task = asyncio.ensure_future(operation)
        timer_task = asyncio.create_task(asyncio.sleep(timeout))
        try:
            done, _ = await asyncio.wait(
                {operation_task, timer_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            operation_task.cancel()
            timer_task.cancel()
            self._track_detached_terminal_task(operation_task)
            timer_task.add_done_callback(self._observe_detached_terminal_task)
            raise

        if (
            operation_task in done
            and timer_task not in done
            and loop.time() < deadline
        ):
            timer_task.cancel()
            timer_task.add_done_callback(self._observe_detached_terminal_task)
            return operation_task.result()

        operation_task.cancel()
        timer_task.cancel()
        self._track_detached_terminal_task(operation_task)
        timer_task.add_done_callback(self._observe_detached_terminal_task)
        raise asyncio.TimeoutError

    async def enable(self) -> bool:
        """Explicitly probe and start a qualified backend.

        Construction never calls this method; the controller therefore remains
        OEM-only unless a caller deliberately requests enablement.
        """

        async with self._lifecycle_lock:
            async with self._state_lock:
                if self._closed:
                    return False
                if self._state in {KeyboardSyncState.ACTIVE_IDLE, KeyboardSyncState.APPLYING}:
                    return True
                if self._state in {KeyboardSyncState.BLOCKED, KeyboardSyncState.QUARANTINED}:
                    return False
                if self._state is not KeyboardSyncState.OEM_ONLY:
                    return False
                self._fault = None
                self._idle_event.clear()
                self._transition_unlocked(KeyboardSyncState.PROBING)

            try:
                probe = await self._await_detachable_deadline(
                    self._backend.probe(),
                    self._probe_timeout,
                )
            except asyncio.CancelledError:
                async with self._state_lock:
                    if self._state is KeyboardSyncState.PROBING:
                        self._block_unlocked(
                            "PROBE_CANCELLED",
                            "Read-only backend probing was cancelled before qualification.",
                        )
                raise
            except asyncio.TimeoutError:
                async with self._state_lock:
                    self._block_unlocked(
                        "PROBE_TIMEOUT",
                        "Read-only backend probing exceeded its deadline.",
                    )
                return False
            except Exception as exc:
                async with self._state_lock:
                    self._block_unlocked("PROBE_FAILED", str(exc) or type(exc).__name__)
                return False

            if not isinstance(probe, BackendProbe) or not probe.qualified:
                reason = probe.reason if isinstance(probe, BackendProbe) else "Backend returned an invalid probe result."
                async with self._state_lock:
                    self._probe = probe if isinstance(probe, BackendProbe) else None
                    self._block_unlocked("BACKEND_UNAVAILABLE", reason)
                return False

            lease_id = uuid.uuid4().hex
            async with self._state_lock:
                self._probe = probe
                self._lease_id = lease_id
                self._transition_unlocked(KeyboardSyncState.STARTING)
                # Once start is issued, ownership is uncertain until either the
                # handshake succeeds or stop is positively acknowledged.
                self._backend_may_be_running = True

            try:
                async with self._backend_operation_lock:
                    started = await self._await_backend_deadline(
                        self._backend.start(lease_id),
                        self._start_timeout,
                    )

                problem = self._validate_start(probe, started, lease_id)
                if problem is not None:
                    stop_error = await self._stop_backend_once()
                    async with self._state_lock:
                        if stop_error is None:
                            self._block_unlocked("UNSAFE_BACKEND", problem)
                        else:
                            self._quarantine_unlocked(
                                "UNSAFE_BACKEND_STOP_FAILED",
                                f"{problem} Backend stop also failed: {stop_error}",
                            )
                            self._idle_event.set()
                    return False

                async with self._state_lock:
                    self._start = started

                verification_problem = await self._verification_problem(lease_id)
                if verification_problem is not None:
                    stop_error = await self._stop_backend_once()
                    async with self._state_lock:
                        message = (
                            "Initial OEM health verification failed: "
                            f"{verification_problem}"
                        )
                        if stop_error is None:
                            self._block_unlocked("OEM_HEALTH_UNVERIFIED", message)
                        else:
                            self._quarantine_unlocked(
                                "OEM_HEALTH_STOP_FAILED",
                                f"{message}; broker stop also failed: {stop_error}",
                            )
                            self._idle_event.set()
                    return False

                async with self._state_lock:
                    self._transition_unlocked(KeyboardSyncState.ACTIVE_IDLE)
                    self._idle_event.set()
                    self._health_task = asyncio.create_task(
                        self._health_loop(),
                        name="KeyboardSyncHealth",
                    )
                return True
            except asyncio.CancelledError as exc:
                await self._settle_enable_cancellation(exc)
                raise
            except Exception as exc:
                cancelled = await self._settle_start_failure(exc)
                if cancelled:
                    raise asyncio.CancelledError
                return False

    async def request_static_color(self, color: StaticColor) -> bool:
        """Queue a static color without waiting for hardware completion.

        While one request is in flight, only the newest pending color is kept.
        The method returns ``False`` unless the controller is actively enabled.
        """

        if not isinstance(color, StaticColor):
            raise TypeError("color must be a StaticColor")

        async with self._state_lock:
            if self._closed or self._state not in {
                KeyboardSyncState.ACTIVE_IDLE,
                KeyboardSyncState.APPLYING,
            }:
                return False
            self._pending_color = color
            self._idle_event.clear()
            if self._apply_task is None or self._apply_task.done():
                self._apply_task = asyncio.create_task(
                    self._apply_loop(),
                    name="KeyboardSyncApply",
                )
            return True

    async def request_brightness(self, level: int) -> bool:
        """Queue the newest qualified brightness level without blocking the UI."""
        if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 4:
            raise ValueError("level must be an integer from 0 through 4")
        async with self._state_lock:
            if self._closed or self._state not in {
                KeyboardSyncState.ACTIVE_IDLE,
                KeyboardSyncState.APPLYING,
            }:
                return False
            self._pending_brightness = level
            self._idle_event.clear()
            if self._apply_task is None or self._apply_task.done():
                self._apply_task = asyncio.create_task(
                    self._apply_loop(),
                    name="KeyboardSyncApply",
                )
            return True

    async def wait_until_idle(self, timeout: float | None = None) -> KeyboardSyncState:
        waiter = self._idle_event.wait()
        if timeout is None:
            await waiter
        else:
            await asyncio.wait_for(waiter, timeout=timeout)
        return self._state

    async def disable(self) -> bool:
        """Idempotently stop a normal session and restore original lighting."""
        return await self._await_cleanup_task(
            self._disable_serialized(close=False),
            "KeyboardSyncDisable",
        )

    async def shutdown(self) -> bool:
        """Idempotently disable the controller and permanently close it."""
        return await self._await_cleanup_task(
            self._disable_serialized(close=True),
            "KeyboardSyncShutdown",
        )

    async def _disable_serialized(self, *, close: bool) -> bool:
        async with self._lifecycle_lock:
            if self._closed:
                return (
                    self._state is not KeyboardSyncState.QUARANTINED
                    and not self._backend_may_be_running
                )
            result = await self._disable_locked()
            if close:
                async with self._state_lock:
                    self._closed = True
            return result

    @staticmethod
    async def _await_cleanup_task(coroutine: Awaitable[bool], name: str) -> bool:
        """Finish bounded cleanup before honoring caller cancellation."""
        task = asyncio.create_task(coroutine, name=name)
        caller_cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    caller_cancelled = True
                if task.done():
                    break
        if caller_cancelled:
            # Observe any cleanup exception, but cancellation remains the
            # public outcome after the cleanup transaction has settled.
            try:
                task.result()
            except BaseException:
                pass
            raise asyncio.CancelledError
        return task.result()

    async def _apply_loop(self) -> None:
        current_task = asyncio.current_task()
        try:
            while True:
                async with self._state_lock:
                    if self._state not in {
                        KeyboardSyncState.ACTIVE_IDLE,
                        KeyboardSyncState.APPLYING,
                    }:
                        return
                    if self._pending_color is None and self._pending_brightness is None:
                        if self._state is KeyboardSyncState.APPLYING:
                            self._transition_unlocked(KeyboardSyncState.ACTIVE_IDLE)
                        self._idle_event.set()
                        return
                    previous_start = self._last_apply_started_at
                    delay = 0.0
                    if previous_start is not None:
                        delay = max(
                            0.0,
                            self._min_apply_interval - (self._clock() - previous_start),
                        )

                if delay:
                    # A tiny positive remainder can round away in injected
                    # clocks. Sleeping at least one microsecond preserves the
                    # no-earlier-than interval instead of spinning.
                    await self._sleep(max(delay, self._PACING_SLEEP_FLOOR_SECONDS))

                quarantine: tuple[str, str] | None = None
                apply_cancelled = False
                async with self._backend_operation_lock:
                    async with self._state_lock:
                        # A health check or disable may have won the operation
                        # gate while this request waited. Never write based on a
                        # stale ACTIVE observation.
                        if self._state not in {
                            KeyboardSyncState.ACTIVE_IDLE,
                            KeyboardSyncState.APPLYING,
                        }:
                            return
                        color = self._pending_color
                        brightness = self._pending_brightness
                        if color is None and brightness is None:
                            continue
                        lease_id = self._lease_id
                        if lease_id is None:
                            self._pending_color = None
                            self._pending_brightness = None
                            self._quarantine_unlocked(
                                "MISSING_LEASE",
                                "Active controller had no helper lease.",
                            )
                            quarantine = (
                                "MISSING_LEASE",
                                "Active controller had no helper lease.",
                            )
                            request = None
                        else:
                            # Pacing and deadline begin when the backend call
                            # can actually start, not when it first queued
                            # behind a read-only verification.
                            started_at = self._clock()
                            remaining = 0.0
                            if self._last_apply_started_at is not None:
                                remaining = max(
                                    0.0,
                                    self._min_apply_interval
                                    - (started_at - self._last_apply_started_at),
                                )
                            if remaining:
                                request = None
                            else:
                                sequence = self._next_sequence
                                self._next_sequence += 1
                                if color is not None:
                                    self._pending_color = None
                                    request = ApplyRequest(
                                        lease_id=lease_id,
                                        sequence=sequence,
                                        color=color,
                                        deadline_monotonic=started_at + self._apply_timeout,
                                    )
                                else:
                                    self._pending_brightness = None
                                    request = BrightnessRequest(
                                        lease_id=lease_id,
                                        sequence=sequence,
                                        level=brightness,
                                        deadline_monotonic=started_at + self._apply_timeout,
                                    )
                                self._inflight = request
                                self._last_apply_started_at = started_at
                                if self._state is KeyboardSyncState.ACTIVE_IDLE:
                                    self._transition_unlocked(KeyboardSyncState.APPLYING)

                    if quarantine is not None:
                        pass
                    elif request is None:
                        # The operation lock may have been delayed without the
                        # injected monotonic clock advancing. Re-evaluate
                        # pacing outside the lock rather than holding it asleep.
                        continue
                    else:
                        try:
                            operation = (
                                self._backend.apply_static_color(request)
                                if isinstance(request, ApplyRequest)
                                else self._backend.apply_brightness(request)
                            )
                            ack = await self._await_backend_deadline(operation, self._apply_timeout)
                        except asyncio.CancelledError:
                            apply_cancelled = True
                            quarantine = (
                                "APPLY_CANCELLED",
                                "An in-flight apply was cancelled; its outcome is unknown.",
                            )
                        except asyncio.TimeoutError:
                            quarantine = (
                                "APPLY_TIMEOUT",
                                "The backend did not acknowledge the apply before its deadline.",
                            )
                        except Exception as exc:
                            quarantine = (
                                "APPLY_FAILED",
                                "The backend apply outcome is unknown: "
                                f"{str(exc) or type(exc).__name__}",
                            )
                        else:
                            if not self._ack_matches(request, ack):
                                quarantine = (
                                    "ACK_MISMATCH",
                                    "The backend acknowledgment did not match the in-flight request.",
                                )

                        async with self._state_lock:
                            if quarantine is not None:
                                self._pending_color = None
                                self._pending_brightness = None
                                self._inflight = None
                                if self._state is not KeyboardSyncState.QUARANTINED:
                                    self._quarantine_unlocked(*quarantine)
                            else:
                                self._inflight = None
                                if isinstance(ack, ApplyAck):
                                    self._applied_color = ack.color
                                else:
                                    self._applied_brightness = ack.level
                                if self._state is KeyboardSyncState.DRAINING:
                                    return
                                if self._state is not KeyboardSyncState.APPLYING:
                                    return
                                self._transition_unlocked(KeyboardSyncState.ACTIVE_IDLE)
                                if self._pending_color is None and self._pending_brightness is None:
                                    self._idle_event.set()
                                    return

                if quarantine is not None:
                    await self._quarantine_runtime(*quarantine)
                    if apply_cancelled:
                        raise asyncio.CancelledError
                    return
        except asyncio.CancelledError:
            await self._quarantine_runtime(
                "APPLY_WORKER_CANCELLED",
                "The serialized apply worker was cancelled before it could settle safely.",
            )
            raise
        finally:
            async with self._state_lock:
                if self._apply_task is current_task:
                    self._apply_task = None
                if self._state in {
                    KeyboardSyncState.OEM_ONLY,
                    KeyboardSyncState.ACTIVE_IDLE,
                    KeyboardSyncState.BLOCKED,
                } and self._pending_color is None and self._pending_brightness is None and self._inflight is None:
                    self._idle_event.set()

    async def _disable_locked(self) -> bool:
        async with self._state_lock:
            state = self._state
            if (
                state in {KeyboardSyncState.OEM_ONLY, KeyboardSyncState.BLOCKED}
                and not self._backend_may_be_running
            ):
                self._idle_event.set()
                return True

            if state in {KeyboardSyncState.ACTIVE_IDLE, KeyboardSyncState.APPLYING}:
                self._transition_unlocked(KeyboardSyncState.DRAINING)
            elif state not in {
                KeyboardSyncState.DRAINING,
                KeyboardSyncState.VERIFYING,
                KeyboardSyncState.QUARANTINED,
            }:
                self._quarantine_unlocked(
                    "UNEXPECTED_DISABLE_STATE",
                    f"Cleanup began from unexpected state {state.value}.",
                )
            self._pending_color = None
            self._pending_brightness = None
            self._idle_event.clear()
            apply_task = self._apply_task

        await self._stop_health_monitor()

        if apply_task is not None and apply_task is not asyncio.current_task():
            try:
                await asyncio.shield(apply_task)
            except asyncio.CancelledError:
                # An apply task cancelled itself only after quarantining and
                # settling its broker stop. External cancellation of this
                # cleanup is absorbed by the public cleanup wrapper.
                pass
            except Exception as exc:
                await self._quarantine_runtime(
                    "APPLY_WORKER_FAILED",
                    f"The apply worker failed during drain: {str(exc) or type(exc).__name__}",
                )

        async with self._state_lock:
            state = self._state

        if state is KeyboardSyncState.QUARANTINED:
            stop_error = await self._stop_backend_once()
            async with self._state_lock:
                if stop_error is not None:
                    existing = self._fault.message if self._fault else "Session quarantined."
                    self._fault = ControllerFault(
                        code=self._fault.code if self._fault else "QUARANTINED",
                        message=f"{existing} Backend stop also failed: {stop_error}",
                        unknown_outcome=True,
                    )
                self._idle_event.set()
            return False

        if state in {KeyboardSyncState.OEM_ONLY, KeyboardSyncState.BLOCKED}:
            stop_error = await self._stop_backend_once()
            async with self._state_lock:
                if stop_error is not None:
                    self._quarantine_unlocked(
                        "BROKER_STOP_UNCONFIRMED",
                        f"Broker ownership remained uncertain: {stop_error}",
                    )
                    self._idle_event.set()
                    return False
                self._idle_event.set()
            return True

        if state is not KeyboardSyncState.DRAINING:
            async with self._state_lock:
                self._quarantine_unlocked(
                    "INVALID_DRAIN_STATE",
                    f"Cannot safely restore lighting from state {state.value}.",
                )
            stop_error = await self._stop_backend_once()
            async with self._state_lock:
                if stop_error is not None and self._fault is not None:
                    self._fault = ControllerFault(
                        code=self._fault.code,
                        message=f"{self._fault.message} Backend stop also failed: {stop_error}",
                        unknown_outcome=True,
                    )
                self._idle_event.set()
            return False

        async with self._state_lock:
            started = self._start
            lease_id = self._lease_id

        if started is None or lease_id is None:
            restore_problem = "The active session has no captured original lighting state."
        else:
            restore_problem = await self._restore_problem(started)

        stop_error = await self._stop_backend_once()
        if restore_problem is not None or stop_error is not None:
            details = []
            if restore_problem is not None:
                details.append(f"restore failed or was not acknowledged: {restore_problem}")
            if stop_error is not None:
                details.append(f"broker stop failed or timed out: {stop_error}")
            async with self._state_lock:
                self._quarantine_unlocked(
                    "DISABLE_FAILED",
                    "Clean backend shutdown failed: " + "; ".join(details),
                )
                self._idle_event.set()
            return False

        async with self._state_lock:
            self._transition_unlocked(KeyboardSyncState.VERIFYING)
            if (
                self._inflight is not None
                or self._pending_color is not None
                or self._pending_brightness is not None
            ):
                self._quarantine_unlocked(
                    "VERIFY_FAILED",
                    "Software verification found unresolved keyboard-sync state.",
                )
                self._idle_event.set()
                return False

        verification_problem = await self._verification_problem(lease_id)
        if verification_problem is not None:
            async with self._state_lock:
                self._quarantine_unlocked(
                    "POST_STOP_OEM_HEALTH_FAILED",
                    "Post-stop OEM health verification failed: "
                    f"{verification_problem}",
                )
                self._idle_event.set()
            return False

        async with self._state_lock:
            self._transition_unlocked(KeyboardSyncState.OEM_ONLY)
            self._reset_session_unlocked()
            self._idle_event.set()
        return True

    async def _settle_enable_cancellation(self, exc: BaseException) -> None:
        await self._settle_start_failure(
            exc,
            code="ENABLE_CANCELLED",
            message="Backend enablement was cancelled after broker start was issued.",
        )

    async def _settle_start_failure(
        self,
        exc: BaseException,
        *,
        code: str = "START_FAILED",
        message: str = "Backend start outcome is unknown.",
    ) -> bool:
        cleanup = asyncio.create_task(
            self._quarantine_start_failure(
                exc,
                code=code,
                message=message,
            ),
            name="KeyboardSyncEnableFailureCleanup",
        )
        caller_cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    caller_cancelled = True
                continue
            except BaseException:
                break
        cleanup.result()
        return caller_cancelled

    async def _quarantine_start_failure(
        self,
        exc: BaseException,
        *,
        code: str = "START_FAILED",
        message: str = "Backend start outcome is unknown.",
    ) -> None:
        stop_error = await self._stop_backend_once()
        reason = str(exc) or type(exc).__name__
        detail = f" Backend stop also failed: {stop_error}" if stop_error else ""
        async with self._state_lock:
            self._quarantine_unlocked(
                code,
                f"{message} Cause: {reason}.{detail}",
            )
            self._idle_event.set()

    async def _quarantine_runtime(self, code: str, message: str) -> None:
        async with self._state_lock:
            self._pending_color = None
            self._pending_brightness = None
            self._inflight = None
            if self._state is not KeyboardSyncState.QUARANTINED:
                self._quarantine_unlocked(code, message)
        await self._stop_health_monitor()
        stop_error = await self._stop_backend_once()
        async with self._state_lock:
            if stop_error is not None:
                existing = self._fault.message if self._fault else message
                self._fault = ControllerFault(
                    code=code,
                    message=f"{existing} Backend stop also failed: {stop_error}",
                    unknown_outcome=True,
                )
            self._idle_event.set()

    async def _health_loop(self) -> None:
        current_task = asyncio.current_task()
        try:
            while True:
                await self._health_sleep(self._health_interval)
                async with self._state_lock:
                    if self._state not in {
                        KeyboardSyncState.ACTIVE_IDLE,
                        KeyboardSyncState.APPLYING,
                    }:
                        return
                    lease_id = self._lease_id
                if lease_id is None:
                    await self._quarantine_runtime(
                        "HEALTH_MISSING_LEASE",
                        "Periodic OEM health verification had no active lease.",
                    )
                    return
                problem = await self._periodic_verification_problem(lease_id)
                if problem is not None:
                    await self._quarantine_runtime(
                        "OEM_HEALTH_LOST",
                        f"Periodic OEM health verification failed: {problem}",
                    )
                    return
        except asyncio.CancelledError:
            async with self._state_lock:
                active_when_cancelled = self._state in {
                    KeyboardSyncState.ACTIVE_IDLE,
                    KeyboardSyncState.APPLYING,
                }
            if active_when_cancelled:
                await self._quarantine_runtime(
                    "HEALTH_MONITOR_CANCELLED",
                    "The OEM health monitor stopped while keyboard sync was active.",
                )
            raise
        except Exception as exc:
            await self._quarantine_runtime(
                "HEALTH_MONITOR_FAILED",
                f"OEM health monitoring failed: {str(exc) or type(exc).__name__}",
            )
        finally:
            async with self._state_lock:
                if self._health_task is current_task:
                    self._health_task = None

    async def _stop_health_monitor(self) -> None:
        current_task = asyncio.current_task()
        async with self._state_lock:
            task = self._health_task
        if task is None or task is current_task:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            # The health loop converts operational failures into quarantine.
            pass
        async with self._state_lock:
            if self._health_task is task:
                self._health_task = None

    async def _verification_problem(self, lease_id: str) -> str | None:
        async with self._backend_operation_lock:
            return await self._verification_problem_locked(lease_id)

    async def _periodic_verification_problem(self, lease_id: str) -> str | None:
        """Verify and revoke write permission before releasing the operation slot."""
        async with self._backend_operation_lock:
            problem = await self._verification_problem_locked(lease_id)
            if problem is None:
                return None
            async with self._state_lock:
                if self._state not in {
                    KeyboardSyncState.ACTIVE_IDLE,
                    KeyboardSyncState.APPLYING,
                }:
                    return None
                self._pending_color = None
                self._pending_brightness = None
                self._inflight = None
                self._quarantine_unlocked(
                    "OEM_HEALTH_LOST",
                    f"Periodic OEM health verification failed: {problem}",
                )
            return problem

    async def _verification_problem_locked(self, lease_id: str) -> str | None:
        try:
            result = await self._await_backend_deadline(
                self._backend.verify_oem_health(lease_id),
                self._apply_timeout,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return "the read-only OEM health check timed out"
        except Exception as exc:
            return "the read-only OEM health check failed: " + (
                str(exc) or type(exc).__name__
            )
        return self._validate_verification(result, lease_id)

    async def _restore_problem(self, started: BackendStart) -> str | None:
        try:
            async with self._backend_operation_lock:
                result = await self._await_backend_deadline(
                    self._backend.restore_original_lighting(),
                    self._stop_timeout,
                )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return "the restore operation timed out with an unknown result"
        except Exception as exc:
            return str(exc) or type(exc).__name__
        if not isinstance(result, RestoreAck):
            return "the backend returned an invalid restore acknowledgment"
        if result.lease_id != started.lease_id:
            return "the restore acknowledgment used the wrong lease"
        if result.confirmed_color != started.original_color:
            return "the vendor-confirmed restored color did not match the pre-sync color"
        return None

    async def _bounded_backend_stop(self) -> None:
        async def serialized_stop() -> None:
            async with self._backend_operation_lock:
                await self._backend.stop()
        await self._await_detachable_deadline(serialized_stop(), self._stop_timeout)

    async def _stop_backend_once(self) -> str | None:
        async with self._state_lock:
            if not self._backend_may_be_running:
                return None
            if not self._stop_attempted:
                self._stop_attempted = True
                self._stop_error = None
                self._stop_task = asyncio.create_task(
                    self._bounded_backend_stop(),
                    name="KeyboardSyncBrokerStop",
                )
            task = self._stop_task
            previous_error = self._stop_error

        if task is None:
            return previous_error or "the previous broker stop outcome is unknown"

        caller_cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    caller_cancelled = True
                if task.done():
                    break
            except BaseException:
                # The completed task's result is normalized below so a stop
                # timeout/error becomes a quarantined outcome, not an escape.
                break

        try:
            task.result()
            stop_error = None
        except BaseException as exc:
            stop_error = str(exc) or type(exc).__name__

        async with self._state_lock:
            if self._stop_task is task:
                self._stop_task = None
                self._stop_error = stop_error
                if stop_error is None:
                    self._backend_may_be_running = False
            elif self._backend_may_be_running:
                stop_error = self._stop_error or stop_error
            else:
                stop_error = None

        if caller_cancelled:
            raise asyncio.CancelledError
        return stop_error

    def _validate_start(
        self,
        probe: BackendProbe,
        started: BackendStart,
        lease_id: str,
    ) -> str | None:
        if not isinstance(started, BackendStart):
            return "Backend returned an invalid start acknowledgment."
        if started.lease_id != lease_id:
            return "Backend start acknowledgment used the wrong lease."
        if started.backend_id != probe.backend_id or started.backend_version != probe.backend_version:
            return "Backend identity changed between probe and start."
        if not isinstance(started.original_color, StaticColor):
            return "Backend did not capture a valid original static color."
        if not isinstance(started.capabilities, frozenset) or not all(
            isinstance(capability, str) and capability for capability in started.capabilities
        ):
            return "Backend returned an invalid capability declaration."
        missing = REQUIRED_BACKEND_CAPABILITIES - started.capabilities
        if missing:
            return f"Backend lacks required safety capabilities: {', '.join(sorted(missing))}."
        return None

    @staticmethod
    def _validate_verification(
        result: Any,
        lease_id: str,
    ) -> str | None:
        if not isinstance(result, BackendVerification):
            return "the backend returned invalid OEM health evidence"
        if result.lease_id != lease_id:
            return "the OEM health evidence used the wrong lease"
        failed = [
            field_name
            for field_name in (
                "receiver_identity_verified",
                "fnkey_resident",
                "fnkey_identity_verified",
                "required_services_running",
                "package_identities_unchanged",
                "cpu_profile_control_available",
                "oem_lighting_owner_resident",
            )
            if not getattr(result, field_name)
        ]
        if failed:
            return "unsafe OEM health evidence: " + ", ".join(failed)
        return None

    @staticmethod
    def _ack_matches(request: ApplyRequest | BrightnessRequest, ack: Any) -> bool:
        if isinstance(request, ApplyRequest):
            return (
                isinstance(ack, ApplyAck)
                and ack.lease_id == request.lease_id
                and ack.sequence == request.sequence
                and ack.color == request.color
            )
        return (
            isinstance(ack, BrightnessAck)
            and ack.lease_id == request.lease_id
            and ack.sequence == request.sequence
            and ack.level == request.level
        )

    def _block_unlocked(self, code: str, message: str) -> None:
        self._fault = ControllerFault(code=code, message=message, unknown_outcome=False)
        self._transition_unlocked(KeyboardSyncState.BLOCKED)
        self._idle_event.set()

    def _quarantine_unlocked(self, code: str, message: str) -> None:
        self._fault = ControllerFault(code=code, message=message, unknown_outcome=True)
        self._idle_event.clear()
        if self._state is not KeyboardSyncState.QUARANTINED:
            self._transition_unlocked(KeyboardSyncState.QUARANTINED)

    def _transition_unlocked(self, new_state: KeyboardSyncState) -> None:
        if new_state is self._state:
            return
        allowed = self._ALLOWED_TRANSITIONS[self._state]
        if new_state not in allowed:
            raise KeyboardSyncError(
                f"invalid keyboard-sync transition: {self._state.value} -> {new_state.value}"
            )
        self._state = new_state

    def _reset_session_unlocked(self) -> None:
        self._probe = None
        self._start = None
        self._lease_id = None
        self._next_sequence = 1
        self._pending_color = None
        self._pending_brightness = None
        self._applied_color = None
        self._applied_brightness = None
        self._inflight = None
        self._last_apply_started_at = None
        self._backend_may_be_running = False
        self._stop_task = None
        self._stop_attempted = False
        self._stop_error = None
        self._fault = None


__all__ = [
    "ApplyAck",
    "ApplyRequest",
    "BrightnessAck",
    "BrightnessRequest",
    "BackendProbe",
    "BackendStart",
    "BackendVerification",
    "BackendUnavailableError",
    "CLEVO_TARGET_ALLOWLIST",
    "ClevoIpcBackend",
    "ClevoTarget",
    "ControllerFault",
    "FakeBackend",
    "JsonLineProtocol",
    "KeyboardLightingBackend",
    "KeyboardSyncController",
    "KeyboardSyncError",
    "KeyboardSyncState",
    "ProtocolValidationError",
    "ReadOnlySnapshotProvider",
    "ReadOnlySystemSnapshot",
    "REQUIRED_BACKEND_CAPABILITIES",
    "RestoreAck",
    "StaticSnapshotProvider",
    "StaticColor",
]
