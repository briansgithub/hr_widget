import math
import tempfile
import unittest
import wave
from array import array
from pathlib import Path
from unittest.mock import patch

import hr_widget
from pulse_clock import BeatGate, CREST_PHASE, PulseClock


class PulseClockTests(unittest.TestCase):
    def test_crest_spacing_matches_bpm(self):
        for bpm in (60, 120, 200, 250):
            with self.subTest(bpm=bpm):
                period = 60.0 / bpm
                clock = PulseClock(bpm=bpm, phase=0.0, now=10.0)
                gate = BeatGate()

                first = gate.arm(clock, 10.0)
                self.assertAlmostEqual(first, 10.0 + period / 4.0)
                self.assertAlmostEqual(math.sin(clock.phase_at(first)), 1.0)
                self.assertTrue(gate.poll(clock, first))
                self.assertAlmostEqual(gate.due_at, first + period)
                self.assertTrue(gate.poll(clock, first + period))
                self.assertAlmostEqual(gate.due_at, first + period * 2.0)

    def test_gate_is_off_until_armed_and_enable_waits_for_next_crest(self):
        clock = PulseClock(bpm=60, phase=0.0, now=0.0)
        gate = BeatGate()

        self.assertFalse(gate.armed)
        self.assertFalse(gate.poll(clock, 0.25))
        self.assertFalse(gate.armed)
        self.assertAlmostEqual(gate.arm(clock, 0.40), 1.25)
        self.assertFalse(gate.poll(clock, 1.249))
        self.assertTrue(gate.poll(clock, 1.25))

    def test_bpm_change_preserves_phase_and_uses_new_period(self):
        clock = PulseClock(bpm=60, phase=0.0, now=0.0)
        phase_before = clock.phase_at(0.50)
        clock.update(120, 0.50)

        self.assertAlmostEqual(clock.phase_at(0.50), phase_before)
        gate = BeatGate()
        first = gate.arm(clock, 0.50)
        self.assertAlmostEqual(first, 0.875)
        self.assertTrue(gate.poll(clock, first))
        self.assertAlmostEqual(gate.due_at - first, 0.50)

    def test_remote_phase_correction_is_authoritative(self):
        clock = PulseClock(bpm=60, phase=0.0, now=0.0)
        clock.update(90, 2.0, phase=CREST_PHASE)

        self.assertAlmostEqual(clock.phase_at(2.0), CREST_PHASE)
        self.assertAlmostEqual(clock.next_crest_after(2.0), 2.0 + 60.0 / 90.0)

    def test_half_cycle_guard_suppresses_duplicate_after_phase_correction(self):
        clock = PulseClock(bpm=60, phase=0.0, now=0.0)
        gate = BeatGate()
        self.assertTrue(gate.poll(clock, gate.arm(clock, 0.0)))

        correction_time = 0.30
        clock.update(60, correction_time, phase=CREST_PHASE - 0.10)
        unguarded_crest = clock.next_crest_after(correction_time)
        guarded_crest = gate.reschedule(clock, correction_time)

        self.assertLess(unguarded_crest - gate.last_started_at, 0.5)
        self.assertAlmostEqual(guarded_crest, unguarded_crest + 1.0)
        self.assertFalse(gate.poll(clock, unguarded_crest))
        self.assertTrue(gate.poll(clock, guarded_crest))

    def test_late_callback_skips_missed_beat_without_catch_up(self):
        clock = PulseClock(bpm=60, phase=0.0, now=0.0)
        gate = BeatGate(max_lateness_seconds=0.100)
        self.assertAlmostEqual(gate.arm(clock, 0.0), 0.25)

        self.assertFalse(gate.poll(clock, 0.351))
        self.assertAlmostEqual(gate.due_at, 1.25)
        self.assertFalse(gate.poll(clock, 0.352))
        self.assertTrue(gate.poll(clock, 1.25))


class _Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Button:
    def __init__(self):
        self.options = {}

    def config(self, **kwargs):
        self.options.update(kwargs)


class _Root:
    def __init__(self):
        self.cancelled = []
        self.scheduled = []

    def after(self, delay, callback):
        job = f"job-{len(self.scheduled) + 1}"
        self.scheduled.append((job, delay, callback))
        return job

    def after_cancel(self, job):
        self.cancelled.append(job)

    def state(self):
        return "withdrawn"


class _Winsound:
    SND_FILENAME = 1
    SND_ASYNC = 2
    SND_NODEFAULT = 4

    def __init__(self, fail_filename=False):
        self.calls = []
        self.fail_filename = fail_filename

    def PlaySound(self, filename, flags):
        self.calls.append((filename, flags))
        if filename is not None and self.fail_filename:
            raise RuntimeError("device unavailable")


def _audio_widget(enabled=True):
    widget = hr_widget.HeartRateWidget.__new__(hr_widget.HeartRateWidget)
    widget.root = _Root()
    widget.current_bpm = 72
    widget.last_received_bpm = 72
    widget.last_bpm_received_time = 0.0
    widget.last_bpm_received_monotonic = 0.0
    widget.status_text = ""
    widget.pulse_phase = 0.0
    widget._pulse_clock = PulseClock(bpm=72, phase=0.0, now=0.0)
    widget._heartbeat_gate = BeatGate()
    widget._heartbeat_job = None
    widget._updating_heartbeat_sound_var = False
    widget._heartbeat_temp_dir = None
    widget._heartbeat_scaled_paths = {}
    widget.heartbeat_sound_var = _Var(enabled)
    widget.heartbeat_volume_var = _Var(100)
    widget.heartbeat_sound_preset_var = _Var(hr_widget.DEFAULT_HEARTBEAT_SOUND_PRESET)
    widget.heartbeat_sound_checkbox = _Button()
    return widget


class HeartbeatAudioLifecycleTests(unittest.TestCase):
    def test_sound_presets_have_expected_labels_files_and_clear_stethoscope_default(self):
        self.assertEqual(hr_widget.DEFAULT_HEARTBEAT_SOUND_PRESET, "Clear Stethoscope")
        self.assertEqual(
            hr_widget.HEARTBEAT_SOUND_PRESETS,
            {
                "Natural Lub-Dub": "heartbeat_lub_dub.wav",
                "Deep Chest": "heartbeat_deep_chest.wav",
                "Soft Resting": "heartbeat_soft_resting.wav",
                "Clear Stethoscope": "heartbeat_clear_stethoscope.wav",
                "Strong Pulse": "heartbeat_strong_pulse.wav",
            },
        )

        widget = _audio_widget(enabled=False)
        self.assertEqual(widget._heartbeat_sound_preset(), "Clear Stethoscope")
        self.assertEqual(widget._selected_heartbeat_sound_path(), hr_widget.HEARTBEAT_SOUND_PATH)

    def test_default_volume_uses_shared_asset_without_temp_copy(self):
        widget = _audio_widget(enabled=True)
        fake_winsound = _Winsound()

        with patch.object(hr_widget, "winsound", fake_winsound):
            self.assertEqual(widget._heartbeat_volume_percent(), 100)
            self.assertTrue(widget._play_heartbeat_sound())

        self.assertEqual(fake_winsound.calls[0][0], str(hr_widget.HEARTBEAT_SOUND_PATH))
        self.assertIsNone(widget._heartbeat_temp_dir)

    def test_selection_change_keeps_gate_and_plays_new_sound_at_existing_due_crest(self):
        widget = _audio_widget(enabled=True)
        due = widget._heartbeat_gate.arm(widget._pulse_clock, 0.0)
        widget._heartbeat_job = "existing-crest-job"
        widget.heartbeat_sound_preset_var.set("Deep Chest")
        fake_winsound = _Winsound()

        with tempfile.TemporaryDirectory() as asset_dir:
            selected = Path(asset_dir) / "heartbeat_deep_chest.wav"
            selected.write_bytes(b"present")
            with (
                patch.object(hr_widget, "winsound", fake_winsound),
                patch.object(hr_widget, "HEARTBEAT_ASSETS_DIR", Path(asset_dir)),
            ):
                widget._on_heartbeat_sound_preset_change()
                self.assertTrue(widget._heartbeat_gate.armed)
                self.assertAlmostEqual(widget._heartbeat_gate.due_at, due)
                self.assertEqual(widget._heartbeat_job, "existing-crest-job")
                self.assertEqual(widget.root.cancelled, [])

                with patch.object(hr_widget.time, "perf_counter", return_value=due):
                    widget._heartbeat_tick()

        self.assertEqual(fake_winsound.calls[0], (None, 0))
        self.assertEqual(fake_winsound.calls[1][0], str(selected))
        self.assertEqual(len(widget.root.scheduled), 1)

    def test_scaled_cache_is_separate_for_each_sound_preset(self):
        widget = _audio_widget(enabled=False)
        widget.heartbeat_volume_var.set(50)

        with tempfile.TemporaryDirectory() as asset_dir:
            asset_dir = Path(asset_dir)
            natural = asset_dir / "heartbeat_lub_dub.wav"
            deep = asset_dir / "heartbeat_deep_chest.wav"
            for source, sample in ((natural, 12000), (deep, -8000)):
                with wave.open(str(source), "wb") as writer:
                    writer.setnchannels(1)
                    writer.setsampwidth(2)
                    writer.setframerate(44100)
                    writer.writeframes(array("h", [sample]).tobytes())

            with (
                patch.object(hr_widget, "HEARTBEAT_SOUND_PATH", natural),
                patch.object(hr_widget, "HEARTBEAT_ASSETS_DIR", asset_dir),
            ):
                widget.heartbeat_sound_preset_var.set("Natural Lub-Dub")
                natural_scaled = widget._heartbeat_playback_path()
                widget.heartbeat_sound_preset_var.set("Deep Chest")
                deep_scaled = widget._heartbeat_playback_path()

                self.assertNotEqual(natural_scaled, deep_scaled)
                self.assertEqual(natural_scaled.name, "heartbeat_lub_dub_050.wav")
                self.assertEqual(deep_scaled.name, "heartbeat_deep_chest_050.wav")
                self.assertEqual(
                    set(widget._heartbeat_scaled_paths),
                    {("Natural Lub-Dub", 50), ("Deep Chest", 50)},
                )
                widget.shutdown_heartbeat_audio()

    def test_zero_volume_stops_current_sound_and_skips_playback(self):
        widget = _audio_widget(enabled=True)
        widget.heartbeat_volume_var.set(0)
        fake_winsound = _Winsound()

        with patch.object(hr_widget, "winsound", fake_winsound):
            widget._on_heartbeat_volume_change()
            self.assertTrue(widget._play_heartbeat_sound())

        self.assertEqual(fake_winsound.calls, [(None, 0)])
        self.assertIsNone(widget._heartbeat_temp_dir)

    def test_half_volume_scales_pcm_and_shutdown_removes_temp_directory(self):
        widget = _audio_widget(enabled=True)
        widget.heartbeat_volume_var.set(50)
        fake_winsound = _Winsound()

        with tempfile.TemporaryDirectory() as source_dir:
            source = Path(source_dir) / "source.wav"
            input_samples = array("h", [-10000, 10000, 32767, -32768])
            with wave.open(str(source), "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(44100)
                writer.writeframes(input_samples.tobytes())

            with (
                patch.object(hr_widget, "winsound", fake_winsound),
                patch.object(hr_widget, "HEARTBEAT_SOUND_PATH", source),
            ):
                scaled = widget._heartbeat_playback_path()
                self.assertEqual(widget._heartbeat_playback_path(), scaled)
                generated_dir = Path(widget._heartbeat_temp_dir.name)
                self.assertTrue(scaled.is_file())

                with wave.open(str(scaled), "rb") as reader:
                    output_samples = array("h")
                    output_samples.frombytes(reader.readframes(reader.getnframes()))

                self.assertEqual(output_samples.tolist(), [-5000, 5000, 16384, -16384])
                widget.shutdown_heartbeat_audio()

        self.assertFalse(generated_dir.exists())
        self.assertEqual(widget._heartbeat_scaled_paths, {})

    def test_invalid_pcm_copy_disables_sound_and_cleans_temp_directory(self):
        widget = _audio_widget(enabled=True)
        widget.heartbeat_volume_var.set(50)
        fake_winsound = _Winsound()

        with tempfile.TemporaryDirectory() as source_dir:
            invalid_source = Path(source_dir) / "invalid.wav"
            invalid_source.write_bytes(b"not a wave")
            with (
                patch.object(hr_widget, "winsound", fake_winsound),
                patch.object(hr_widget, "HEARTBEAT_SOUND_PATH", invalid_source),
            ):
                self.assertFalse(widget._play_heartbeat_sound())

        self.assertFalse(widget.heartbeat_sound_var.get())
        self.assertIsNone(widget._heartbeat_temp_dir)
        self.assertEqual(widget._heartbeat_scaled_paths, {})

    def test_toggle_off_stops_pending_and_current_audio(self):
        widget = _audio_widget(enabled=False)
        widget._heartbeat_gate.arm(widget._pulse_clock, 0.0)
        widget._heartbeat_job = "toggle-job"
        fake_winsound = _Winsound()

        with patch.object(hr_widget, "winsound", fake_winsound):
            widget._on_heartbeat_sound_toggle()

        self.assertFalse(widget._heartbeat_gate.armed)
        self.assertEqual(widget.root.cancelled, ["toggle-job"])
        self.assertEqual(fake_winsound.calls[-1], (None, 0))

    def test_missing_asset_reverts_toggle_off(self):
        widget = _audio_widget(enabled=True)
        fake_winsound = _Winsound()

        with (
            patch.object(hr_widget, "winsound", fake_winsound),
            patch.object(hr_widget, "HEARTBEAT_SOUND_PATH", Path("definitely-missing.wav")),
            patch.object(hr_widget.time, "perf_counter", return_value=1.0),
        ):
            widget._on_heartbeat_sound_toggle()

        self.assertFalse(widget.heartbeat_sound_var.get())
        self.assertFalse(widget._heartbeat_gate.armed)
        self.assertIn("○ Heartbeat sound", widget.heartbeat_sound_checkbox.options["text"])

    def test_missing_selected_preset_asset_reverts_toggle_off(self):
        widget = _audio_widget(enabled=True)
        widget.heartbeat_sound_preset_var.set("Strong Pulse")
        fake_winsound = _Winsound()

        with tempfile.TemporaryDirectory() as asset_dir:
            with (
                patch.object(hr_widget, "winsound", fake_winsound),
                patch.object(hr_widget, "HEARTBEAT_ASSETS_DIR", Path(asset_dir)),
                patch.object(hr_widget.time, "perf_counter", return_value=1.0),
            ):
                widget._on_heartbeat_sound_toggle()

        self.assertFalse(widget.heartbeat_sound_var.get())
        self.assertFalse(widget._heartbeat_gate.armed)
        self.assertIn("○ Heartbeat sound", widget.heartbeat_sound_checkbox.options["text"])

    def test_playback_failure_reverts_toggle_off(self):
        widget = _audio_widget(enabled=True)
        fake_winsound = _Winsound(fail_filename=True)

        with patch.object(hr_widget, "winsound", fake_winsound):
            self.assertFalse(widget._play_heartbeat_sound())

        self.assertFalse(widget.heartbeat_sound_var.get())
        self.assertEqual(fake_winsound.calls[-1], (None, 0))

    def test_bpm_zero_stops_pending_and_current_audio(self):
        widget = _audio_widget(enabled=True)
        widget._heartbeat_gate.arm(widget._pulse_clock, 0.0)
        widget._heartbeat_job = "heartbeat-job"
        fake_winsound = _Winsound()

        with (
            patch.object(hr_widget, "winsound", fake_winsound),
            patch.object(hr_widget, "_bot_ref", None),
            patch.object(hr_widget.time, "perf_counter", return_value=1.0),
        ):
            widget.update_data("Disconnected", 0)

        self.assertEqual(widget.current_bpm, 0)
        self.assertFalse(widget._heartbeat_gate.armed)
        self.assertEqual(widget.root.cancelled, ["heartbeat-job"])
        self.assertEqual(fake_winsound.calls[-1], (None, 0))

    def test_hidden_widget_tick_plays_and_schedules_next_crest(self):
        widget = _audio_widget(enabled=True)
        due = widget._heartbeat_gate.arm(widget._pulse_clock, 0.0)
        fake_winsound = _Winsound()

        with (
            patch.object(hr_widget, "winsound", fake_winsound),
            patch.object(hr_widget.time, "perf_counter", return_value=due),
        ):
            widget._heartbeat_tick()

        self.assertEqual(widget.root.state(), "withdrawn")
        self.assertEqual(fake_winsound.calls[0][0], str(hr_widget.HEARTBEAT_SOUND_PATH))
        self.assertEqual(len(widget.root.scheduled), 1)
        self.assertGreater(widget.root.scheduled[0][1], 0)

    def test_stale_data_and_shutdown_stop_audio(self):
        widget = _audio_widget(enabled=True)
        widget._heartbeat_gate.arm(widget._pulse_clock, 0.0)
        widget._heartbeat_job = "stale-job"
        fake_winsound = _Winsound()

        with (
            patch.object(hr_widget, "winsound", fake_winsound),
            patch.object(hr_widget, "_bot_ref", None),
        ):
            self.assertTrue(widget._expire_stale_bpm(hr_widget.BPM_STALE_TIMEOUT_SECONDS + 1.0))
            widget._heartbeat_job = "shutdown-job"
            widget.shutdown_heartbeat_audio()

        self.assertEqual(widget.current_bpm, 0)
        self.assertEqual(widget.status_text, "-- BPM")
        self.assertEqual(widget.root.cancelled, ["stale-job", "shutdown-job"])
        self.assertEqual(fake_winsound.calls[-1], (None, 0))


if __name__ == "__main__":
    unittest.main()
