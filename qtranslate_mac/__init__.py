"""QTranslateMac: a QTranslate-style translation helper for macOS.

This package is split in two:

- ``qtranslate_mac.core`` — pure Python logic with no macOS dependency
  (settings, history, hotkey-combo parsing, the translation call itself).
  This half runs and is tested on plain Linux/Windows/macOS.
- ``qtranslate_mac.macapp`` — the macOS menu-bar app (rumps + pyobjc):
  global hotkeys, clipboard capture/replace, popup and main windows.
  This half only imports on macOS with pyobjc installed.
"""

__version__ = "0.1.0"
