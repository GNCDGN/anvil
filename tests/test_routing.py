"""anvil/routing.py model-selection seam tests (v4 Phase 1a Steps 1 + 3).

Covers the public surfaces — MODEL_ALIASES, resolve_model, client_for_model,
call_model_for_subtask — plus the module-load invariant: alias resolution,
bare-version-string passthrough, unknown-name warn-and-fallback (debounced,
never-raises), the None/default path, and the Step 3 sub-task entry point's
lightweight never-raises+retry wrapper (Amendment 3) with no brief-block
caching (Q-A3).

Hermetic: no network, no API key required (the shared client is faked via
routing._client; anthropic constructs without a key anyway). `sonnet` was
dropped in Phase 1a (Amendment 1) and restored in v4 Phase 3a (Step 0 Q-A5 /
DC4) for the Phase 3c screen-aware vision interpreter.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import anthropic
import httpx

from anvil import routing
from anvil.events import MODEL_RATES
from anvil.routing import (
    DEFAULT_MODEL,
    MODEL_ALIASES,
    call_model_for_subtask,
    client_for_model,
    resolve_model,
)

_ROUTING_LOGGER = "anvil.routing"


def _fake_message(*text_blocks: str):
    """A fake non-streaming SDK response: .content is a list of text blocks."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=t) for t in text_blocks]
    )


def _api_timeout() -> anthropic.APITimeoutError:
    return anthropic.APITimeoutError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )


def _rate_limit() -> anthropic.RateLimitError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.RateLimitError(
        "rate limited", response=httpx.Response(429, request=req), body=None
    )


class TestModelAliases(unittest.TestCase):
    def test_alias_contents(self):
        # Q-A1 + v4 Phase 3a (Step 0 Q-A5 / DC4): opus, sonnet, haiku. Sonnet was
        # dropped in Phase 1a (Amendment 1) and restored in 3a for the 3c vision
        # interpreter (its rate is now in MODEL_RATES).
        self.assertEqual(
            MODEL_ALIASES,
            {
                "opus": "claude-opus-4-7",
                "sonnet": "claude-sonnet-4-6",
                "haiku": "claude-haiku-4-5-20251001",
            },
        )

    def test_sonnet_alias_restored(self):
        # v4 Phase 3a (Step 0 Q-A5 / DC4): sonnet restored (dropped in Phase 1a,
        # Amendment 1) — alias present, target resolvable, rate registered.
        self.assertEqual(MODEL_ALIASES.get("sonnet"), "claude-sonnet-4-6")
        self.assertEqual(resolve_model("sonnet"), "claude-sonnet-4-6")
        self.assertIn("claude-sonnet-4-6", MODEL_RATES)

    def test_module_load_invariant_targets_in_model_rates(self):
        # The same condition routing.py asserts at module load: every alias
        # target is a known model in MODEL_RATES. Fails loud if a future
        # config-author points an alias at an unregistered model.
        for alias, target in MODEL_ALIASES.items():
            with self.subTest(alias=alias):
                self.assertIn(target, MODEL_RATES)


class TestResolveModel(unittest.TestCase):
    """resolve_model — promoted to public in Step 3 (Amendment 5)."""

    def test_alias_resolves_to_version_string(self):
        self.assertEqual(resolve_model("opus"), "claude-opus-4-7")
        self.assertEqual(resolve_model("haiku"), "claude-haiku-4-5-20251001")

    def test_known_version_string_passthrough(self):
        for version in ("claude-opus-4-7", "claude-haiku-4-5-20251001"):
            with self.subTest(version=version):
                self.assertEqual(resolve_model(version), version)

    def test_none_and_empty_return_default(self):
        self.assertEqual(resolve_model(None), DEFAULT_MODEL)
        self.assertEqual(resolve_model(""), DEFAULT_MODEL)

    def test_unknown_name_falls_back_to_default_and_warns(self):
        routing._warned_unknown.discard("gpt-4")  # order-independent
        with self.assertLogs(_ROUTING_LOGGER, level="WARNING") as cm:
            resolved = resolve_model("gpt-4")
        self.assertEqual(resolved, DEFAULT_MODEL)
        self.assertTrue(any("gpt-4" in line for line in cm.output))

    def test_unknown_warning_debounced_once_per_process(self):
        routing._warned_unknown.discard("debounce-probe")
        with self.assertLogs(_ROUTING_LOGGER, level="WARNING") as cm1:
            self.assertEqual(resolve_model("debounce-probe"), DEFAULT_MODEL)
        self.assertTrue(any("debounce-probe" in line for line in cm1.output))
        # Same unknown name again: still falls back, but does NOT warn again.
        with self.assertNoLogs(_ROUTING_LOGGER, level="WARNING"):
            self.assertEqual(resolve_model("debounce-probe"), DEFAULT_MODEL)


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
        routing._warned_unknown.discard("not-a-real-alias")
        with self.assertLogs(_ROUTING_LOGGER, level="WARNING") as cm:
            client = client_for_model("not-a-real-alias")
        self.assertIsInstance(client, anthropic.Anthropic)
        self.assertTrue(any("not-a-real-alias" in line for line in cm.output))

    def test_unknown_version_string_warns_and_returns_client_no_raise(self):
        routing._warned_unknown.discard("claude-nonexistent-9")
        with self.assertLogs(_ROUTING_LOGGER, level="WARNING") as cm:
            client = client_for_model("claude-nonexistent-9")
        self.assertIsInstance(client, anthropic.Anthropic)
        self.assertTrue(any("claude-nonexistent-9" in line for line in cm.output))


class TestCallModelForSubtask(unittest.TestCase):
    """v4 Phase 1a Step 3 (Amendment 3): the generic sub-task entry point —
    resolve + call + lightweight never-raises+retry, no brief-block caching.
    The shared client is faked via routing._client; time.sleep is patched so
    retries don't actually sleep."""

    @staticmethod
    def _fake_client(*, create_side_effect=None, create_return=None):
        client = mock.MagicMock()
        if create_side_effect is not None:
            client.messages.create.side_effect = create_side_effect
        else:
            client.messages.create.return_value = create_return
        return client

    def test_returns_text_on_normal_response(self):
        client = self._fake_client(create_return=_fake_message("hello world"))
        with mock.patch.object(routing, "_client", return_value=client):
            out = call_model_for_subtask("haiku", "sys", "user")
        self.assertIsInstance(out, str)
        self.assertEqual(out, "hello world")

    def test_concatenates_multiple_text_blocks(self):
        client = self._fake_client(create_return=_fake_message("a", "b", "c"))
        with mock.patch.object(routing, "_client", return_value=client):
            out = call_model_for_subtask("opus", "sys", "user")
        self.assertEqual(out, "abc")

    def test_resolves_alias_for_model_param(self):
        client = self._fake_client(create_return=_fake_message("ok"))
        with mock.patch.object(routing, "_client", return_value=client):
            call_model_for_subtask("haiku", "sys", "user")
        self.assertEqual(
            client.messages.create.call_args.kwargs["model"],
            "claude-haiku-4-5-20251001",
        )

    def test_no_cache_control_in_sdk_call(self):
        # Q-A3: system passed as a plain string, no cache_control anywhere.
        client = self._fake_client(create_return=_fake_message("ok"))
        with mock.patch.object(routing, "_client", return_value=client):
            call_model_for_subtask("opus", "SYS-PROMPT", "user-msg")
        kwargs = client.messages.create.call_args.kwargs
        self.assertEqual(kwargs["system"], "SYS-PROMPT")  # plain string
        self.assertNotIn("cache_control", repr(kwargs))
        self.assertEqual(
            kwargs["messages"], [{"role": "user", "content": "user-msg"}]
        )

    def test_retries_once_on_api_timeout_then_succeeds(self):
        client = self._fake_client(
            create_side_effect=[_api_timeout(), _fake_message("recovered")]
        )
        with mock.patch.object(routing, "_client", return_value=client), \
                mock.patch.object(routing.time, "sleep") as sleep:
            out = call_model_for_subtask("opus", "sys", "user")
        self.assertEqual(out, "recovered")
        self.assertEqual(client.messages.create.call_count, 2)
        self.assertTrue(sleep.called)

    def test_retries_once_on_rate_limit_then_succeeds(self):
        client = self._fake_client(
            create_side_effect=[_rate_limit(), _fake_message("ok")]
        )
        with mock.patch.object(routing, "_client", return_value=client), \
                mock.patch.object(routing.time, "sleep") as sleep:
            out = call_model_for_subtask("opus", "sys", "user")
        self.assertEqual(out, "ok")
        self.assertEqual(client.messages.create.call_count, 2)
        self.assertTrue(sleep.called)

    def test_two_transient_failures_return_structured_error(self):
        client = self._fake_client(
            create_side_effect=[_api_timeout(), _api_timeout()]
        )
        with mock.patch.object(routing, "_client", return_value=client), \
                mock.patch.object(routing.time, "sleep"):
            out = call_model_for_subtask("opus", "sys", "user")
        self.assertTrue(out.startswith("[call_model_for_subtask error:"))
        self.assertEqual(client.messages.create.call_count, 2)

    def test_terminal_error_returns_structured_error_no_retry(self):
        client = self._fake_client(create_side_effect=RuntimeError("boom"))
        with mock.patch.object(routing, "_client", return_value=client), \
                mock.patch.object(routing.time, "sleep") as sleep:
            out = call_model_for_subtask("opus", "sys", "user")
        self.assertEqual(out, "[call_model_for_subtask error: boom]")
        self.assertEqual(client.messages.create.call_count, 1)  # no retry
        self.assertFalse(sleep.called)


if __name__ == "__main__":
    unittest.main()
