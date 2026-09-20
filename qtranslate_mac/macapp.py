"""The macOS menu-bar app: global hotkeys, clipboard bridge, popup/main
windows. Everything here needs pyobjc + rumps and only really *runs* on
macOS, though it's designed to *import* cleanly enough to smoke-test on CI.

Honesty note (read this before trusting any of the interactive behavior):
importing this module and constructing its classes can be verified by CI
on a real macOS runner (see .github/workflows/ci.yml) — that catches wrong
API names, missing constants, import errors. What CI cannot verify, because
it fundamentally requires a human: whether a global hotkey actually fires
when *you* press it, whether Cmd+C really grabs your selection in some other
app, whether the popup lands in the right spot on your screen. That part
needs your own hands, once, after installing — see INSTALL.md.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from .core import (
    Settings,
    HistoryStore,
    KeyCombo,
    InvalidKeyCombo,
    Translator,
    TranslationError,
    language_name,
    default_settings_path,
    default_history_path,
)

# --- macOS-only imports -----------------------------------------------------
# Deferred to import time of this module (not package import time) so that
# `import qtranslate_mac.core` alone never requires pyobjc/rumps.
import objc  # noqa: E402
import rumps  # noqa: E402
from AppKit import (  # noqa: E402
    NSApplication,
    NSEvent,
    NSEventMaskKeyDown,
    NSPasteboard,
    NSPanel,
    NSTextField,
    NSButton,
    NSFont,
    NSColor,
    NSMakeRect,
    NSScreen,
    NSFloatingWindowLevel,
    NSWindowStyleMaskBorderless,
    NSBackingStoreBuffered,
    NSObject,
)
import Quartz  # noqa: E402


# ---------------------------------------------------------------------------
# Virtual keycode table (US layout) — same values Apple's Carbon HIToolbox
# headers define as kVK_ANSI_*. pyobjc has no first-class constant for these,
# so they're listed explicitly.
# ---------------------------------------------------------------------------
_VIRTUAL_KEYCODES = {
    "a": 0x00, "b": 0x0B, "c": 0x08, "d": 0x02, "e": 0x0E, "f": 0x03, "g": 0x05,
    "h": 0x04, "i": 0x22, "j": 0x26, "k": 0x28, "l": 0x25, "m": 0x2E, "n": 0x2D,
    "o": 0x1F, "p": 0x23, "q": 0x0C, "r": 0x0F, "s": 0x01, "t": 0x11, "u": 0x20,
    "v": 0x09, "w": 0x0D, "x": 0x07, "y": 0x10, "z": 0x06,
    "0": 0x1D, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "5": 0x17,
    "6": 0x16, "7": 0x1A, "8": 0x1C, "9": 0x19,
}

_KEYCODE_C = _VIRTUAL_KEYCODES["c"]
_KEYCODE_V = _VIRTUAL_KEYCODES["v"]

# NSEvent modifier flag bits we care about.
_MOD_FLAG_BITS = {
    "ctrl": 1 << 18,   # NSEventModifierFlagControl
    "alt": 1 << 19,    # NSEventModifierFlagOption
    "shift": 1 << 17,  # NSEventModifierFlagShift
    "cmd": 1 << 20,    # NSEventModifierFlagCommand
}
_ALL_MOD_BITS = sum(_MOD_FLAG_BITS.values())


def _combo_modifier_mask(combo: KeyCombo) -> int:
    mask = 0
    for m in combo.modifiers:
        mask |= _MOD_FLAG_BITS[m]
    return mask


def _event_matches_combo(event, combo: KeyCombo) -> bool:
    keycode = _VIRTUAL_KEYCODES.get(combo.key)
    if keycode is None or event.keyCode() != keycode:
        return False
    return int(event.modifierFlags()) & _ALL_MOD_BITS == _combo_modifier_mask(combo)


# ---------------------------------------------------------------------------
# Clipboard / synthetic keystroke bridge
# ---------------------------------------------------------------------------
_PASTEBOARD_TYPE = "public.utf8-plain-text"


def _post_keystroke(keycode: int, command: bool = True) -> None:
    source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    down = Quartz.CGEventCreateKeyboardEvent(source, keycode, True)
    up = Quartz.CGEventCreateKeyboardEvent(source, keycode, False)
    flags = Quartz.kCGEventFlagMaskCommand if command else 0
    Quartz.CGEventSetFlags(down, flags)
    Quartz.CGEventSetFlags(up, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


def capture_selected_text(settle_seconds: float = 0.18) -> Optional[str]:
    """Copies the current system-wide selection and returns it, restoring
    whatever was previously on the clipboard. Mirrors QTranslate's own
    clipboard-hijack approach on Windows. Requires Accessibility permission
    to be granted to the app bundle, or synthetic events are silently
    dropped by macOS."""
    pb = NSPasteboard.generalPasteboard()
    previous_change = pb.changeCount()
    previous_text = pb.stringForType_(_PASTEBOARD_TYPE)

    pb.clearContents()
    _post_keystroke(_KEYCODE_C, command=True)
    time.sleep(settle_seconds)

    result = None
    if pb.changeCount() != previous_change:
        result = pb.stringForType_(_PASTEBOARD_TYPE)

    pb.clearContents()
    if previous_text:
        pb.setString_forType_(previous_text, _PASTEBOARD_TYPE)

    return result.strip() if result else None


def replace_selection(text: str, restore_after_seconds: float = 0.6) -> None:
    """Puts `text` on the clipboard, sends Cmd+V to paste it over the current
    selection in the frontmost app, then restores the previous clipboard
    contents shortly after."""
    pb = NSPasteboard.generalPasteboard()
    previous_text = pb.stringForType_(_PASTEBOARD_TYPE)

    pb.clearContents()
    pb.setString_forType_(text, _PASTEBOARD_TYPE)
    _post_keystroke(_KEYCODE_V, command=True)

    def _restore():
        pb2 = NSPasteboard.generalPasteboard()
        pb2.clearContents()
        if previous_text:
            pb2.setString_forType_(previous_text, _PASTEBOARD_TYPE)

    rumps.Timer(lambda _timer: (_restore(), _timer.stop()), restore_after_seconds).start()


# ---------------------------------------------------------------------------
# Global hotkeys
# ---------------------------------------------------------------------------
class HotkeyMonitor:
    """Watches system-wide key-down events and dispatches to registered
    KeyCombo bindings. Needs Accessibility permission — without it,
    NSEvent's global monitor simply never receives events (no error)."""

    def __init__(self):
        self._bindings: dict[str, tuple[KeyCombo, Callable[[], None]]] = {}
        self._global_monitor = None
        self._local_monitor = None

    def start(self) -> None:
        self._global_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSEventMaskKeyDown, self._handle
        )
        self._local_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            NSEventMaskKeyDown, self._handle_local
        )

    def stop(self) -> None:
        if self._global_monitor is not None:
            NSEvent.removeMonitor_(self._global_monitor)
            self._global_monitor = None
        if self._local_monitor is not None:
            NSEvent.removeMonitor_(self._local_monitor)
            self._local_monitor = None

    def register(self, identifier: str, combo: KeyCombo, action: Callable[[], None]) -> None:
        self._bindings[identifier] = (combo, action)

    def unregister(self, identifier: str) -> None:
        self._bindings.pop(identifier, None)

    @staticmethod
    def is_accessibility_trusted() -> bool:
        from ApplicationServices import AXIsProcessTrustedWithOptions
        opts = {"AXTrustedCheckOptionPrompt": False}
        return bool(AXIsProcessTrustedWithOptions(opts))

    @staticmethod
    def request_accessibility() -> None:
        from ApplicationServices import AXIsProcessTrustedWithOptions
        opts = {"AXTrustedCheckOptionPrompt": True}
        AXIsProcessTrustedWithOptions(opts)

    def _dispatch(self, event) -> bool:
        for combo, action in self._bindings.values():
            if _event_matches_combo(event, combo):
                action()
                return True
        return False

    def _handle(self, event):
        self._dispatch(event)

    def _handle_local(self, event):
        return None if self._dispatch(event) else event


# ---------------------------------------------------------------------------
# Popup panel (floating window near the cursor)
# ---------------------------------------------------------------------------
class _PopupActions(NSObject):
    """Tiny target/action bridge: AppKit controls call Objective-C selectors,
    so button clicks land here and get forwarded to plain Python callbacks."""

    def initWithCallbacks_(self, callbacks: dict):
        self = objc.super(_PopupActions, self).init()
        if self is None:
            return None
        self._callbacks = callbacks
        return self

    def closeClicked_(self, sender):
        cb = self._callbacks.get("close")
        if cb:
            cb()

    def replaceClicked_(self, sender):
        cb = self._callbacks.get("replace")
        if cb:
            cb()


class PopupPanel:
    """A small borderless floating panel near the mouse cursor, showing the
    source text and its translation — the equivalent of QTranslate's Ctrl+Q
    popup."""

    WIDTH, HEIGHT = 340, 180

    def __init__(self):
        self._panel = None
        self._actions = None

    def show(self, source_text: str, translated_text: str,
              on_close: Optional[Callable[[], None]] = None,
              on_replace: Optional[Callable[[], None]] = None) -> None:
        mouse = NSEvent.mouseLocation()
        screen = NSScreen.mainScreen()
        frame = screen.visibleFrame() if screen else None

        x = mouse.x + 12
        y = mouse.y - self.HEIGHT - 12
        if frame:
            x = min(max(x, frame.origin.x + 8), frame.origin.x + frame.size.width - self.WIDTH - 8)
            y = min(max(y, frame.origin.y + 8), frame.origin.y + frame.size.height - self.HEIGHT - 8)

        rect = NSMakeRect(x, y, self.WIDTH, self.HEIGHT)
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
        )
        panel.setLevel_(NSFloatingWindowLevel)
        panel.setOpaque_(False)
        panel.setHasShadow_(True)
        panel.setBackgroundColor_(NSColor.windowBackgroundColor())

        content = panel.contentView()

        src_field = NSTextField.alloc().initWithFrame_(NSMakeRect(12, 108, self.WIDTH - 24, 50))
        src_field.setStringValue_(source_text)
        src_field.setEditable_(False)
        src_field.setBezeled_(False)
        src_field.setDrawsBackground_(False)
        src_field.setFont_(NSFont.systemFontOfSize_(12))
        src_field.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(src_field)

        dst_field = NSTextField.alloc().initWithFrame_(NSMakeRect(12, 46, self.WIDTH - 24, 58))
        dst_field.setStringValue_(translated_text)
        dst_field.setEditable_(False)
        dst_field.setBezeled_(False)
        dst_field.setDrawsBackground_(False)
        dst_field.setFont_(NSFont.boldSystemFontOfSize_(14))
        content.addSubview_(dst_field)

        self._actions = _PopupActions.alloc().initWithCallbacks_({
            "close": lambda: (self.hide(), on_close() if on_close else None),
            "replace": lambda: (self.hide(), on_replace() if on_replace else None),
        })

        close_button = NSButton.alloc().initWithFrame_(NSMakeRect(self.WIDTH - 34, self.HEIGHT - 30, 22, 22))
        close_button.setTitle_("✕")
        close_button.setBezelStyle_(1)
        close_button.setTarget_(self._actions)
        close_button.setAction_("closeClicked:")
        content.addSubview_(close_button)

        replace_button = NSButton.alloc().initWithFrame_(NSMakeRect(12, 12, self.WIDTH - 24, 24))
        replace_button.setTitle_("Заменить выделенное переводом")
        replace_button.setBezelStyle_(1)
        replace_button.setTarget_(self._actions)
        replace_button.setAction_("replaceClicked:")
        content.addSubview_(replace_button)

        panel.orderFrontRegardless()
        self._panel = panel

    def hide(self) -> None:
        if self._panel is not None:
            self._panel.orderOut_(None)


# ---------------------------------------------------------------------------
# The rumps app itself
# ---------------------------------------------------------------------------
class QTranslateMacApp(rumps.App):
    def __init__(self):
        super().__init__("QTranslateMac", icon=None, title="\U0001F310", quit_button="Выйти")
        self.settings = Settings.load(default_settings_path())
        self.history = HistoryStore(path=default_history_path(), max_items=self.settings.max_history_items)
        self._translator: Optional[Translator] = None
        self.popup = PopupPanel()
        self.hotkeys = HotkeyMonitor()

        self.popup_hotkey_item = rumps.MenuItem(
            self._popup_hotkey_title(), callback=self.show_popup_hotkey_settings
        )
        self.replace_hotkey_item = rumps.MenuItem(
            self._replace_hotkey_title(), callback=self.show_replace_hotkey_settings
        )

        self.menu = [
            rumps.MenuItem("Перевести выделенный текст", callback=self.translate_selection),
            rumps.MenuItem("Заменить выделенное переводом", callback=self.replace_selection),
            None,
            rumps.MenuItem("История переводов", callback=self.show_history),
            rumps.MenuItem("Настройки…", callback=self.show_settings),
            self.popup_hotkey_item,
            self.replace_hotkey_item,
            None,
            rumps.MenuItem("Проверить доступ (Accessibility)", callback=self.check_accessibility),
        ]

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        self.hotkeys.start()
        self._register_hotkeys()
        if not HotkeyMonitor.is_accessibility_trusted():
            HotkeyMonitor.request_accessibility()
        self.run()

    def _register_hotkeys(self) -> None:
        self.hotkeys.register("popup", KeyCombo.parse(self.settings.popup_hotkey), self._on_popup_hotkey)
        self.hotkeys.register("replace", KeyCombo.parse(self.settings.replace_hotkey), self._on_replace_hotkey)

    def _translator_instance(self) -> Translator:
        if self._translator is None:
            self._translator = Translator()
        return self._translator

    # -- actions ---------------------------------------------------------
    def _on_popup_hotkey(self) -> None:
        self.translate_selection(None)

    def _on_replace_hotkey(self) -> None:
        self.replace_selection(None)

    def translate_selection(self, _sender) -> None:
        text = capture_selected_text()
        if not text:
            rumps.notification("QTranslateMac", "Нет текста", "Выделите текст перед переводом.")
            return
        try:
            result = self._translator_instance().translate(
                text, source=self.settings.source_language, target=self.settings.target_language
            )
        except TranslationError as exc:
            rumps.notification("QTranslateMac", "Ошибка перевода", str(exc))
            return
        self.history.add(text, result, self.settings.source_language, self.settings.target_language)
        self.popup.show(text, result, on_replace=lambda: replace_selection(result))

    def replace_selection(self, _sender) -> None:
        text = capture_selected_text()
        if not text:
            rumps.notification("QTranslateMac", "Нет текста", "Выделите текст перед переводом.")
            return
        try:
            result = self._translator_instance().translate(
                text, source=self.settings.source_language, target=self.settings.target_language
            )
        except TranslationError as exc:
            rumps.notification("QTranslateMac", "Ошибка перевода", str(exc))
            return
        self.history.add(text, result, self.settings.source_language, self.settings.target_language)
        replace_selection(result)

    def show_history(self, _sender) -> None:
        if not self.history.entries:
            rumps.alert("История пуста", "Переводов пока нет.")
            return
        lines = [
            f"{language_name(e.source_language)} → {language_name(e.target_language)}\n"
            f"{e.source_text}\n→ {e.translated_text}"
            for e in self.history.entries[:15]
        ]
        rumps.alert("История переводов (последние 15)", "\n\n".join(lines))

    def show_settings(self, _sender) -> None:
        window = rumps.Window(
            title="Настройки QTranslateMac",
            message=(
                "Исходный/целевой язык, код ISO (например ru, en, auto):\n"
                "Формат: source,target"
            ),
            default_text=f"{self.settings.source_language},{self.settings.target_language}",
            ok="Сохранить",
            cancel="Отмена",
        )
        response = window.run()
        if response.clicked and "," in response.text:
            src, tgt = [p.strip() for p in response.text.split(",", 1)]
            if src and tgt:
                self.settings.source_language = src
                self.settings.target_language = tgt
                self.settings.save(default_settings_path())

    # -- hotkey configuration --------------------------------------------
    def _popup_hotkey_title(self) -> str:
        return f"Хоткей перевода: {KeyCombo.parse(self.settings.popup_hotkey).display()}"

    def _replace_hotkey_title(self) -> str:
        return f"Хоткей замены: {KeyCombo.parse(self.settings.replace_hotkey).display()}"

    def _update_hotkey_menu_titles(self) -> None:
        self.popup_hotkey_item.title = self._popup_hotkey_title()
        self.replace_hotkey_item.title = self._replace_hotkey_title()

    def show_popup_hotkey_settings(self, _sender) -> None:
        self._configure_hotkey(
            prompt_title="Хоткей: показать перевод во всплывающем окне",
            current=self.settings.popup_hotkey,
            identifier="popup",
            action=self._on_popup_hotkey,
            store=lambda combo: setattr(self.settings, "popup_hotkey", combo.to_string()),
        )

    def show_replace_hotkey_settings(self, _sender) -> None:
        self._configure_hotkey(
            prompt_title="Хоткей: заменить выделенное переводом",
            current=self.settings.replace_hotkey,
            identifier="replace",
            action=self._on_replace_hotkey,
            store=lambda combo: setattr(self.settings, "replace_hotkey", combo.to_string()),
        )

    def _configure_hotkey(
        self,
        prompt_title: str,
        current: str,
        identifier: str,
        action: Callable[[], None],
        store: Callable[[KeyCombo], None],
    ) -> None:
        window = rumps.Window(
            title=prompt_title,
            message=(
                "Формат: модификаторы через «+» и одна буква/цифра в конце.\n"
                "Модификаторы: ctrl, alt, shift, cmd. Например: cmd+alt+t"
            ),
            default_text=current,
            ok="Сохранить",
            cancel="Отмена",
        )
        response = window.run()
        if not response.clicked:
            return
        try:
            combo = KeyCombo.parse(response.text)
        except InvalidKeyCombo as exc:
            rumps.alert("Некорректная комбинация", str(exc))
            return

        # Conflict check: two hotkeys pointing at the same combo would mean
        # only one of them ever fires (HotkeyMonitor dispatches to the first
        # match), so refuse instead of silently shadowing the other action.
        other_value = self.settings.replace_hotkey if identifier == "popup" else self.settings.popup_hotkey
        if combo.to_string() == KeyCombo.parse(other_value).to_string():
            rumps.alert(
                "Комбинация уже занята",
                f"{combo.display()} уже используется другим действием. Выберите другую.",
            )
            return

        store(combo)
        self.settings.save(default_settings_path())
        self.hotkeys.unregister(identifier)
        self.hotkeys.register(identifier, combo, action)
        self._update_hotkey_menu_titles()
        rumps.alert("Готово", f"Новый хоткей: {combo.display()}")

    def check_accessibility(self, _sender) -> None:
        trusted = HotkeyMonitor.is_accessibility_trusted()
        if trusted:
            rumps.alert("Доступ есть", "Universal Access разрешён — хоткеи должны работать.")
        else:
            HotkeyMonitor.request_accessibility()
            rumps.alert(
                "Нужно разрешение",
                "Откройте System Settings → Privacy & Security → Accessibility "
                "и включите QTranslateMac.",
            )


def main() -> None:
    app = QTranslateMacApp()
    app.start()


if __name__ == "__main__":
    main()
