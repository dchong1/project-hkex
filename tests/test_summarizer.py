"""Unit tests for summarizer mode selection and none fallback (no network)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import config as app_config
from llm_summarizer import NO_LLM_NOTE, none_fallback_summary


class ResolveSummarizerModeTests(unittest.TestCase):
    def _clear(self) -> None:
        for key in (
            "SUMMARIZER",
            "LLM_PROVIDER",
            "GROK_API_KEY",
        ):
            os.environ.pop(key, None)

    def setUp(self) -> None:
        self._clear()

    def tearDown(self) -> None:
        self._clear()

    def test_defaults_to_none_without_key_or_explicit_mode(self) -> None:
        self.assertEqual(app_config.resolve_summarizer_mode(), "none")

    def test_grok_when_grok_api_key_set_and_mode_unset(self) -> None:
        os.environ["GROK_API_KEY"] = "xai-test"
        self.assertEqual(app_config.resolve_summarizer_mode(), "grok")

    def test_explicit_none_overrides_grok_key(self) -> None:
        os.environ["GROK_API_KEY"] = "xai-test"
        os.environ["SUMMARIZER"] = "none"
        self.assertEqual(app_config.resolve_summarizer_mode(), "none")

    def test_explicit_grok(self) -> None:
        os.environ["SUMMARIZER"] = "grok"
        self.assertEqual(app_config.resolve_summarizer_mode(), "grok")

    def test_openai_compatible_alias(self) -> None:
        os.environ["LLM_PROVIDER"] = "openai-compatible"
        self.assertEqual(app_config.resolve_summarizer_mode(), "openai")

    def test_invalid_mode_raises(self) -> None:
        os.environ["SUMMARIZER"] = "anthropic"
        with self.assertRaises(RuntimeError):
            app_config.resolve_summarizer_mode()


class NoneFallbackSummaryTests(unittest.TestCase):
    def test_includes_title_category_and_note(self) -> None:
        text = none_fallback_summary(
            "Interim results announcement",
            "Announcements and Notices",
        )
        self.assertIn("Interim results announcement", text)
        self.assertIn("Announcements and Notices", text)
        self.assertIn(NO_LLM_NOTE, text)

    def test_word_cap(self) -> None:
        long_title = "word " * 200
        text = none_fallback_summary(long_title, "Cat")
        self.assertLessEqual(len(text.split()), 70)

    def test_empty_title_uses_placeholder(self) -> None:
        text = none_fallback_summary("", "")
        self.assertIn("HKEX announcement", text)
        self.assertIn("General", text)


class CreateSummarizerTests(unittest.TestCase):
    def test_none_mode_does_not_require_grok_key(self) -> None:
        from llm_summarizer import NoneSummarizer, create_summarizer

        env = {k: v for k, v in os.environ.items() if k not in ("GROK_API_KEY", "SUMMARIZER", "LLM_PROVIDER")}
        env["SUMMARIZER"] = "none"
        with patch.dict(os.environ, env, clear=True):
            s = create_summarizer()
        self.assertIsInstance(s, NoneSummarizer)

    def test_grok_mode_requires_key(self) -> None:
        from llm_summarizer import create_summarizer

        env = {k: v for k, v in os.environ.items() if k not in ("GROK_API_KEY", "SUMMARIZER")}
        env["SUMMARIZER"] = "grok"
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                create_summarizer()


if __name__ == "__main__":
    unittest.main()
