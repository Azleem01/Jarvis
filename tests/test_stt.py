"""Transcription post-processing: name-correction and contact hotword biasing.

Offline — exercises the pure helpers in stt_engine directly, so no Whisper model
is loaded and nothing is downloaded.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import stt_engine  # noqa: E402


class CorrectionTests(unittest.TestCase):
    def test_fixes_the_assistant_name_mishears(self):
        corr = stt_engine._load_corrections()
        self.assertEqual(
            stt_engine._correct("hey diabetes open notepad", corr).lower(),
            "hey jarvis open notepad",
        )

    def test_leaves_ordinary_words_alone(self):
        corr = stt_engine._load_corrections()
        sentence = "open notepad and search downloads for the report"
        self.assertEqual(stt_engine._correct(sentence, corr), sentence)

    def test_is_whole_word_only(self):
        # "javascript" -> "Jarvis", but not a substring inside another word.
        corr = stt_engine._load_corrections()
        self.assertEqual(stt_engine._correct("javascripting", corr), "javascripting")

    def test_user_corrections_merge_in(self):
        with mock.patch.object(config, "STT_CORRECTIONS", '{"travis":"Jarvis"}'):
            corr = stt_engine._load_corrections()
        self.assertIn("jarvis", stt_engine._correct("hey travis", corr).lower())

    def test_bad_user_corrections_fall_back(self):
        with mock.patch.object(config, "STT_CORRECTIONS", "{ not json"):
            corr = stt_engine._load_corrections()
        self.assertEqual(corr["diabetes"], "Jarvis")  # defaults survive


class HotwordTests(unittest.TestCase):
    def test_includes_the_assistant_name_and_contacts(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(path)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with mock.patch.object(config, "WHATSAPP_CONTACTS", {"boss": "+1"}), \
                mock.patch.object(config, "WHATSAPP_CONTACT_CACHE", path):
            hot = stt_engine._load_hotwords().lower()
        self.assertIn("jarvis", hot)
        self.assertIn("boss", hot)

    def test_survives_a_missing_cache(self):
        with mock.patch.object(config, "WHATSAPP_CONTACTS", {}), \
                mock.patch.object(config, "WHATSAPP_CONTACT_CACHE", "/no/such/file.json"):
            hot = stt_engine._load_hotwords()
        self.assertIn("Jarvis", hot)


if __name__ == "__main__":
    unittest.main(verbosity=2)
