"""Runs only on macOS in CI. Imports the macOS app module and constructs its
classes WITHOUT starting the rumps run loop (that would hang CI forever) and
without calling anything that requires a granted Accessibility permission
(CI has none, and shouldn't need one to prove the code itself is sound).

This is the strongest verification possible without a live human sitting at
a real desktop: it proves every pyobjc/rumps API name used in macapp.py
actually exists and behaves as called on real macOS, on the actual Python
version being installed. It does NOT and CANNOT prove that a hotkey fires
when a person presses it, or that copy/paste grabs a real selection in some
other app — that requires a human, see INSTALL.md.
"""
import sys
from pathlib import Path

# Run as `python scripts/macos_smoke_test.py` from the repo root: Python puts
# only this script's own directory (scripts/) on sys.path, not the repo
# root, so the qtranslate_mac package next to it wouldn't be importable
# without this — same fix tests/test_core.py already needed.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

print(f"Python: {sys.version}")
print(f"Platform: {sys.platform}")

import qtranslate_mac.macapp as macapp  # noqa: E402

print("Imported qtranslate_mac.macapp OK")

# --- Accessibility check function must at least run without raising -------
trusted = macapp.HotkeyMonitor.is_accessibility_trusted()
print(f"is_accessibility_trusted() -> {trusted!r} (expected False in CI, and that's fine)")
assert isinstance(trusted, bool)

# --- Hotkey monitor: start/register/stop must not raise -------------------
monitor = macapp.HotkeyMonitor()
monitor.start()
from qtranslate_mac.core import KeyCombo

monitor.register("popup", KeyCombo.parse("cmd+alt+t"), lambda: None)
assert "popup" in monitor._bindings
monitor.unregister("popup")
assert "popup" not in monitor._bindings
monitor.stop()
print("HotkeyMonitor start/register/unregister/stop OK")

# --- Popup panel: constructing + building the NSPanel must not raise ------
popup = macapp.PopupPanel()
popup.show("hello", "привет")
popup.hide()
print("PopupPanel show/hide OK")

# --- Clipboard bridge functions must at least be callable ------------------
# (We don't assert on the captured value: there is no real user selection in
# a headless CI runner, so this just proves the pasteboard/CGEvent calls
# don't raise, not that they captured anything meaningful.)
result = macapp.capture_selected_text(settle_seconds=0.05)
print(f"capture_selected_text() -> {result!r} (no real selection exists in CI, None/empty is expected)")

# --- The rumps App itself: construct (but never call .start()/.run()) -----
app = macapp.QTranslateMacApp()
assert app.settings is not None
assert app.history is not None
assert len(app.menu) > 0
print("QTranslateMacApp constructed OK (menu, settings, history all loaded)")

print("\nSMOKE TEST PASSED")
