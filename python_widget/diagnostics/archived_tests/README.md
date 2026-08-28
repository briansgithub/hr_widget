# Archived Clevo qualification tests

These files are retained as historical Clevo LED and CPU-profile qualification
checks, including their supporting probe modules. They are intentionally outside
the active root-level test suite because they validate one-off diagnostic scripts
rather than current product behavior.

The active automated regressions remain in the repository root:

- `test_keyboard_sync.py`
- `test_hr_widget_ui.py`
- `test_hr_widget_lifecycle.py`
- `test_heartbeat_timing.py`

Related manual investigation scripts and compact hardware evidence remain under
the parent `diagnostics/` directory.
