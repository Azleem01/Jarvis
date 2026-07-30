"""The OpenRouter provider: payload translation, fallback, and the model split.

Nothing here touches the network. The HTTP call is replaced, so what is tested
is the part that would fail silently: turning a google-genai payload into an
OpenAI one, and deciding which model is allowed to do what.

The pointing/vision split is the most important thing in this file. It is not a
style choice — it came out of a benchmark against generated quiz pages with
known option positions, and getting it wrong makes Azleem click the wrong answer
while reporting the right one.
"""

import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.genai import types  # noqa: E402

import config  # noqa: E402
import openrouter_client as orc  # noqa: E402


def reply(text="hello"):
    return json.dumps({"choices": [{"message": {"content": text}}]})


def http_error(code, body=""):
    return urllib.error.HTTPError("u", code, "err", {}, __import__("io").BytesIO(body.encode()))


class TestPayloadTranslation(unittest.TestCase):
    def test_image_becomes_a_data_uri(self):
        payload = orc.build_payload("m", [
            types.Part.from_bytes(data=b"\xff\xd8raw", mime_type="image/jpeg"),
            "describe this",
        ])
        content = payload["messages"][-1]["content"]
        self.assertEqual(content[0]["type"], "image_url")
        self.assertTrue(
            content[0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(content[1], {"type": "text", "text": "describe this"})

    def test_system_instruction_becomes_a_system_message(self):
        payload = orc.build_payload("m", ["hi"], types.GenerateContentConfig(
            system_instruction="you are a robot"))
        self.assertEqual(payload["messages"][0],
                         {"role": "system", "content": "you are a robot"})

    def test_generation_parameters_are_renamed(self):
        payload = orc.build_payload("m", ["hi"], types.GenerateContentConfig(
            temperature=0.25, max_output_tokens=64))
        self.assertEqual(payload["temperature"], 0.25)
        self.assertEqual(payload["max_tokens"], 64)

    def test_a_bare_string_is_accepted(self):
        payload = orc.build_payload("m", "just text")
        self.assertEqual(payload["messages"][-1]["content"],
                         [{"type": "text", "text": "just text"}])

    def test_max_tokens_always_present(self):
        """Some free models default to a tiny completion budget and truncate."""
        self.assertIn("max_tokens", orc.build_payload("m", ["hi"]))


class TestModelChains(unittest.TestCase):
    """The measured split between models that can point and models that cannot."""

    def test_pointing_chain_excludes_models_that_mislocate(self):
        """Nemotron answers correctly but misses boxes by up to 231/1000.

        A model in this list that cannot point selects the wrong option while
        reporting the right one — the worst possible failure, because it looks
        like success.
        """
        for model in config.OPENROUTER_POINTING_MODELS:
            self.assertNotIn(
                "nemotron", model.lower(),
                f"{model} was benchmarked as unable to point accurately; it "
                f"must not be in the pointing chain.",
            )

    def test_pointing_chain_is_not_empty(self):
        self.assertTrue(config.OPENROUTER_POINTING_MODELS)

    def test_vision_chain_is_a_superset(self):
        self.assertTrue(
            set(config.OPENROUTER_POINTING_MODELS)
            <= set(config.OPENROUTER_VISION_MODELS),
            "A model good enough to click with is certainly good enough to read.",
        )

    def test_every_configured_model_is_free(self):
        """The user asked for free models only — this is the guard on that."""
        for model in config.OPENROUTER_VISION_MODELS:
            self.assertTrue(
                model.endswith(":free"),
                f"{model} is not a ':free' model; it would spend real credit.",
            )

    def test_the_two_routers_are_distinct(self):
        self.assertIsNot(orc.get_router("vision"), orc.get_router("pointing"))


class TestFallback(unittest.TestCase):
    def setUp(self):
        self.router = orc.OpenRouterRouter(["model-a", "model-b"])
        patcher = mock.patch.object(config, "OPENROUTER_API_KEY", "test-key")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_first_working_model_answers(self):
        with mock.patch.object(self.router, "_post", return_value=reply("ok")):
            self.assertEqual(self.router.generate_content(contents=["x"]).text, "ok")

    def test_rate_limited_model_is_skipped_next_time(self):
        calls = []

        def post(payload):
            calls.append(payload["model"])
            if payload["model"] == "model-a":
                raise http_error(429, '{"error":{"message":"quota"}}')
            return reply("from b")

        with mock.patch.object(self.router, "_post", side_effect=post):
            self.assertEqual(self.router.generate_content(contents=["x"]).text, "from b")
            calls.clear()
            self.router.generate_content(contents=["x"])
        self.assertEqual(calls, ["model-b"], "a cooling-down model must be skipped")

    def test_upstream_429_gets_a_short_cooldown(self):
        """Seen live on gemma-4-31b after a single request.

        Provider-busy clears in seconds; treating it like a daily quota would
        park a perfectly good model for a minute.
        """
        body = '{"error":{"metadata":{"raw":"model-a:free is temporarily rate-limited upstream"}}}'
        self.assertTrue(orc._is_upstream_rate_limit(body))
        self.assertLess(orc._cooldown_for(429, body), config.MODEL_COOLDOWN_SECONDS)

    def test_plain_quota_429_gets_the_full_cooldown(self):
        self.assertGreaterEqual(orc._cooldown_for(429, '{"error":"quota"}'),
                                config.MODEL_COOLDOWN_SECONDS)

    def test_auth_failure_does_not_walk_the_chain(self):
        """401 fails identically everywhere; walking the chain hides the cause."""
        with mock.patch.object(self.router, "_post",
                               side_effect=http_error(401, "bad key")):
            with self.assertRaises(RuntimeError):
                self.router.generate_content(contents=["x"])

    def test_all_models_down_raises(self):
        with mock.patch.object(self.router, "_post", side_effect=http_error(429, "{}")):
            with self.assertRaises(orc.AllModelsUnavailable):
                self.router.generate_content(contents=["x"])

    def test_empty_reply_falls_through_instead_of_returning_nothing(self):
        """A 200 with no content would otherwise be parsed as JSON downstream."""
        posts = [json.dumps({"choices": []}), reply("real answer")]
        with mock.patch.object(self.router, "_post", side_effect=posts):
            self.assertEqual(self.router.generate_content(contents=["x"]).text,
                             "real answer")

    def test_missing_key_raises_not_configured(self):
        with mock.patch.object(config, "OPENROUTER_API_KEY", ""):
            with self.assertRaises(orc.NotConfigured):
                self.router.generate_content(contents=["x"])


class TestProviderDispatch(unittest.TestCase):
    """providers.vision_generate: OpenRouter first, Gemini as the safety net."""

    def test_falls_back_to_gemini_when_openrouter_is_exhausted(self):
        import providers

        with mock.patch.object(config, "OPENROUTER_API_KEY", "k"), \
             mock.patch.object(config, "VISION_PROVIDER", "openrouter"), \
             mock.patch.object(providers.openrouter_client, "generate_content",
                               side_effect=orc.AllModelsUnavailable("spent")), \
             mock.patch.object(providers.gemini_client, "generate_content",
                               return_value=mock.Mock(text="gemini")) as gem:
            self.assertEqual(providers.vision_generate(["x"]).text, "gemini")
        gem.assert_called_once()

    def test_gemini_only_when_configured_that_way(self):
        import providers

        with mock.patch.object(config, "VISION_PROVIDER", "gemini"), \
             mock.patch.object(providers.openrouter_client, "generate_content") as orx, \
             mock.patch.object(providers.gemini_client, "generate_content",
                               return_value=mock.Mock(text="g")):
            providers.vision_generate(["x"])
        orx.assert_not_called()

    def test_pointing_requests_use_the_pointing_chain(self):
        import providers

        with mock.patch.object(config, "OPENROUTER_API_KEY", "k"), \
             mock.patch.object(config, "VISION_PROVIDER", "openrouter"), \
             mock.patch.object(providers.openrouter_client, "generate_content",
                               return_value=mock.Mock(text="ok")) as orx:
            providers.vision_generate(["x"], needs_pointing=True)
        self.assertEqual(orx.call_args.kwargs["kind"], "pointing")

    def test_reading_requests_use_the_vision_chain(self):
        import providers

        with mock.patch.object(config, "OPENROUTER_API_KEY", "k"), \
             mock.patch.object(config, "VISION_PROVIDER", "openrouter"), \
             mock.patch.object(providers.openrouter_client, "generate_content",
                               return_value=mock.Mock(text="ok")) as orx:
            providers.vision_generate(["x"])
        self.assertEqual(orx.call_args.kwargs["kind"], "vision")


if __name__ == "__main__":
    unittest.main(verbosity=2)
