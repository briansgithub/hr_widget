import asyncio
import time
import unittest
from dataclasses import replace
from pathlib import Path

from keyboard_sync import (
    ApplyAck,
    ApplyRequest,
    BackendStart,
    BackendVerification,
    BackendUnavailableError,
    CLEVO_TARGET_ALLOWLIST,
    ClevoIpcBackend,
    FakeBackend,
    JsonLineProtocol,
    KeyboardSyncController,
    KeyboardSyncState,
    ProtocolValidationError,
    ReadOnlySystemSnapshot,
    REQUIRED_BACKEND_CAPABILITIES,
    RestoreAck,
    StaticSnapshotProvider,
    StaticColor,
)


class ManualClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.now += duration
        await asyncio.sleep(0)


def snapshot_for_target(target):
    return ReadOnlySystemSnapshot(
        system_manufacturer=target.system_manufacturer,
        system_product_name=target.system_product_name,
        baseboard_product=target.baseboard_product,
        publisher_id=target.publisher_id,
        fnkey_package_name=target.fnkey_package_name,
        fnkey_package_version=target.fnkey_package_version,
        fnkey_package_status=target.fnkey_package_status,
        fnkey_pid=1234,
        fnkey_executable_path=target.fnkey_executable_path,
        led_package_name=target.led_package_name,
        led_package_version=target.led_package_version,
        led_package_status=target.led_package_status,
        control_center_package_name=target.control_center_package_name,
        control_center_package_version=target.control_center_package_version,
        control_center_package_status=target.control_center_package_status,
        running_processes=frozenset({target.fnkey_process_name}),
        running_services=target.required_services,
    )


class KeyboardSyncControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_is_oem_only_and_hardware_free(self) -> None:
        backend = FakeBackend()
        controller = KeyboardSyncController(backend)

        self.assertEqual(controller.state, KeyboardSyncState.OEM_ONLY)
        self.assertFalse(controller.enabled)
        self.assertEqual(backend.calls, [])
        self.assertFalse(await controller.request_static_color(StaticColor(1, 2, 3)))
        self.assertEqual(backend.calls, [])

    async def test_unqualified_backend_blocks_without_starting(self) -> None:
        backend = FakeBackend(qualified=False)
        controller = KeyboardSyncController(backend)

        self.assertFalse(await controller.enable())
        self.assertEqual(controller.state, KeyboardSyncState.BLOCKED)
        self.assertEqual(backend.probe_count, 1)
        self.assertEqual(backend.start_count, 0)
        self.assertFalse(controller.fault.unknown_outcome)
        self.assertTrue(await controller.disable())
        self.assertEqual(backend.stop_count, 0)

    async def test_clevo_backend_is_deliberately_fail_closed(self) -> None:
        backend = ClevoIpcBackend()
        probe = await backend.probe()

        self.assertFalse(probe.qualified)
        with self.assertRaises(BackendUnavailableError):
            await backend.start("lease")
        with self.assertRaises(BackendUnavailableError):
            await backend.apply_static_color(
                ApplyRequest("lease", 1, StaticColor(1, 2, 3), 1.0)
            )
        with self.assertRaises(BackendUnavailableError):
            await backend.restore_original_lighting()
        self.assertIsNone(await backend.stop())

    async def test_exact_snapshot_matches_allowlist_but_ipc_remains_unqualified(self) -> None:
        target = CLEVO_TARGET_ALLOWLIST[0]
        snapshot = snapshot_for_target(target)
        provider = StaticSnapshotProvider(snapshot)
        backend = ClevoIpcBackend(provider)

        probe = await backend.probe()

        self.assertEqual(provider.calls, 1)
        self.assertIs(backend.last_snapshot, snapshot)
        self.assertEqual(backend.matching_target, target)
        self.assertFalse(probe.qualified)
        self.assertIn("explicitly enables", probe.reason)

    async def test_snapshot_mismatch_is_blocked_without_starting(self) -> None:
        target = CLEVO_TARGET_ALLOWLIST[0]
        provider = StaticSnapshotProvider(
            replace(snapshot_for_target(target), baseboard_product="different-board")
        )
        backend = ClevoIpcBackend(provider)
        controller = KeyboardSyncController(backend)

        self.assertFalse(await controller.enable())
        self.assertEqual(controller.state, KeyboardSyncState.BLOCKED)
        self.assertIsNone(backend.matching_target)
        self.assertIn("allowlist", controller.fault.message)

    async def test_package_service_and_fnkey_path_changes_each_block(self) -> None:
        target = CLEVO_TARGET_ALLOWLIST[0]
        exact = snapshot_for_target(target)
        mismatches = {
            "package version": replace(exact, led_package_version="1.14.0.1"),
            "service loss": replace(
                exact,
                running_services=frozenset({"CCDCHUService"}),
            ),
            "FnKey path": replace(exact, fnkey_executable_path=r"C:\unexpected\FnKey.exe"),
        }

        for label, snapshot in mismatches.items():
            with self.subTest(label=label):
                backend = ClevoIpcBackend(StaticSnapshotProvider(snapshot))
                controller = KeyboardSyncController(backend)
                self.assertFalse(await controller.enable())
                self.assertEqual(controller.state, KeyboardSyncState.BLOCKED)
                self.assertIsNone(backend.matching_target)

    async def test_enable_requires_all_safety_capabilities(self) -> None:
        capabilities = REQUIRED_BACKEND_CAPABILITIES - {"coexists_with_oem"}
        backend = FakeBackend(capabilities=capabilities)
        controller = KeyboardSyncController(backend)

        self.assertFalse(await controller.enable())
        self.assertEqual(controller.state, KeyboardSyncState.BLOCKED)
        self.assertEqual(backend.start_count, 1)
        self.assertEqual(backend.stop_count, 1)
        self.assertIn("coexists_with_oem", controller.fault.message)

    async def test_cancel_during_slow_probe_leaves_stable_blocked_state(self) -> None:
        class SlowProbeBackend(FakeBackend):
            def __init__(self) -> None:
                super().__init__()
                self.probe_started = asyncio.Event()
                self.probe_gate = asyncio.Event()

            async def probe(self):
                self.probe_started.set()
                await self.probe_gate.wait()
                return await super().probe()

        backend = SlowProbeBackend()
        controller = KeyboardSyncController(backend, probe_timeout=60.0)
        enabling = asyncio.create_task(controller.enable())
        await asyncio.wait_for(backend.probe_started.wait(), timeout=1.0)

        enabling.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await enabling

        self.assertEqual(controller.state, KeyboardSyncState.BLOCKED)
        self.assertEqual(controller.fault.code, "PROBE_CANCELLED")
        self.assertEqual(backend.start_count, 0)
        self.assertEqual(backend.stop_count, 0)
        self.assertFalse(await controller.request_static_color(StaticColor(1, 2, 3)))
        self.assertEqual(
            await controller.wait_until_idle(timeout=1.0),
            KeyboardSyncState.BLOCKED,
        )

    async def test_late_probe_result_cannot_start_backend(self) -> None:
        class CancellationResistantProbeBackend(FakeBackend):
            def __init__(self) -> None:
                super().__init__()
                self.release_late_probe = asyncio.Event()
                self.probe_cancellation_seen = asyncio.Event()
                self.late_probe_returned = asyncio.Event()

            async def probe(self):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.probe_cancellation_seen.set()
                    await self.release_late_probe.wait()
                result = await super().probe()
                self.late_probe_returned.set()
                return result

        backend = CancellationResistantProbeBackend()
        controller = KeyboardSyncController(backend, probe_timeout=0.01)

        self.assertFalse(await asyncio.wait_for(controller.enable(), timeout=0.5))
        self.assertEqual(controller.state, KeyboardSyncState.BLOCKED)
        self.assertEqual(controller.fault.code, "PROBE_TIMEOUT")
        self.assertEqual(backend.start_count, 0)

        backend.release_late_probe.set()
        await asyncio.wait_for(backend.late_probe_returned.wait(), timeout=0.5)
        await asyncio.sleep(0)

        self.assertEqual(controller.state, KeyboardSyncState.BLOCKED)
        self.assertEqual(backend.start_count, 0)

    async def test_cancel_during_slow_start_cleans_up_and_quarantines(self) -> None:
        class SlowStartBackend(FakeBackend):
            def __init__(self) -> None:
                super().__init__()
                self.start_waiting = asyncio.Event()
                self.start_gate = asyncio.Event()

            async def start(self, lease_id: str) -> BackendStart:
                started = await super().start(lease_id)
                self.start_waiting.set()
                await self.start_gate.wait()
                return started

        backend = SlowStartBackend()
        controller = KeyboardSyncController(backend, start_timeout=60.0)
        enabling = asyncio.create_task(controller.enable())
        await asyncio.wait_for(backend.start_waiting.wait(), timeout=1.0)

        enabling.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await enabling

        self.assertEqual(controller.state, KeyboardSyncState.QUARANTINED)
        self.assertEqual(controller.fault.code, "ENABLE_CANCELLED")
        self.assertEqual(backend.stop_count, 1)
        self.assertFalse(backend._started)
        self.assertFalse(await controller.request_static_color(StaticColor(1, 2, 3)))

    async def test_late_start_result_is_rejected_before_stop(self) -> None:
        class CancellationResistantStartBackend(FakeBackend):
            def __init__(self) -> None:
                super().__init__()
                self.release_late_start = asyncio.Event()
                self.start_cancellation_seen = asyncio.Event()
                self.late_start_returned = asyncio.Event()

            async def start(self, lease_id: str) -> BackendStart:
                self.calls.append("start")
                self.start_count += 1
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.start_cancellation_seen.set()
                    await self.release_late_start.wait()
                self._started = True
                self._lease_id = lease_id
                self.late_start_returned.set()
                return BackendStart(
                    lease_id=lease_id,
                    backend_id=self.backend_id,
                    backend_version=self.backend_version,
                    capabilities=self.capabilities,
                    original_color=self.original_color,
                )

        backend = CancellationResistantStartBackend()
        controller = KeyboardSyncController(backend, start_timeout=0.01)
        enabling = asyncio.create_task(controller.enable())
        await asyncio.wait_for(backend.start_cancellation_seen.wait(), timeout=0.5)

        self.assertFalse(enabling.done())
        self.assertEqual(backend.stop_count, 0)

        backend.release_late_start.set()
        self.assertFalse(await asyncio.wait_for(enabling, timeout=0.5))

        self.assertEqual(controller.state, KeyboardSyncState.QUARANTINED)
        self.assertEqual(controller.fault.code, "START_FAILED")
        self.assertEqual(backend.start_count, 1)
        self.assertEqual(backend.stop_count, 1)
        self.assertFalse(backend._started)
        self.assertLess(backend.calls.index("start"), backend.calls.index("stop"))

    async def test_cancel_during_start_error_cleanup_still_settles_state(self) -> None:
        stop_gate = asyncio.Event()
        backend = FakeBackend(
            start_error=RuntimeError("start failed"),
            stop_gate=stop_gate,
        )
        controller = KeyboardSyncController(backend, stop_timeout=60.0)
        enabling = asyncio.create_task(controller.enable())
        await asyncio.wait_for(backend.stop_started.wait(), timeout=1.0)

        enabling.cancel()
        await asyncio.sleep(0)
        self.assertFalse(enabling.done())
        stop_gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await enabling

        self.assertEqual(controller.state, KeyboardSyncState.QUARANTINED)
        self.assertEqual(controller.fault.code, "START_FAILED")
        self.assertEqual(backend.stop_count, 1)
        self.assertEqual(
            await controller.wait_until_idle(timeout=1.0),
            KeyboardSyncState.QUARANTINED,
        )

    async def test_cancel_during_slow_stop_finishes_cleanup_before_propagating(self) -> None:
        class SlowStopBackend(FakeBackend):
            def __init__(self) -> None:
                super().__init__(stop_gate=asyncio.Event())
                self.stop_started = asyncio.Event()
                self.stop_completed = asyncio.Event()

            async def stop(self) -> None:
                self.stop_started.set()
                await super().stop()
                self.stop_completed.set()

        backend = SlowStopBackend()
        controller = KeyboardSyncController(backend, stop_timeout=60.0)
        self.assertTrue(await controller.enable())

        shutting_down = asyncio.create_task(controller.shutdown())
        await asyncio.wait_for(backend.stop_started.wait(), timeout=1.0)
        shutting_down.cancel()
        await asyncio.sleep(0)

        self.assertFalse(shutting_down.done())
        self.assertFalse(backend.stop_completed.is_set())
        self.assertNotEqual(controller.state, KeyboardSyncState.OEM_ONLY)

        backend.stop_gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await shutting_down

        self.assertTrue(backend.stop_completed.is_set())
        self.assertFalse(backend._started)
        self.assertEqual(controller.state, KeyboardSyncState.OEM_ONLY)
        self.assertTrue(controller.closed)
        self.assertEqual(backend.stop_count, 1)
        self.assertTrue(await controller.shutdown())

    async def test_stop_timeout_is_quarantined_and_never_retried(self) -> None:
        backend = FakeBackend(stop_gate=asyncio.Event())
        controller = KeyboardSyncController(backend, stop_timeout=0.01)
        self.assertTrue(await controller.enable())

        self.assertFalse(await controller.disable())
        self.assertEqual(controller.state, KeyboardSyncState.QUARANTINED)
        self.assertEqual(backend.stop_count, 1)

        self.assertFalse(await controller.disable())
        self.assertEqual(backend.stop_count, 1)

    async def test_late_stop_result_is_never_accepted_as_confirmed(self) -> None:
        class CancellationResistantStopBackend(FakeBackend):
            def __init__(self) -> None:
                super().__init__()
                self.release_late_stop = asyncio.Event()
                self.stop_cancellation_seen = asyncio.Event()
                self.late_stop_returned = asyncio.Event()

            async def stop(self) -> None:
                self.calls.append("stop")
                self.stop_count += 1
                self.stop_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.stop_cancellation_seen.set()
                    await self.release_late_stop.wait()
                self._started = False
                self.late_stop_returned.set()

        backend = CancellationResistantStopBackend()
        controller = KeyboardSyncController(backend, stop_timeout=0.01)
        self.assertTrue(await controller.enable())

        disabling = asyncio.create_task(controller.disable())
        await asyncio.wait_for(backend.stop_cancellation_seen.wait(), timeout=0.5)
        self.assertFalse(await asyncio.wait_for(disabling, timeout=0.5))
        self.assertEqual(controller.state, KeyboardSyncState.QUARANTINED)
        self.assertFalse(controller.broker_stop_confirmed)
        self.assertEqual(backend.stop_count, 1)

        backend.release_late_stop.set()
        await asyncio.wait_for(backend.late_stop_returned.wait(), timeout=0.5)

        self.assertFalse(controller.broker_stop_confirmed)
        self.assertEqual(controller.state, KeyboardSyncState.QUARANTINED)
        self.assertFalse(await controller.disable())
        self.assertEqual(backend.stop_count, 1)

    async def test_blocking_late_stop_is_never_accepted_as_confirmed(self) -> None:
        class BlockingStopBackend(FakeBackend):
            async def stop(self) -> None:
                self.calls.append("stop")
                self.stop_count += 1
                self.stop_started.set()
                time.sleep(0.05)
                self._started = False

        backend = BlockingStopBackend()
        controller = KeyboardSyncController(backend, stop_timeout=0.01)
        self.assertTrue(await controller.enable())

        self.assertFalse(await controller.disable())

        self.assertEqual(controller.state, KeyboardSyncState.QUARANTINED)
        self.assertFalse(controller.broker_stop_confirmed)
        self.assertEqual(backend.stop_count, 1)
        self.assertFalse(await controller.disable())
        self.assertEqual(backend.stop_count, 1)

    async def test_wait_until_idle_does_not_settle_before_quarantine_stop(self) -> None:
        class SlowStopBackend(FakeBackend):
            def __init__(self) -> None:
                super().__init__(
                    apply_error=RuntimeError("uncertain transport failure"),
                    stop_gate=asyncio.Event(),
                )
                self.stop_started = asyncio.Event()
                self.stop_completed = asyncio.Event()

            async def stop(self) -> None:
                self.stop_started.set()
                await super().stop()
                self.stop_completed.set()

        backend = SlowStopBackend()
        controller = KeyboardSyncController(backend, stop_timeout=60.0)
        self.assertTrue(await controller.enable())
        self.assertTrue(await controller.request_static_color(StaticColor(1, 2, 3)))
        await asyncio.wait_for(backend.stop_started.wait(), timeout=1.0)

        settled = asyncio.create_task(controller.wait_until_idle())
        await asyncio.sleep(0)
        self.assertFalse(settled.done())
        self.assertFalse(backend.stop_completed.is_set())

        backend.stop_gate.set()
        self.assertEqual(
            await asyncio.wait_for(settled, timeout=1.0),
            KeyboardSyncState.QUARANTINED,
        )
        self.assertTrue(backend.stop_completed.is_set())

    async def test_initial_typed_health_failure_blocks_writes(self) -> None:
        backend = FakeBackend(health_ok=False)
        controller = KeyboardSyncController(backend)

        self.assertFalse(await controller.enable())
        self.assertEqual(controller.state, KeyboardSyncState.BLOCKED)
        self.assertEqual(controller.fault.code, "OEM_HEALTH_UNVERIFIED")
        self.assertEqual(backend.health_count, 1)
        self.assertEqual(backend.stop_count, 1)
        self.assertEqual(backend.applied_requests, [])
        self.assertFalse(await controller.request_static_color(StaticColor(1, 2, 3)))

    async def test_restore_confirmation_mismatch_quarantines(self) -> None:
        class MismatchedRestoreBackend(FakeBackend):
            async def restore_original_lighting(self) -> RestoreAck:
                acknowledged = await super().restore_original_lighting()
                return RestoreAck(
                    lease_id=acknowledged.lease_id,
                    confirmed_color=StaticColor(9, 9, 9),
                )

        original = StaticColor(1, 2, 3)
        backend = MismatchedRestoreBackend(original_color=original)
        controller = KeyboardSyncController(backend)

        self.assertTrue(await controller.enable())
        self.assertFalse(await controller.disable())

        self.assertEqual(controller.state, KeyboardSyncState.QUARANTINED)

    async def test_restore_uses_stop_timeout_not_color_apply_timeout(self) -> None:
        class DelayedRestoreBackend(FakeBackend):
            async def restore_original_lighting(self) -> RestoreAck:
                await asyncio.sleep(0.03)
                return await super().restore_original_lighting()

        backend = DelayedRestoreBackend()
        controller = KeyboardSyncController(
            backend,
            apply_timeout=0.01,
            stop_timeout=0.10,
        )
        self.assertTrue(await controller.enable())
        self.assertTrue(await controller.request_static_color(StaticColor(1, 2, 3)))
        await controller.wait_until_idle(timeout=1.0)

        self.assertTrue(await controller.disable())
        self.assertEqual(controller.state, KeyboardSyncState.OEM_ONLY)
        self.assertTrue(controller.broker_stop_confirmed)
        self.assertEqual(backend.restore_count, 1)
        self.assertEqual(backend.stop_count, 1)

    async def test_late_restore_ack_is_rejected_before_stop(self) -> None:
        class CancellationResistantRestoreBackend(FakeBackend):
            def __init__(self) -> None:
                super().__init__()
                self.release_late_restore = asyncio.Event()
                self.restore_cancellation_seen = asyncio.Event()

            async def restore_original_lighting(self) -> RestoreAck:
                self.calls.append("restore")
                self.restore_count += 1
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.restore_cancellation_seen.set()
                    await self.release_late_restore.wait()
                self.current_color = self.original_color
                return RestoreAck(
                    lease_id=self._lease_id,
                    confirmed_color=self.original_color,
                )

        backend = CancellationResistantRestoreBackend()
        controller = KeyboardSyncController(backend, stop_timeout=0.01)
        self.assertTrue(await controller.enable())

        disabling = asyncio.create_task(controller.disable())
        await asyncio.wait_for(backend.restore_cancellation_seen.wait(), timeout=0.5)
        self.assertFalse(disabling.done())
        self.assertEqual(backend.stop_count, 0)

        backend.release_late_restore.set()
        self.assertFalse(await asyncio.wait_for(disabling, timeout=0.5))

        self.assertEqual(controller.state, KeyboardSyncState.QUARANTINED)
        self.assertEqual(controller.fault.code, "DISABLE_FAILED")
        self.assertEqual(backend.restore_count, 1)
        self.assertEqual(backend.stop_count, 1)
        self.assertLess(backend.calls.index("restore"), backend.calls.index("stop"))

    async def test_runtime_typed_health_loss_quarantines(self) -> None:
        class RuntimeHealthBackend(FakeBackend):
            async def verify_oem_health(self, lease_id: str) -> BackendVerification:
                return await super().verify_oem_health(lease_id)

        class HealthTick:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def sleep(self, duration: float) -> None:
                self.started.set()
                await self.release.wait()

        backend = RuntimeHealthBackend()
        tick = HealthTick()
        controller = KeyboardSyncController(
            backend,
            health_interval=1.0,
            health_sleeper=tick.sleep,
        )
        self.assertTrue(await controller.enable())
        await asyncio.wait_for(tick.started.wait(), timeout=1.0)

        backend.health_ok = False
        tick.release.set()

        async def wait_for_quarantine() -> None:
            while controller.state is not KeyboardSyncState.QUARANTINED:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_quarantine(), timeout=1.0)
        self.assertEqual(
            await controller.wait_until_idle(timeout=1.0),
            KeyboardSyncState.QUARANTINED,
        )
        self.assertGreaterEqual(backend.health_count, 2)
        self.assertEqual(backend.stop_count, 1)
        self.assertEqual(backend.applied_requests, [])
        self.assertFalse(await controller.request_static_color(StaticColor(1, 2, 3)))

    async def test_failed_periodic_health_check_revokes_queued_write(self) -> None:
        class OneHealthTick:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.never = asyncio.Event()
                self.calls = 0

            async def sleep(self, _duration: float) -> None:
                self.calls += 1
                if self.calls == 1:
                    self.started.set()
                    await self.release.wait()
                else:
                    await self.never.wait()

        class GatedHealthBackend(FakeBackend):
            def __init__(self) -> None:
                super().__init__()
                self.runtime_health_started = asyncio.Event()
                self.runtime_health_gate = asyncio.Event()

            async def verify_oem_health(self, lease_id: str) -> BackendVerification:
                if self.health_count >= 1:
                    self.runtime_health_started.set()
                    await self.runtime_health_gate.wait()
                    self.health_ok = False
                return await super().verify_oem_health(lease_id)

        tick = OneHealthTick()
        backend = GatedHealthBackend()
        controller = KeyboardSyncController(
            backend,
            health_sleeper=tick.sleep,
        )
        self.assertTrue(await controller.enable())
        await asyncio.wait_for(tick.started.wait(), timeout=1.0)
        tick.release.set()
        await asyncio.wait_for(backend.runtime_health_started.wait(), timeout=1.0)

        self.assertTrue(await controller.request_static_color(StaticColor(1, 2, 3)))
        backend.runtime_health_gate.set()

        self.assertEqual(
            await controller.wait_until_idle(timeout=1.0),
            KeyboardSyncState.QUARANTINED,
        )
        self.assertEqual(backend.applied_requests, [])
        self.assertEqual(backend.stop_count, 1)

    async def test_late_periodic_health_result_cannot_authorize_queued_write(self) -> None:
        class ImmediateHealthTick:
            def __init__(self) -> None:
                self.calls = 0
                self.never = asyncio.Event()

            async def sleep(self, _duration: float) -> None:
                self.calls += 1
                if self.calls > 1:
                    await self.never.wait()

        class CancellationResistantHealthBackend(FakeBackend):
            def __init__(self) -> None:
                super().__init__()
                self.release_late_health = asyncio.Event()
                self.health_cancellation_seen = asyncio.Event()

            async def verify_oem_health(self, lease_id: str) -> BackendVerification:
                if self.health_count == 0:
                    return await super().verify_oem_health(lease_id)
                self.calls.append("verify")
                self.health_count += 1
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.health_cancellation_seen.set()
                    await self.release_late_health.wait()
                return BackendVerification(
                    lease_id=lease_id,
                    receiver_identity_verified=True,
                    fnkey_resident=True,
                    fnkey_identity_verified=True,
                    required_services_running=True,
                    package_identities_unchanged=True,
                    cpu_profile_control_available=True,
                    oem_lighting_owner_resident=True,
                )

        tick = ImmediateHealthTick()
        backend = CancellationResistantHealthBackend()
        controller = KeyboardSyncController(
            backend,
            apply_timeout=0.01,
            health_sleeper=tick.sleep,
        )
        self.assertTrue(await controller.enable())
        await asyncio.wait_for(backend.health_cancellation_seen.wait(), timeout=0.5)

        self.assertTrue(await controller.request_static_color(StaticColor(1, 2, 3)))
        self.assertEqual(backend.applied_requests, [])
        self.assertEqual(backend.stop_count, 0)

        backend.release_late_health.set()
        self.assertEqual(
            await controller.wait_until_idle(timeout=0.5),
            KeyboardSyncState.QUARANTINED,
        )

        self.assertEqual(controller.fault.code, "OEM_HEALTH_LOST")
        self.assertEqual(backend.applied_requests, [])
        self.assertEqual(backend.stop_count, 1)

    async def test_pacing_begins_at_actual_backend_call_after_health_wait(self) -> None:
        class OneHealthTick:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.never = asyncio.Event()
                self.calls = 0

            async def sleep(self, _duration: float) -> None:
                self.calls += 1
                if self.calls == 1:
                    self.started.set()
                    await self.release.wait()
                else:
                    await self.never.wait()

        class GatedHealthBackend(FakeBackend):
            def __init__(self) -> None:
                super().__init__()
                self.runtime_health_started = asyncio.Event()
                self.runtime_health_gate = asyncio.Event()

            async def verify_oem_health(self, lease_id: str) -> BackendVerification:
                if self.health_count >= 1:
                    self.runtime_health_started.set()
                    await self.runtime_health_gate.wait()
                return await super().verify_oem_health(lease_id)

        clock = ManualClock()
        tick = OneHealthTick()
        backend = GatedHealthBackend()
        controller = KeyboardSyncController(
            backend,
            clock=clock,
            sleeper=clock.sleep,
            health_sleeper=tick.sleep,
        )
        self.assertTrue(await controller.enable())
        await asyncio.wait_for(tick.started.wait(), timeout=1.0)
        tick.release.set()
        await asyncio.wait_for(backend.runtime_health_started.wait(), timeout=1.0)

        self.assertTrue(await controller.request_static_color(StaticColor(1, 2, 3)))
        clock.now = 0.9
        backend.runtime_health_gate.set()
        await controller.wait_until_idle(timeout=1.0)

        self.assertTrue(await controller.request_static_color(StaticColor(4, 5, 6)))
        await controller.wait_until_idle(timeout=1.0)
        starts = [request.deadline_monotonic - 1.0 for request in backend.applied_requests]
        self.assertEqual(len(starts), 2)
        self.assertAlmostEqual(starts[0], 0.9)
        self.assertGreaterEqual(starts[1] - starts[0], 0.1)
        self.assertTrue(await controller.shutdown())

    async def test_one_inflight_and_newest_pending_are_enforced(self) -> None:
        gate = asyncio.Event()
        clock = ManualClock()
        backend = FakeBackend(apply_gate=gate)
        controller = KeyboardSyncController(backend, clock=clock, sleeper=clock.sleep)
        first = StaticColor(10, 20, 30)
        superseded = StaticColor(40, 50, 60)
        newest = StaticColor(70, 80, 90)

        self.assertTrue(await controller.enable())
        self.assertTrue(await controller.request_static_color(first))
        await asyncio.wait_for(backend.apply_started.wait(), timeout=1.0)
        self.assertEqual(controller.state, KeyboardSyncState.APPLYING)

        self.assertTrue(await controller.request_static_color(superseded))
        self.assertTrue(await controller.request_static_color(newest))
        self.assertEqual(controller.pending_color, newest)
        self.assertEqual(backend.max_concurrent_applies, 1)

        gate.set()
        self.assertEqual(
            await controller.wait_until_idle(timeout=1.0),
            KeyboardSyncState.ACTIVE_IDLE,
        )
        self.assertEqual([request.color for request in backend.applied_requests], [first, newest])
        self.assertEqual(backend.max_concurrent_applies, 1)
        self.assertEqual(len(clock.sleeps), 1)
        self.assertGreaterEqual(clock.sleeps[0], 0.1)
        self.assertEqual(controller.applied_color, newest)

        self.assertTrue(await controller.shutdown())

    async def test_brightness_uses_acknowledged_serial_queue(self) -> None:
        backend = FakeBackend()
        controller = KeyboardSyncController(backend)

        self.assertTrue(await controller.enable())
        self.assertTrue(await controller.request_brightness(3))
        self.assertEqual(
            await controller.wait_until_idle(timeout=1.0),
            KeyboardSyncState.ACTIVE_IDLE,
        )
        self.assertEqual(
            [request.level for request in backend.applied_brightness_requests],
            [3],
        )
        self.assertEqual(controller.applied_brightness, 3)
        self.assertTrue(await controller.shutdown())

    async def test_ack_sequence_mismatch_quarantines_without_retry(self) -> None:
        backend = FakeBackend(ack_sequence_offset=1)
        controller = KeyboardSyncController(backend)
        first = StaticColor(1, 2, 3)
        pending = StaticColor(4, 5, 6)

        self.assertTrue(await controller.enable())
        self.assertTrue(await controller.request_static_color(first))
        self.assertTrue(await controller.request_static_color(pending))
        self.assertEqual(
            await controller.wait_until_idle(timeout=1.0),
            KeyboardSyncState.QUARANTINED,
        )
        self.assertEqual(len(backend.applied_requests), 1)
        self.assertIsNone(controller.pending_color)
        self.assertEqual(controller.fault.code, "ACK_MISMATCH")
        self.assertTrue(controller.fault.unknown_outcome)
        self.assertEqual(backend.stop_count, 1)

        self.assertFalse(await controller.disable())
        self.assertEqual(backend.stop_count, 1)

    async def test_ack_color_mismatch_quarantines(self) -> None:
        backend = FakeBackend(ack_color=StaticColor(9, 9, 9))
        controller = KeyboardSyncController(backend)

        self.assertTrue(await controller.enable())
        self.assertTrue(await controller.request_static_color(StaticColor(1, 2, 3)))
        await controller.wait_until_idle(timeout=1.0)

        self.assertEqual(controller.state, KeyboardSyncState.QUARANTINED)
        self.assertEqual(controller.fault.code, "ACK_MISMATCH")
        self.assertEqual(len(backend.applied_requests), 1)

    async def test_unknown_apply_error_quarantines_and_never_retries(self) -> None:
        backend = FakeBackend(apply_error=RuntimeError("uncertain transport failure"))
        controller = KeyboardSyncController(backend)

        self.assertTrue(await controller.enable())
        self.assertTrue(await controller.request_static_color(StaticColor(1, 2, 3)))
        self.assertTrue(await controller.request_static_color(StaticColor(4, 5, 6)))
        await controller.wait_until_idle(timeout=1.0)

        self.assertEqual(controller.state, KeyboardSyncState.QUARANTINED)
        self.assertEqual(controller.fault.code, "APPLY_FAILED")
        self.assertEqual(len(backend.applied_requests), 1)
        self.assertEqual(backend.restore_count, 0)
        self.assertEqual(backend.stop_count, 1)

    async def test_disable_waits_for_inflight_then_restores_once(self) -> None:
        gate = asyncio.Event()
        backend = FakeBackend(apply_gate=gate)
        controller = KeyboardSyncController(backend)

        self.assertTrue(await controller.enable())
        self.assertTrue(await controller.request_static_color(StaticColor(1, 2, 3)))
        await asyncio.wait_for(backend.apply_started.wait(), timeout=1.0)

        disabling = asyncio.create_task(controller.disable())
        async def wait_for_draining() -> None:
            while controller.state is KeyboardSyncState.APPLYING:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_draining(), timeout=1.0)
        self.assertEqual(controller.state, KeyboardSyncState.DRAINING)
        gate.set()
        self.assertTrue(await asyncio.wait_for(disabling, timeout=1.0))

        self.assertEqual(controller.state, KeyboardSyncState.OEM_ONLY)
        self.assertEqual(backend.restore_count, 1)
        self.assertEqual(backend.stop_count, 1)
        self.assertTrue(await controller.disable())
        self.assertEqual(backend.restore_count, 1)
        self.assertEqual(backend.stop_count, 1)

    async def test_shutdown_is_idempotent_and_prevents_reenable(self) -> None:
        backend = FakeBackend()
        controller = KeyboardSyncController(backend)

        self.assertTrue(await controller.enable())
        self.assertTrue(await controller.shutdown())
        self.assertTrue(await controller.shutdown())
        self.assertTrue(controller.closed)
        self.assertEqual(controller.state, KeyboardSyncState.OEM_ONLY)
        self.assertEqual(backend.restore_count, 1)
        self.assertEqual(backend.stop_count, 1)
        self.assertFalse(await controller.enable())

    async def test_minimum_interval_cannot_be_reduced_below_ten_hz(self) -> None:
        with self.assertRaises(ValueError):
            KeyboardSyncController(FakeBackend(), min_apply_interval=0.099)

    async def test_apply_timeout_quarantines_without_retry(self) -> None:
        gate = asyncio.Event()
        backend = FakeBackend(apply_gate=gate)
        controller = KeyboardSyncController(backend, apply_timeout=0.01)

        self.assertTrue(await controller.enable())
        self.assertTrue(await controller.request_static_color(StaticColor(1, 2, 3)))
        await controller.wait_until_idle(timeout=1.0)

        self.assertEqual(controller.state, KeyboardSyncState.QUARANTINED)
        self.assertEqual(controller.fault.code, "APPLY_TIMEOUT")
        self.assertEqual(len(backend.applied_requests), 1)
        self.assertEqual(backend.stop_count, 1)

    async def test_late_apply_ack_is_ignored_after_hard_deadline(self) -> None:
        class CancellationResistantApplyBackend(FakeBackend):
            def __init__(self) -> None:
                super().__init__()
                self.release_late_ack = asyncio.Event()
                self.apply_cancellation_seen = asyncio.Event()
                self.late_ack_returned = asyncio.Event()

            async def apply_static_color(self, request: ApplyRequest) -> ApplyAck:
                self.calls.append("apply")
                self.applied_requests.append(request)
                self.apply_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.apply_cancellation_seen.set()
                    await self.release_late_ack.wait()
                self.late_ack_returned.set()
                return ApplyAck(
                    lease_id=request.lease_id,
                    sequence=request.sequence,
                    color=request.color,
                    duration_ms=50.0,
                )

        backend = CancellationResistantApplyBackend()
        controller = KeyboardSyncController(backend, apply_timeout=0.01)
        color = StaticColor(1, 2, 3)
        self.assertTrue(await controller.enable())
        self.assertTrue(await controller.request_static_color(color))

        settled = asyncio.create_task(controller.wait_until_idle())
        await asyncio.wait_for(backend.apply_cancellation_seen.wait(), timeout=0.5)
        self.assertFalse(settled.done())

        backend.release_late_ack.set()
        self.assertEqual(
            await asyncio.wait_for(settled, timeout=0.5),
            KeyboardSyncState.QUARANTINED,
        )
        self.assertEqual(controller.fault.code, "APPLY_TIMEOUT")
        self.assertIsNone(controller.applied_color)
        self.assertEqual(backend.stop_count, 1)

        await asyncio.wait_for(backend.late_ack_returned.wait(), timeout=0.5)

        self.assertEqual(controller.state, KeyboardSyncState.QUARANTINED)
        self.assertIsNone(controller.applied_color)
        self.assertEqual(len(backend.applied_requests), 1)
        self.assertLess(backend.calls.index("apply"), backend.calls.index("stop"))

    async def test_cancelled_apply_worker_before_commit_quarantines(self) -> None:
        backend = FakeBackend()
        controller = KeyboardSyncController(backend)
        self.assertTrue(await controller.enable())
        self.assertTrue(await controller.request_static_color(StaticColor(1, 2, 3)))
        await controller.wait_until_idle(timeout=1.0)

        self.assertTrue(await controller.request_static_color(StaticColor(4, 5, 6)))
        await asyncio.sleep(0)
        worker = controller._apply_task
        self.assertIsNotNone(worker)
        worker.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await worker

        self.assertEqual(
            await controller.wait_until_idle(timeout=1.0),
            KeyboardSyncState.QUARANTINED,
        )
        self.assertEqual(len(backend.applied_requests), 1)
        self.assertEqual(backend.stop_count, 1)

    async def test_cancelled_health_monitor_quarantines_active_session(self) -> None:
        backend = FakeBackend()
        controller = KeyboardSyncController(backend)
        self.assertTrue(await controller.enable())

        monitor = controller._health_task
        self.assertIsNotNone(monitor)
        await asyncio.sleep(0)
        monitor.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await monitor

        self.assertEqual(
            await controller.wait_until_idle(timeout=1.0),
            KeyboardSyncState.QUARANTINED,
        )
        self.assertEqual(backend.stop_count, 1)

    async def test_start_timeout_quarantines_and_stops_backend_once(self) -> None:
        class SlowStartBackend(FakeBackend):
            async def start(self, lease_id: str) -> BackendStart:
                await asyncio.sleep(60)
                return await super().start(lease_id)

        backend = SlowStartBackend()
        controller = KeyboardSyncController(backend, start_timeout=0.01)

        self.assertFalse(await controller.enable())
        self.assertEqual(controller.state, KeyboardSyncState.QUARANTINED)
        self.assertEqual(controller.fault.code, "START_FAILED")
        self.assertEqual(backend.stop_count, 1)

    async def test_receiver_identity_change_is_rejected_before_any_apply(self) -> None:
        class WrongIdentityBackend(FakeBackend):
            async def start(self, lease_id: str) -> BackendStart:
                started = await super().start(lease_id)
                return BackendStart(
                    lease_id=started.lease_id,
                    backend_id="different-receiver",
                    backend_version=started.backend_version,
                    capabilities=started.capabilities,
                    original_color=started.original_color,
                )

        backend = WrongIdentityBackend()
        controller = KeyboardSyncController(backend)

        self.assertFalse(await controller.enable())
        self.assertEqual(controller.state, KeyboardSyncState.BLOCKED)
        self.assertEqual(backend.stop_count, 1)
        self.assertEqual(backend.applied_requests, [])

    async def test_suspend_cycle_returns_to_oem_only_and_requires_new_enable(self) -> None:
        backend = FakeBackend()
        controller = KeyboardSyncController(backend)

        self.assertTrue(await controller.enable())
        self.assertTrue(await controller.disable())
        self.assertEqual(controller.state, KeyboardSyncState.OEM_ONLY)
        self.assertFalse(controller.enabled)
        self.assertTrue(await controller.enable())
        self.assertEqual(backend.probe_count, 2)
        self.assertTrue(await controller.shutdown())


class JsonLineProtocolTests(unittest.TestCase):
    def test_valid_set_color_round_trip(self) -> None:
        message = {
            "type": "SET_COLOR",
            "lease_id": "lease-1",
            "sequence": 7,
            "rgb": {"r": 1, "g": 2, "b": 3},
        }

        encoded = JsonLineProtocol.encode(message)
        self.assertLessEqual(len(encoded), JsonLineProtocol.MAX_LINE_BYTES)
        self.assertEqual(JsonLineProtocol.decode(encoded), message)

    def test_rejects_oversized_line_before_parsing(self) -> None:
        line = b"{" + (b"x" * JsonLineProtocol.MAX_LINE_BYTES) + b"}\n"
        with self.assertRaises(ProtocolValidationError):
            JsonLineProtocol.decode(line)

    def test_rejects_multiple_or_unterminated_lines(self) -> None:
        valid = JsonLineProtocol.encode({"type": "STOP", "lease_id": "lease"})
        with self.assertRaises(ProtocolValidationError):
            JsonLineProtocol.decode(valid + valid)
        with self.assertRaises(ProtocolValidationError):
            JsonLineProtocol.decode(valid.rstrip(b"\n"))

    def test_rejects_non_object_unknown_type_and_extra_fields(self) -> None:
        with self.assertRaises(ProtocolValidationError):
            JsonLineProtocol.decode(b"[]\n")
        with self.assertRaises(ProtocolValidationError):
            JsonLineProtocol.decode(b'{"type":[]}\n')
        with self.assertRaises(ProtocolValidationError):
            JsonLineProtocol.decode(b'{"type":"NOPE"}\n')
        with self.assertRaises(ProtocolValidationError):
            JsonLineProtocol.decode(b'{"type":"STOP","lease_id":"x","extra":1}\n')

    def test_rejects_duplicate_fields_and_boolean_protocol_version(self) -> None:
        with self.assertRaises(ProtocolValidationError):
            JsonLineProtocol.decode(
                b'{"type":"STOP","lease_id":"first","lease_id":"second"}\n'
            )
        with self.assertRaises(ProtocolValidationError):
            JsonLineProtocol.encode(
                {
                    "type": "HELLO",
                    "protocol": True,
                    "backend_id": "backend",
                    "backend_version": "1",
                }
            )

    def test_rejects_unhashable_nack_phase_cleanly(self) -> None:
        with self.assertRaises(ProtocolValidationError):
            JsonLineProtocol.encode(
                {
                    "type": "NACK",
                    "lease_id": "lease",
                    "sequence": 1,
                    "phase": [],
                    "fatal": True,
                    "code": "bad",
                }
            )

    def test_rejects_invalid_ack_and_unsafe_interval(self) -> None:
        with self.assertRaises(ProtocolValidationError):
            JsonLineProtocol.encode(
                {
                    "type": "ACK",
                    "lease_id": "lease",
                    "sequence": 0,
                    "confirmed_rgb": {"r": 1, "g": 2, "b": 3},
                    "confirmation": "synchronous_vendor_return_103",
                    "duration_ms": 1.0,
                }
            )
        with self.assertRaises(ProtocolValidationError):
            JsonLineProtocol.encode(
                {
                    "type": "CONFIG",
                    "lease_id": "lease",
                    "hardware_identity": "fake",
                    "min_interval_ms": -1,
                }
            )

    def test_protocol_surface_has_only_qualified_color_and_brightness_writes(self) -> None:
        self.assertEqual(
            JsonLineProtocol._MESSAGE_TYPES,
            {
                "HELLO",
                "CONFIG",
                "READY",
                "SET_COLOR",
                "ACK",
                "SET_BRIGHTNESS",
                "BRIGHTNESS_ACK",
                "NACK",
                "STOP",
                "STOPPED",
            },
        )
        with self.assertRaises(ProtocolValidationError):
            JsonLineProtocol.encode(
                {
                    "type": "SET_COLOR",
                    "lease_id": "lease",
                    "sequence": 1,
                    "rgb": {"r": 1, "g": 2, "b": 3, "brightness": 50},
                }
            )
        for level in range(5):
            message = {
                "type": "SET_BRIGHTNESS",
                "lease_id": "lease",
                "sequence": level + 1,
                "level": level,
            }
            self.assertEqual(JsonLineProtocol.decode(JsonLineProtocol.encode(message)), message)
        for invalid_level in (-1, 5, True):
            with self.assertRaises(ProtocolValidationError):
                JsonLineProtocol.encode(
                    {
                        "type": "SET_BRIGHTNESS",
                        "lease_id": "lease",
                        "sequence": 1,
                        "level": invalid_level,
                    }
                )
        with self.assertRaises(ProtocolValidationError):
            JsonLineProtocol.encode(
                {
                    "type": "ACK",
                    "lease_id": "lease",
                    "sequence": 1,
                    "readback_rgb": {"r": 1, "g": 2, "b": 3},
                    "duration_ms": 1.0,
                }
            )


class ProductionSafetyTests(unittest.TestCase):
    def test_production_sources_have_no_legacy_takeover_surface(self) -> None:
        root = Path(__file__).resolve().parent
        production_text = "\n".join(
            (root / name).read_text(encoding="utf-8")
            for name in ("hr_widget.py", "keyboard_sync.py")
        )
        production = production_text.casefold()
        forbidden = (
            "--clevo-helper",
            "kbmodule",
            "setrgballcolor",
            "setbrightnesslevel",
            "updatecurrentstatus",
            "taskkill",
            "stop-process",
            "add-appxpackage",
            "setdchu_data",
            "insydedchu",
            "clv0001",
            "import clr",
        )
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, production)
        self.assertIn(
            "self.keyboard_sync_var = tk.BooleanVar(value=False)",
            production_text,
        )
        self.assertIn("--disable-experimental-clevo-ipc", production_text)
        self.assertIn("--keyboard-sync-off", production_text)
        self.assertIn("keyboard_sync_default_on=keyboard_sync_default_on", production_text)
        self.assertIn("self._keyboard_sync_auto_start_pending", production_text)
        self.assertIn("self._begin_keyboard_sync_enable(generation)", production_text)

    def test_legacy_experiments_exit_before_historical_vendor_actions(self) -> None:
        root = Path(__file__).resolve().parent
        for relative in (
            Path("diagnostics/archived_tests/clevo_led_example.py"),
            Path("scratch/test_pythonnet.py"),
        ):
            source = (root / relative).read_text(encoding="utf-8")
            exit_at = source.index("SystemExit")
            first_historical_action = min(
                position
                for token in ("import clr", "Stop-Process")
                if (position := source.find(token)) >= 0
            )
            with self.subTest(path=str(relative)):
                self.assertLess(exit_at, first_historical_action)

    def test_exact_allowlist_contains_every_restored_package_identity(self) -> None:
        self.assertEqual(len(CLEVO_TARGET_ALLOWLIST), 1)
        target = CLEVO_TARGET_ALLOWLIST[0]
        self.assertEqual(target.system_manufacturer, "Micro Electronics Inc")
        self.assertEqual(target.system_product_name, "1520")
        self.assertEqual(target.baseboard_product, "PB50EF-G")
        self.assertEqual(target.fnkey_package_version, "1.14.0.0")
        self.assertEqual(target.led_package_version, "7.68.0.0")
        self.assertEqual(target.control_center_package_version, "7.68.1.0")
        self.assertEqual(target.publisher_id, "6h6z29zh29qx0")
        self.assertEqual(target.required_services, {"CCDCHUService", "HKClipSvc"})


if __name__ == "__main__":
    unittest.main()
