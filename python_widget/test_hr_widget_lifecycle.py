import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import hr_widget


class _Root:
    def after(self, _delay, callback, *args):
        callback(*args)


class _App:
    def __init__(self):
        self.root = _Root()
        self.updates = []
        self.pulse_phase = 0.0

    def update_data(self, *args):
        self.updates.append(args)


class HeartRateWidgetLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_shutdown_cancels_ble_before_stopping_sync_link(self):
        events = []
        ble_started = asyncio.Event()

        async def fake_scan_and_connect(_app):
            events.append("ble-started")
            ble_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append("ble-stopped")

        class FakeSyncLink:
            def __init__(self, *_args, **kwargs):
                self.on_role_change = kwargs["on_role_change"]
                self.on_status = kwargs["on_status"]

            async def start(self):
                events.append("sync-started")

            async def stop(self):
                events.append("sync-stopped")
                self.on_role_change(hr_widget.ROLE_UNLINKED, None)
                self.on_status("Not linked (shutting down)")

        with (
            patch.object(hr_widget, "SyncLink", FakeSyncLink),
            patch.object(hr_widget, "scan_and_connect", fake_scan_and_connect),
            patch.object(hr_widget, "load_or_create_token", return_value="test-token"),
            patch.object(hr_widget, "sync_link_ref", None),
            patch.object(hr_widget, "_bot_ref", None),
            patch.object(hr_widget, "_shutdown_initiated", True),
        ):
            main_task = asyncio.create_task(hr_widget.main_async(_App()))
            await asyncio.wait_for(ble_started.wait(), timeout=1.0)
            hr_widget._shutdown_event.set()
            await asyncio.wait_for(main_task, timeout=1.0)

        self.assertEqual(
            events,
            ["sync-started", "ble-started", "ble-stopped", "sync-stopped"],
        )
        self.assertIsNone(hr_widget._ble_task)

    def test_unchanged_bpm_refreshes_receipt_timestamp(self):
        widget = hr_widget.HeartRateWidget.__new__(hr_widget.HeartRateWidget)
        widget.root = SimpleNamespace(state=lambda: "withdrawn")
        widget.current_bpm = 72
        widget.last_received_bpm = 72
        widget.last_bpm_received_time = 1.0
        widget.status_text = ""
        widget.pulse_phase = 0.0

        with (
            patch.object(hr_widget, "_bot_ref", None),
            patch.object(hr_widget.time, "time", return_value=100.0),
        ):
            widget.update_data("", 72)

        self.assertEqual(widget.current_bpm, 72)
        self.assertEqual(widget.last_bpm_received_time, 100.0)


if __name__ == "__main__":
    unittest.main()
