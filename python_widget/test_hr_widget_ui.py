import queue
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import hr_widget as hr_widget_module
from hr_widget import (
    HeartRateWidget,
    KEYBOARD_BRIGHTNESS_MAX,
    KEYBOARD_BRIGHTNESS_MIN,
)


class _Placeable:
    def __init__(self):
        self.place_calls = []
        self.forget_count = 0
        self.options = {}

    def place(self, **kwargs):
        self.place_calls.append(dict(kwargs))

    def place_forget(self):
        self.forget_count += 1

    def config(self, **kwargs):
        self.options.update(kwargs)


class _Variable:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Canvas:
    def __init__(self):
        self.coords_by_item = {}
        self.bounds_by_item = {}
        self.item_options = {}
        self.config_calls = []

    def coords(self, item, *coords):
        self.coords_by_item[item] = coords

    def itemconfigure(self, item, **kwargs):
        self.item_options.setdefault(item, {}).update(kwargs)

    def config(self, **kwargs):
        self.config_calls.append(dict(kwargs))

    def bbox(self, item):
        return self.bounds_by_item.get(item)


class _DragRoot:
    def __init__(self):
        self.x_queries = 0
        self.y_queries = 0
        self.scheduled = []

    def winfo_x(self):
        self.x_queries += 1
        return 40

    def winfo_y(self):
        self.y_queries += 1
        return 50

    def after(self, delay, callback):
        self.scheduled.append((delay, callback))
        return "drag-job"


def _make_hover_widget():
    widget = HeartRateWidget.__new__(HeartRateWidget)
    widget.canvas = _Canvas()
    widget._cached_window_size = (
        HeartRateWidget.CONTROL_MIN_WIDTH,
        HeartRateWidget.CONTROL_MIN_HEIGHT,
    )
    widget.controls_visible = False
    widget._cursor_left_time = None
    widget._cursor_entered_time = None

    for item_name in (
        "_bg_rect",
        "_left_panel",
        "_right_panel",
        "_levels_panel",
        "_header_divider",
        "_left_divider",
        "_right_divider",
        "_experience_title",
        "_system_title",
        "_connections_title",
        "_hardware_title",
        "_drag_text",
    ):
        setattr(widget, item_name, item_name)

    control_names = (
        "checkbox",
        "heartbeat_sound_checkbox",
        "topmost_checkbox",
        "screen_off_checkbox",
        "hibernate_checkbox",
        "threshold_checkbox",
        "discord_checkbox",
        "sync_link_checkbox",
        "pair_code_button",
        "keyboard_sync_checkbox",
        "min_brightness_scale",
        "max_brightness_scale",
        "heartbeat_volume_scale",
        "exit_button",
    )
    controls = []
    for control_name in control_names:
        control = _Placeable()
        setattr(widget, control_name, control)
        controls.append(control)
    widget._hover_controls = tuple(controls)
    return widget, controls


class HoverControlLayoutTests(unittest.TestCase):
    def test_hover_timing_constants_remain_exact(self):
        self.assertEqual(HeartRateWidget.CONTROL_REVEAL_DELAY_SECONDS, 1.5)
        self.assertEqual(HeartRateWidget.CONTROL_HIDE_DELAY_SECONDS, 1.0)

    def test_idle_reveal_activates_only_over_metric_text(self):
        widget, _ = _make_hover_widget()
        widget._dragging = False
        widget._idle_hover_items = ("bpm", "time", "uptime")
        widget.canvas.bounds_by_item = {
            "bpm": (250, 150, 350, 200),
            "time": (270, 120, 330, 140),
            "uptime": (275, 215, 325, 235),
        }

        self.assertFalse(widget._point_over_idle_text(100, 100))
        self.assertTrue(widget._point_over_idle_text(300, 175))
        self.assertTrue(widget._point_over_idle_text(300, 130))
        self.assertTrue(widget._point_over_idle_text(300, 225))

        widget.on_motion(SimpleNamespace(x=100, y=100))
        self.assertIsNone(widget._cursor_entered_time)
        widget.on_motion(SimpleNamespace(x=300, y=175))
        self.assertIsNotNone(widget._cursor_entered_time)
        widget.on_motion(SimpleNamespace(x=100, y=100))
        self.assertIsNone(widget._cursor_entered_time)

    def test_topmost_default_is_rendered_as_selected(self):
        widget = HeartRateWidget.__new__(HeartRateWidget)
        attribute_calls = []
        widget.root = SimpleNamespace(attributes=lambda *args: attribute_calls.append(args))
        widget.topmost_var = SimpleNamespace(get=lambda: True)
        widget.topmost_checkbox = _Placeable()

        widget._on_topmost_toggle()

        self.assertEqual(attribute_calls, [("-topmost", True)])
        self.assertEqual(widget.topmost_checkbox.options["text"], "● Always on top")
        self.assertEqual(
            widget.topmost_checkbox.options["bg"],
            HeartRateWidget.CONTROL_COLORS["selected"],
        )

    def test_first_reveal_sizes_card_and_places_every_control(self):
        widget, controls = _make_hover_widget()

        widget._show_controls()

        self.assertTrue(widget.controls_visible)
        for control in controls:
            self.assertEqual(len(control.place_calls), 1)

        card_coords = widget.canvas.coords_by_item[widget._bg_rect]
        self.assertEqual((min(card_coords[0::2]), max(card_coords[0::2])), (8, 642))
        self.assertEqual((min(card_coords[1::2]), max(card_coords[1::2])), (8, 392))
        self.assertEqual(widget.canvas.item_options["hover_decor"]["state"], "normal")
        self.assertEqual(widget.canvas.config_calls[-1], {"cursor": "fleur"})

    def test_hide_forgets_every_control_and_restores_transparent_cursor(self):
        widget, controls = _make_hover_widget()
        widget._show_controls()

        widget._hide_controls()

        self.assertFalse(widget.controls_visible)
        for control in controls:
            self.assertEqual(control.forget_count, 1)
        self.assertEqual(widget.canvas.item_options["hover_decor"]["state"], "hidden")
        self.assertEqual(widget.canvas.config_calls[-1], {"cursor": ""})

    def test_larger_layout_keeps_edge_controls_anchored(self):
        widget, _ = _make_hover_widget()
        widget.controls_visible = True

        widget._layout_hover_panel(800, 500)

        card_coords = widget.canvas.coords_by_item[widget._bg_rect]
        self.assertEqual((min(card_coords[0::2]), max(card_coords[0::2])), (8, 792))
        self.assertEqual((min(card_coords[1::2]), max(card_coords[1::2])), (8, 492))
        self.assertEqual(widget.exit_button.place_calls[-1]["x"], 724)
        self.assertEqual(widget.exit_button.place_calls[-1]["y"], 460)
        self.assertEqual(widget.heartbeat_volume_scale.place_calls[-1]["y"], 399)
        self.assertLess(
            widget.heartbeat_volume_scale.place_calls[-1]["y"]
            + widget.heartbeat_volume_scale.place_calls[-1]["height"],
            widget.exit_button.place_calls[-1]["y"],
        )
        self.assertLess(
            widget.min_brightness_scale.place_calls[-1]["x"],
            widget.max_brightness_scale.place_calls[-1]["x"],
        )
        self.assertLess(
            widget.max_brightness_scale.place_calls[-1]["x"],
            widget.heartbeat_volume_scale.place_calls[-1]["x"],
        )

    def test_visibility_transitions_at_configured_boundaries(self):
        widget, _ = _make_hover_widget()
        widget._cursor_entered_time = 10.0

        widget._update_control_visibility(11.499, True)
        self.assertFalse(widget.controls_visible)
        widget._update_control_visibility(11.5, True)
        self.assertTrue(widget.controls_visible)

        widget._cursor_left_time = 20.0
        widget._update_control_visibility(20.999, False)
        self.assertTrue(widget.controls_visible)
        widget._update_control_visibility(21.0, False)
        self.assertFalse(widget.controls_visible)

    def test_cursor_reentry_cancels_pending_hide(self):
        widget, _ = _make_hover_widget()
        widget.controls_visible = True
        widget._cursor_left_time = 20.0

        widget._update_control_visibility(20.5, True)

        self.assertTrue(widget.controls_visible)
        self.assertIsNone(widget._cursor_left_time)


class DragInteractionTests(unittest.TestCase):
    def test_drag_starts_only_from_visible_canvas(self):
        widget = HeartRateWidget.__new__(HeartRateWidget)
        widget.canvas = _Canvas()
        widget.root = _DragRoot()
        widget.controls_visible = False
        widget._dragging = False
        canvas_event = SimpleNamespace(widget=widget.canvas, x_root=100, y_root=120)

        widget._on_drag_start(canvas_event)
        self.assertFalse(widget._dragging)
        self.assertEqual(widget.root.x_queries, 0)

        widget.controls_visible = True
        control_event = SimpleNamespace(widget=_Placeable(), x_root=100, y_root=120)
        widget._on_drag_start(control_event)
        self.assertFalse(widget._dragging)
        self.assertEqual(widget.root.x_queries, 0)

        widget._on_drag_start(canvas_event)
        self.assertTrue(widget._dragging)
        self.assertEqual((widget._drag_start_x, widget._drag_start_y), (100, 120))
        self.assertEqual((widget._win_x, widget._win_y), (40, 50))

    def test_drag_motion_coalesces_updates_at_sixteen_milliseconds(self):
        widget = HeartRateWidget.__new__(HeartRateWidget)
        widget.root = _DragRoot()
        widget._dragging = True
        widget._drag_job = None
        widget._win_x = 40
        widget._win_y = 50
        widget._drag_start_x = 100
        widget._drag_start_y = 120

        widget._on_drag_motion(SimpleNamespace(x_root=110, y_root=140))
        widget._on_drag_motion(SimpleNamespace(x_root=130, y_root=150))

        self.assertEqual(len(widget.root.scheduled), 1)
        self.assertEqual(widget.root.scheduled[0][0], 16)
        self.assertEqual((widget._drag_target_x, widget._drag_target_y), (70, 80))


class KeyboardSyncUiTests(unittest.TestCase):
    def test_keyboard_work_is_rejected_before_background_ready(self):
        class RunningLoop:
            @staticmethod
            def is_running():
                return True

        original_loop = hr_widget_module._loop_ref
        was_ready = hr_widget_module._background_ready.is_set()
        widget = HeartRateWidget.__new__(HeartRateWidget)
        called = []
        try:
            hr_widget_module._loop_ref = RunningLoop()
            hr_widget_module._background_ready.clear()
            result = widget._submit_keyboard_sync(
                lambda: called.append(True),
            )
        finally:
            hr_widget_module._loop_ref = original_loop
            if was_ready:
                hr_widget_module._background_ready.set()
            else:
                hr_widget_module._background_ready.clear()

        self.assertIsNone(result)
        self.assertEqual(called, [])

    def test_completion_queue_runs_callbacks_on_tk_poller(self):
        widget = HeartRateWidget.__new__(HeartRateWidget)
        widget.root = _DragRoot()
        widget._keyboard_sync_completions = queue.SimpleQueue()
        widget._keyboard_sync_poll_job = "old-job"
        refreshed = []
        completed = object()
        observed = []
        widget._refresh_keyboard_sync_availability = lambda: refreshed.append(True)
        widget._keyboard_sync_completions.put(
            (lambda value: observed.append(value), completed)
        )

        widget._drain_keyboard_sync_completions()

        self.assertEqual(observed, [completed])
        self.assertEqual(refreshed, [True])
        self.assertEqual(widget.root.scheduled[0][0], 50)

    def test_session_without_opt_in_forces_toggle_back_off(self):
        widget = HeartRateWidget.__new__(HeartRateWidget)
        widget.keyboard_sync_var = _Variable(True)
        widget.keyboard_sync_checkbox = _Placeable()
        widget._updating_keyboard_sync_var = False
        widget._keyboard_sync_opt_in = False
        widget._keyboard_sync_desired = False
        widget._keyboard_sync_generation = 0
        widget._last_requested_keyboard_color = object()
        widget._keyboard_color_request_future = None

        widget._on_keyboard_sync_toggle()

        self.assertFalse(widget.keyboard_sync_var.get())
        self.assertFalse(widget._keyboard_sync_desired)
        self.assertIn("blocked", widget.keyboard_sync_checkbox.options["text"])

    def test_stale_enable_completion_cannot_reenable_after_disable(self):
        widget = HeartRateWidget.__new__(HeartRateWidget)
        scheduled = []
        widget.keyboard_sync = SimpleNamespace(disable=lambda: None)
        widget._keyboard_sync_generation = 2
        widget._keyboard_sync_desired = False
        widget._submit_keyboard_sync = (
            lambda factory, on_done=None: scheduled.append(factory) or object()
        )
        completed = SimpleNamespace(result=lambda: True)

        widget._complete_keyboard_sync_enable(completed, generation=1)

        self.assertEqual(scheduled, [widget.keyboard_sync.disable])

    def test_failed_disable_submission_never_claims_normal_off(self):
        widget = HeartRateWidget.__new__(HeartRateWidget)
        widget.keyboard_sync_var = _Variable(False)
        widget.keyboard_sync_checkbox = _Placeable()
        widget._updating_keyboard_sync_var = False
        widget._keyboard_sync_opt_in = True
        widget._keyboard_sync_desired = True
        widget._keyboard_sync_generation = 0
        widget._keyboard_sync_future = object()
        widget._last_requested_keyboard_color = object()
        widget._submit_keyboard_sync = lambda factory, on_done=None: None
        widget.keyboard_sync = SimpleNamespace(disable=lambda: None)

        widget._on_keyboard_sync_toggle()

        self.assertIn("blocked", widget.keyboard_sync_checkbox.options["text"])
        self.assertEqual(widget.keyboard_sync_checkbox.options["state"], "disabled")
        self.assertFalse(widget._keyboard_sync_desired)

    def test_quarantined_enable_is_labeled_quarantined(self):
        widget = HeartRateWidget.__new__(HeartRateWidget)
        widget.root = object()
        widget.keyboard_sync_var = _Variable(True)
        widget.keyboard_sync_checkbox = _Placeable()
        widget._updating_keyboard_sync_var = False
        widget._keyboard_sync_generation = 1
        widget._keyboard_sync_desired = True
        widget._keyboard_sync_future = object()
        widget._last_requested_keyboard_color = object()
        widget.keyboard_sync = SimpleNamespace(
            enabled=False,
            state=hr_widget_module.KeyboardSyncState.QUARANTINED,
            fault=SimpleNamespace(message="unknown start outcome"),
        )

        with patch.object(hr_widget_module.messagebox, "showwarning"):
            widget._complete_keyboard_sync_enable(
                SimpleNamespace(result=lambda: False),
                generation=1,
            )

        self.assertIn("blocked", widget.keyboard_sync_checkbox.options["text"])
        self.assertEqual(widget.keyboard_sync_checkbox.options["state"], "disabled")

    def test_successful_enable_makes_keyboard_sync_clickable(self):
        widget = HeartRateWidget.__new__(HeartRateWidget)
        widget.keyboard_sync_var = _Variable(True)
        widget.keyboard_sync_checkbox = _Placeable()
        widget._keyboard_sync_generation = 1
        widget._keyboard_sync_desired = True
        widget._keyboard_sync_future = object()
        widget._last_requested_keyboard_color = object()
        widget.keyboard_sync = SimpleNamespace(enabled=True)

        completed = SimpleNamespace(result=lambda: True)
        widget._complete_keyboard_sync_enable(completed, generation=1)

        self.assertEqual(widget.keyboard_sync_checkbox.options["state"], "normal")
        self.assertEqual(widget.keyboard_sync_checkbox.options["cursor"], "hand2")
        self.assertIn("(on)", widget.keyboard_sync_checkbox.options["text"])

    def test_hibernate_prepare_precedes_async_drain_submission(self):
        widget = HeartRateWidget.__new__(HeartRateWidget)
        order = []
        widget._prepare_keyboard_sync_for_suspend = lambda: order.append("prepare")
        widget._submit_keyboard_sync = (
            lambda factory, on_done=None: order.append("submit") or object()
        )

        widget._begin_keyboard_safe_hibernate()

        self.assertEqual(order, ["prepare", "submit"])

    def test_stale_enable_completion_does_not_disable_newer_enable_request(self):
        widget = HeartRateWidget.__new__(HeartRateWidget)
        scheduled = []
        widget.keyboard_sync = SimpleNamespace(disable=lambda: None)
        widget._keyboard_sync_generation = 3
        widget._keyboard_sync_desired = True
        widget._submit_keyboard_sync = lambda factory, on_done=None: scheduled.append(factory)
        completed = SimpleNamespace(result=lambda: True)

        widget._complete_keyboard_sync_enable(completed, generation=1)

        self.assertEqual(scheduled, [])

    def test_static_color_request_is_not_requeued_until_color_changes(self):
        widget = HeartRateWidget.__new__(HeartRateWidget)
        callbacks = []
        futures = []

        def submit(_factory, on_done=None):
            future = SimpleNamespace(result=lambda: True)
            futures.append(future)
            callbacks.append(on_done)
            return future

        widget.current_bpm = 70
        widget._cached_color = "#010203"
        widget._keyboard_sync_desired = True
        widget._keyboard_sync_generation = 4
        widget._keyboard_sync_future = None
        widget._keyboard_color_request_future = None
        widget._last_requested_keyboard_color = None
        widget.keyboard_sync = SimpleNamespace(enabled=True)
        widget._submit_keyboard_sync = submit

        widget._request_current_keyboard_color()
        widget._request_current_keyboard_color()
        self.assertEqual(len(futures), 1)

        callbacks[0](futures[0])
        widget._request_current_keyboard_color()
        self.assertEqual(len(futures), 1)

        widget._cached_color = "#040506"
        widget._request_current_keyboard_color()
        self.assertEqual(len(futures), 2)

    def test_keyboard_brightness_tracks_phase_at_ten_hz(self):
        widget = HeartRateWidget.__new__(HeartRateWidget)
        callbacks = []
        futures = []

        def submit(_factory, on_done=None):
            future = SimpleNamespace(result=lambda: True)
            futures.append(future)
            callbacks.append(on_done)
            return future

        widget.current_bpm = 60
        widget.pulse_phase = -hr_widget_module.math.pi / 2
        widget._keyboard_sync_desired = True
        widget._keyboard_sync_generation = 4
        widget._keyboard_sync_future = None
        widget._keyboard_brightness_request_future = None
        widget._last_requested_keyboard_brightness = None
        widget._last_keyboard_brightness_request_time = float("-inf")
        widget.keyboard_sync = SimpleNamespace(enabled=True)
        widget._submit_keyboard_sync = submit

        widget._request_current_keyboard_brightness(1.0)
        self.assertEqual(len(futures), 1)
        callbacks[0](futures[0])
        self.assertEqual(
            widget._last_requested_keyboard_brightness,
            KEYBOARD_BRIGHTNESS_MIN,
        )

        widget.pulse_phase = hr_widget_module.math.pi / 2
        widget._request_current_keyboard_brightness(1.099)
        self.assertEqual(len(futures), 1)
        widget._request_current_keyboard_brightness(1.100)
        self.assertEqual(len(futures), 2)
        callbacks[1](futures[1])
        self.assertEqual(
            widget._last_requested_keyboard_brightness,
            KEYBOARD_BRIGHTNESS_MAX,
        )

    def test_keyboard_brightness_respects_user_minimum_and_maximum(self):
        self.assertEqual(
            HeartRateWidget._keyboard_brightness_for_phase(
                -hr_widget_module.math.pi / 2,
                2,
                3,
            ),
            2,
        )
        self.assertEqual(
            HeartRateWidget._keyboard_brightness_for_phase(
                hr_widget_module.math.pi / 2,
                2,
                3,
            ),
            3,
        )

    def test_brightness_sliders_keep_an_ordered_range(self):
        widget = HeartRateWidget.__new__(HeartRateWidget)
        widget._updating_brightness_sliders = False
        widget.min_brightness_var = _Variable(4)
        widget.max_brightness_var = _Variable(2)
        widget._last_requested_keyboard_brightness = 3
        widget._last_keyboard_brightness_request_time = 10.0

        widget._on_min_brightness_change()
        self.assertEqual(widget.max_brightness_var.get(), 4)
        self.assertIsNone(widget._last_requested_keyboard_brightness)

        widget.min_brightness_var.set(3)
        widget.max_brightness_var.set(1)
        widget._on_max_brightness_change()
        self.assertEqual(widget.min_brightness_var.get(), 1)
        self.assertEqual(widget._last_keyboard_brightness_request_time, float("-inf"))


class MirrorStatusTests(unittest.TestCase):
    """While MIRRORing, the link-status loop and the relayed-bpm callback both
    write to the same caption. Only the loop must yield: update_data(text)
    defaults to bpm=0, so an unguarded status write erases a live reading and
    the display flips between the BPM and "Mirroring <peer>" once a second."""

    def _widget(self, current_bpm):
        widget = HeartRateWidget.__new__(HeartRateWidget)
        widget.current_bpm = current_bpm
        widget.update_data_calls = []
        widget.update_data = lambda *args, **kwargs: widget.update_data_calls.append(
            (args, kwargs)
        )
        return widget

    def test_status_write_leaves_a_live_reading_alone(self):
        widget = self._widget(72)
        widget.update_status_if_idle("Mirroring Pixel 7a")
        self.assertEqual(widget.update_data_calls, [])

    def test_status_write_applies_when_no_reading_is_shown(self):
        widget = self._widget(0)
        widget.update_status_if_idle("Mirroring Pixel 7a")
        self.assertEqual(widget.update_data_calls, [(("Mirroring Pixel 7a",), {})])

    def test_sync_status_defers_to_a_live_reading(self):
        widget = self._widget(88)
        widget.on_sync_status("Mirroring HR from Pixel 7a")
        self.assertEqual(widget.update_data_calls, [])

        widget.current_bpm = 0
        widget.on_sync_status("Not linked (link lost)")
        self.assertEqual(widget.update_data_calls, [(("Not linked (link lost)",), {})])


if __name__ == "__main__":
    unittest.main()
