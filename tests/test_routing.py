"""v4 Phase 1a Step 1 — anvil/routing.py model-selection seam tests.

Covers the two public surfaces (MODEL_ALIASES, client_for_model) plus the
private resolver they share: alias resolution, bare-version-string passthrough,
unknown-name warn-and-fallback (never-raises), the None/default path, and the
module-load invariant (every MODEL_ALIASES target exists in MODEL_RATES).

Hermetic: no network, no API key required (anthropic 0.102.0 constructs a
client without a key; no call is made). Per brief Amendment 1, `sonnet` is not
an alias in Phase 1a.
"""
from __future__ import annotations

import unittest

import anthropic

from anvil import routing
from anvil.routing import DEFAULT_MODEL, MODEL_ALIASES, client_for_model
from tools.harness_v2 import MODEL_RATES

_ROUTING_LOGGER = "anvil.routing"


class TestModelAliases(unittest.TestCase):
    def test_alias_contents(self):
        # Q-A1 disposition + Amendment 1: exactly opus and haiku, no sonnet.
        self.assertEqual(
            MODEL_ALIASES,
            {"opus": "claude-opus-4-7", "haiku": "claude-haiku-4-5-20251001"},
        )

    def test_no_sonnet_alias(self):
        # Amendment 1 guard: sonnet was dropped (no MODEL_RATES entry).
        self.assertNotIn("sonnet", MODEL_ALIASES)

    def test_module_load_invariant_targets_in_model_rates(self):
        # The same condition routing.py asserts at module load: every alias
        # target is a known model in MODEL_RATES. Fails loud if a future
        # config-author points an alias at an unregistered model.
        for alias, target in MODEL_ALIASES.items():
            with self.subTest(alias=alias):
                self.assertIn(target, MODEL_RATES)


class TestResolve(unittest.TestCase):
    def test_alias_resolves_to_version_string(self):
        self.assertEqual(routing._resolve("opus"), "claude-opus-4-7")
        self.assertEqual(routing._resolve("haiku"), "claude-haiku-4-5-20251001")

    def test_known_version_string_passthrough(self):
        for version in ("claude-opus-4-7", "claude-haiku-4-5-20251001"):
            with self.subTest(version=version):
                self.assertEqual(routing._resolve(version), version)

    def test_none_and_empty_return_default(self):
        self.assertEqual(routing._resolve(None), DEFAULT_MODEL)
        self.assertEqual(routing._resolve(""), DEFAULT_MODEL)

    def test_unknown_name_falls_back_to_default_and_warns(self):
        with self.assertLogs(_ROUTING_LOGGER, level="WARNING") as cm:
            resolved = routing._resolve("gpt-4")
        self.assertEqual(resolved, DEFAULT_MODEL)
        self.assertTrue(any("gpt-4" in line for line in cm.output))


class TestClientForModel(unittest.TestCase):
    def test_alias_returns_client_no_warn(self):
        with self.assertNoLogs(_ROUTING_LOGGER, level="WARNING"):
            client = client_for_model("opus")
        self.assertIsInstance(client, anthropic.Anthropic)

    def test_version_string_returns_client_no_warn(self):
        with self.assertNoLogs(_ROUTING_LOGGER, level="WARNING"):
            client = client_for_model("claude-haiku-4-5-20251001")
        self.assertIsInstance(client, anthropic.Anthropic)

    def test_none_returns_default_client_no_warn(self):
        with self.assertNoLogs(_ROUTING_LOGGER, level="WARNING"):
            client = client_for_model(None)
        self.assertIsInstance(client, anthropic.Anthropic)

    def test_unknown_alias_warns_and_returns_client_no_raise(self):
        with self.assertLogs(_ROUTING_LOGGER, level="WARNING") as cm:
            client = client_for_model("not-a-real-alias")
        self.assertIsInstance(client, anthropic.Anthropic)
        self.assertTrue(any("not-a-real-alias" in line for line in cm.output))

    def test_unknown_version_string_warns_and_returns_client_no_raise(self):
        with self.assertLogs(_ROUTING_LOGGER, level="WARNING") as cm:
            client = client_for_model("claude-nonexistent-9")
        self.assertIsInstance(client, anthropic.Anthropic)
        self.assertTrue(any("claude-nonexistent-9" in line for line in cm.output))


if __name__ == "__main__":
    unittest.main()
