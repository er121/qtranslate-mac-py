"""Unit tests for qtranslate_mac.core — pure logic, no macOS dependency.

Run locally with:  python3 -m unittest discover -s tests -v
(stdlib unittest is used instead of pytest so these tests run anywhere,
even where `pip install pytest` isn't possible, e.g. offline sandboxes.)
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qtranslate_mac.core import (  # noqa: E402
    KeyCombo,
    InvalidKeyCombo,
    Settings,
    HistoryStore,
    Translator,
    TranslationError,
    language_name,
)


class KeyComboTests(unittest.TestCase):
    def test_parse_basic(self):
        combo = KeyCombo.parse("cmd+alt+t")
        self.assertEqual(combo.key, "t")
        self.assertIn("cmd", combo.modifiers)
        self.assertIn("alt", combo.modifiers)

    def test_canonical_order_independent_of_input_order(self):
        a = KeyCombo.parse("cmd+alt+t")
        b = KeyCombo.parse("alt+cmd+t")
        self.assertEqual(a.modifiers, b.modifiers)
        self.assertEqual(a.to_string(), b.to_string())

    def test_round_trip_string(self):
        combo = KeyCombo.parse("ctrl+shift+alt+cmd+9")
        self.assertEqual(KeyCombo.parse(combo.to_string()), combo)

    def test_display_has_symbols_and_uppercase_key(self):
        combo = KeyCombo.parse("cmd+alt+t")
        self.assertIn("T", combo.display())

    def test_rejects_empty(self):
        with self.assertRaises(InvalidKeyCombo):
            KeyCombo.parse("")

    def test_rejects_missing_key(self):
        with self.assertRaises(InvalidKeyCombo):
            KeyCombo.parse("cmd+alt")

    def test_rejects_unknown_modifier(self):
        with self.assertRaises(InvalidKeyCombo):
            KeyCombo.parse("fn+t")

    def test_rejects_multi_char_key(self):
        with self.assertRaises(InvalidKeyCombo):
            KeyCombo.parse("cmd+tab")


class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        s = Settings()
        self.assertEqual(s.source_language, "auto")
        self.assertEqual(s.target_language, "ru")
        # Canonical modifier order follows Apple's own menu convention
        # (⌃ control, ⌥ option/alt, ⇧ shift, ⌘ command), so "alt+cmd+t".
        self.assertEqual(KeyCombo.parse(s.popup_hotkey).to_string(), "alt+cmd+t")

    def test_json_round_trip(self):
        s = Settings(target_language="de", popup_autohide_seconds=5)
        restored = Settings.from_json(s.to_json())
        self.assertEqual(restored, s)

    def test_swap_languages(self):
        s = Settings(source_language="en", target_language="ru")
        swapped = s.swapped_languages()
        self.assertEqual(swapped.source_language, "ru")
        self.assertEqual(swapped.target_language, "en")

    def test_swap_noop_when_auto(self):
        s = Settings(source_language="auto", target_language="ru")
        swapped = s.swapped_languages()
        self.assertEqual(swapped.source_language, "auto")
        self.assertEqual(swapped.target_language, "ru")

    def test_load_missing_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "nope" / "settings.json"
            s = Settings.load(path)
            self.assertEqual(s, Settings())

    def test_save_then_load_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            original = Settings(target_language="fr")
            original.save(path)
            loaded = Settings.load(path)
            self.assertEqual(loaded, original)

    def test_load_tolerates_corrupt_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            path.write_text("{not valid json", encoding="utf-8")
            s = Settings.load(path)
            self.assertEqual(s, Settings())

    def test_load_ignores_unknown_fields_forward_compat(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            data = {"target_language": "es", "some_future_field": 123}
            path.write_text(json.dumps(data), encoding="utf-8")
            s = Settings.load(path)
            self.assertEqual(s.target_language, "es")


class HistoryStoreTests(unittest.TestCase):
    def test_add_inserts_newest_first(self):
        store = HistoryStore()
        store.add("hello", "привет", "en", "ru")
        store.add("bye", "пока", "en", "ru")
        self.assertEqual(store.entries[0].source_text, "bye")
        self.assertEqual(store.entries[1].source_text, "hello")

    def test_max_items_enforced(self):
        store = HistoryStore(max_items=3)
        for i in range(5):
            store.add(f"text {i}", f"translated {i}", "en", "ru")
        self.assertEqual(len(store.entries), 3)
        # newest 3 survive: "text 4", "text 3", "text 2"
        self.assertEqual(store.entries[0].source_text, "text 4")
        self.assertEqual(store.entries[-1].source_text, "text 2")

    def test_toggle_favorite(self):
        store = HistoryStore()
        entry = store.add("hi", "привет", "en", "ru")
        self.assertFalse(entry.favorite)
        store.toggle_favorite(entry.id)
        self.assertTrue(store.entries[0].favorite)
        store.toggle_favorite(entry.id)
        self.assertFalse(store.entries[0].favorite)

    def test_remove(self):
        store = HistoryStore()
        entry = store.add("hi", "привет", "en", "ru")
        store.remove(entry.id)
        self.assertEqual(len(store.entries), 0)

    def test_clear_keeps_favorites_by_default(self):
        store = HistoryStore()
        kept = store.add("keep me", "оставь", "en", "ru")
        store.toggle_favorite(kept.id)
        store.add("drop me", "удали", "en", "ru")
        store.clear(keep_favorites=True)
        self.assertEqual(len(store.entries), 1)
        self.assertEqual(store.entries[0].source_text, "keep me")

    def test_clear_all(self):
        store = HistoryStore()
        store.add("a", "а", "en", "ru")
        store.clear(keep_favorites=False)
        self.assertEqual(len(store.entries), 0)

    def test_persistence_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "history.json"
            store = HistoryStore(path=path)
            store.add("persist me", "сохрани меня", "en", "ru")

            reopened = HistoryStore(path=path)
            self.assertEqual(len(reopened.entries), 1)
            self.assertEqual(reopened.entries[0].source_text, "persist me")

    def test_persistence_tolerates_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "does_not_exist_yet.json"
            store = HistoryStore(path=path)
            self.assertEqual(store.entries, [])


class LanguageTests(unittest.TestCase):
    def test_known_code(self):
        self.assertEqual(language_name("ru"), "Русский")

    def test_unknown_code_falls_back_to_code_itself(self):
        self.assertEqual(language_name("xx"), "xx")


class TranslatorLiveTests(unittest.TestCase):
    """These actually call the translation backend. They are skipped
    automatically if `deep-translator` isn't installed or there's no
    internet — which is expected on an offline machine — but they DO run,
    for real, in this project's GitHub Actions CI (both the Linux and the
    macOS jobs), which is where the translation logic gets genuinely
    verified end-to-end.
    """

    @classmethod
    def setUpClass(cls):
        try:
            cls.translator = Translator()
        except TranslationError:
            cls.translator = None

    def test_translate_hello_to_russian(self):
        if self.translator is None:
            self.skipTest("deep-translator not installed in this environment")
        try:
            result = self.translator.translate("hello", source="en", target="ru")
        except TranslationError as exc:
            self.skipTest(f"no network / translation backend unavailable: {exc}")
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)
        # "привет" is the expected Google Translate output for "hello" -> ru.
        self.assertIn("привет", result.lower())

    def test_empty_text_raises(self):
        if self.translator is None:
            self.skipTest("deep-translator not installed in this environment")
        with self.assertRaises(TranslationError):
            self.translator.translate("   ", source="en", target="ru")


if __name__ == "__main__":
    unittest.main()
