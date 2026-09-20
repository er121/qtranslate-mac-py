"""Platform-independent logic: settings, history, hotkey combos, translation.

Nothing in this file imports AppKit/pyobjc/rumps, so it runs and can be
unit-tested on any OS (Linux, Windows, macOS) with plain CPython.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Languages
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Language:
    code: str
    name: str


LANGUAGES: list[Language] = [
    Language("auto", "Определить автоматически"),
    Language("en", "Английский"),
    Language("ru", "Русский"),
    Language("de", "Немецкий"),
    Language("fr", "Французский"),
    Language("es", "Испанский"),
    Language("it", "Итальянский"),
    Language("pt", "Португальский"),
    Language("zh-CN", "Китайский (упрощ.)"),
    Language("ja", "Японский"),
    Language("ko", "Корейский"),
    Language("tr", "Турецкий"),
    Language("pl", "Польский"),
    Language("uk", "Украинский"),
    Language("ar", "Арабский"),
    Language("hi", "Хинди"),
]

LANGUAGE_BY_CODE = {lang.code: lang for lang in LANGUAGES}


def language_name(code: str) -> str:
    lang = LANGUAGE_BY_CODE.get(code)
    return lang.name if lang else code


# ---------------------------------------------------------------------------
# Hotkey combo — a small, testable string representation
# ---------------------------------------------------------------------------

_MODIFIER_ORDER = ["ctrl", "alt", "shift", "cmd"]
_MODIFIER_SYMBOLS = {"ctrl": "⌃", "alt": "⌥", "shift": "⇧", "cmd": "⌘"}
_VALID_MODIFIERS = set(_MODIFIER_ORDER)


class InvalidKeyCombo(ValueError):
    pass


@dataclass(frozen=True)
class KeyCombo:
    """A hotkey such as "cmd+alt+t". Stored/parsed as lowercase, '+'-joined,
    modifiers in a fixed canonical order, followed by exactly one key."""

    modifiers: tuple[str, ...]
    key: str

    @staticmethod
    def parse(text: str) -> "KeyCombo":
        if not text or not text.strip():
            raise InvalidKeyCombo("Пустая комбинация клавиш")
        parts = [p.strip().lower() for p in text.split("+") if p.strip()]
        if len(parts) < 2:
            raise InvalidKeyCombo(f"Нужен минимум один модификатор и клавиша: {text!r}")
        *mods, key = parts
        for m in mods:
            if m not in _VALID_MODIFIERS:
                raise InvalidKeyCombo(f"Неизвестный модификатор: {m!r}")
        if not re.fullmatch(r"[a-z0-9]", key):
            raise InvalidKeyCombo(f"Клавиша должна быть одной буквой или цифрой: {key!r}")
        ordered = tuple(m for m in _MODIFIER_ORDER if m in mods)
        return KeyCombo(modifiers=ordered, key=key)

    def to_string(self) -> str:
        return "+".join([*self.modifiers, self.key])

    def display(self) -> str:
        symbols = "".join(_MODIFIER_SYMBOLS[m] for m in self.modifiers)
        return f"{symbols}{self.key.upper()}"


DEFAULT_POPUP_HOTKEY = KeyCombo.parse("cmd+alt+t")
DEFAULT_REPLACE_HOTKEY = KeyCombo.parse("cmd+alt+r")
DEFAULT_MAIN_WINDOW_HOTKEY = KeyCombo.parse("cmd+alt+m")
DEFAULT_SPEAK_HOTKEY = KeyCombo.parse("cmd+alt+l")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    source_language: str = "auto"
    target_language: str = "ru"
    popup_hotkey: str = DEFAULT_POPUP_HOTKEY.to_string()
    replace_hotkey: str = DEFAULT_REPLACE_HOTKEY.to_string()
    main_window_hotkey: str = DEFAULT_MAIN_WINDOW_HOTKEY.to_string()
    speak_hotkey: str = DEFAULT_SPEAK_HOTKEY.to_string()
    popup_autohide_seconds: float = 12.0
    max_history_items: int = 200

    def swapped_languages(self) -> "Settings":
        if self.source_language == "auto":
            return self
        new = Settings(**asdict(self))
        new.source_language, new.target_language = self.target_language, self.source_language
        return new

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @staticmethod
    def from_json(text: str) -> "Settings":
        data = json.loads(text)
        known = {f for f in Settings.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in known}
        return Settings(**filtered)

    @staticmethod
    def load(path: Path) -> "Settings":
        if not path.exists():
            return Settings()
        try:
            return Settings.from_json(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError, ValueError):
            return Settings()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")


def default_settings_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "QTranslateMac" / "settings.json"


def default_history_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "QTranslateMac" / "history.json"


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

@dataclass
class HistoryEntry:
    source_text: str
    translated_text: str
    source_language: str
    target_language: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    favorite: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "HistoryEntry":
        known = {f for f in HistoryEntry.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in known}
        return HistoryEntry(**filtered)


class HistoryStore:
    """In-memory history list with JSON persistence. Newest entries first."""

    def __init__(self, path: Optional[Path] = None, max_items: int = 200):
        self.path = path
        self.max_items = max_items
        self.entries: list[HistoryEntry] = []
        if path is not None:
            self.load()

    def add(self, source_text: str, translated_text: str, source_language: str, target_language: str) -> HistoryEntry:
        entry = HistoryEntry(
            source_text=source_text,
            translated_text=translated_text,
            source_language=source_language,
            target_language=target_language,
        )
        self.entries.insert(0, entry)
        if len(self.entries) > self.max_items:
            del self.entries[self.max_items:]
        self._persist()
        return entry

    def toggle_favorite(self, entry_id: str) -> None:
        for e in self.entries:
            if e.id == entry_id:
                e.favorite = not e.favorite
                break
        self._persist()

    def remove(self, entry_id: str) -> None:
        self.entries = [e for e in self.entries if e.id != entry_id]
        self._persist()

    def clear(self, keep_favorites: bool = True) -> None:
        self.entries = [e for e in self.entries if keep_favorites and e.favorite]
        self._persist()

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = [HistoryEntry.from_dict(d) for d in raw]
        except (json.JSONDecodeError, TypeError, ValueError):
            self.entries = []

    def _persist(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps([e.to_dict() for e in self.entries], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------

class TranslationError(RuntimeError):
    pass


class Translator:
    """Wraps `deep-translator`'s GoogleTranslator (free, no API key, needs
    internet). The import is deferred to the constructor so that code which
    doesn't need translation (e.g. settings/history tests) never requires the
    dependency to be installed.
    """

    def __init__(self):
        try:
            from deep_translator import GoogleTranslator  # type: ignore
        except ImportError as exc:  # pragma: no cover - exercised via error path test
            raise TranslationError(
                "Пакет 'deep-translator' не установлен. Установите: pip install deep-translator"
            ) from exc
        self._GoogleTranslator = GoogleTranslator

    def translate(self, text: str, source: str, target: str) -> str:
        text = text.strip()
        if not text:
            raise TranslationError("Нет текста для перевода")
        src = "auto" if source == "auto" else source
        try:
            translator = self._GoogleTranslator(source=src, target=target)
            result = translator.translate(text)
        except Exception as exc:  # noqa: BLE001 - surface as our own error type
            raise TranslationError(f"Ошибка перевода: {exc}") from exc
        if not result:
            raise TranslationError("Пустой ответ от сервиса перевода")
        return result
