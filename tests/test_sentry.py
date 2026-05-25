"""v4 Phase 1b Step 2: tests for anvil/integrations/sentry.py.

Every Sentry call is REST-mocked by patching ``urllib.request.urlopen`` (Q-B6 —
hermetic; no live Sentry, no network). No SENTRY_API_TOKEN is required to run
the suite: the missing-token tests intentionally leave it unset, and every other
test sets a dummy token AND mocks urlopen so no real request is made. Coverage:
request URL + Authorization header, JSON-result parsing, the never-raises
contract (4xx / 5xx / malformed JSON / connection error / missing token), and
scope enforcement (a read function under no scope is refused WITHOUT any HTTP
call).
"""
from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

from anvil.integrations import sentry

_TOKEN_ENV = {"SENTRY_API_TOKEN": "test-token", "SENTRY_ORG": "anvil"}


def _resp(body, status: int = 200):
    """A urlopen return value supporting the context-manager + .read()/.status
    protocol sentry._get reads."""
    m = mock.MagicMock()
    m.read.return_value = body.encode() if isinstance(body, str) else body
    m.status = status
    m.getcode.return_value = status
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    return m


def _http_error(code: int, body: str = ""):
    return urllib.error.HTTPError(
        url="https://sentry.io/api/0/x/", code=code, msg="err",
        hdrs=None, fp=io.BytesIO(body.encode()),
    )


def _patch_urlopen(**kwargs):
    return mock.patch("urllib.request.urlopen", **kwargs)


def _with_token():
    return mock.patch.dict("os.environ", _TOKEN_ENV)


class TestListIssues(unittest.TestCase):
    def test_happy_path_url_auth_and_parse(self) -> None:
        issues = [{"id": "1", "title": "boom", "level": "error"}]
        captured: dict = {}

        def fake_urlopen(req, *a, **kw):
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
            return _resp(json.dumps(issues))

        with _with_token(), _patch_urlopen(side_effect=fake_urlopen):
            res = sentry.list_issues("anvil", scope="read")
        self.assertTrue(res["ok"])
        self.assertEqual(res["result"], issues)
        self.assertIn("/projects/anvil/anvil/issues/", captured["url"])
        self.assertEqual(captured["auth"], "Bearer test-token")

    def test_since_param_in_url(self) -> None:
        captured: dict = {}

        def fake_urlopen(req, *a, **kw):
            captured["url"] = req.full_url
            return _resp("[]")

        with _with_token(), _patch_urlopen(side_effect=fake_urlopen):
            res = sentry.list_issues(
                "anvil", since="2026-06-01T00:00:00Z", scope="read")
        self.assertTrue(res["ok"])
        self.assertIn("since=", captured["url"])

    def test_4xx_structured_error_with_body(self) -> None:
        with _with_token(), _patch_urlopen(
            side_effect=_http_error(404, '{"detail":"no"}')
        ):
            res = sentry.list_issues("anvil", scope="read")
        self.assertFalse(res["ok"])
        self.assertIn("HTTP 404", res["error"])

    def test_5xx_structured_error(self) -> None:
        with _with_token(), _patch_urlopen(side_effect=_http_error(500)):
            res = sentry.list_issues("anvil", scope="read")
        self.assertFalse(res["ok"])
        self.assertIn("HTTP 500", res["error"])

    def test_malformed_json_structured_error(self) -> None:
        with _with_token(), _patch_urlopen(return_value=_resp("not json{")):
            res = sentry.list_issues("anvil", scope="read")
        self.assertFalse(res["ok"])
        self.assertIn("non-JSON", res["error"])

    def test_connection_error_structured_error(self) -> None:
        with _with_token(), _patch_urlopen(
            side_effect=urllib.error.URLError("connection refused")
        ):
            res = sentry.list_issues("anvil", scope="read")
        self.assertFalse(res["ok"])
        self.assertIn("request failed", res["error"])

    def test_missing_token_structured_error_no_http(self) -> None:
        # Token unset → structured error, urlopen never reached.
        with mock.patch.dict("os.environ", {}, clear=True), \
                _patch_urlopen() as m:
            res = sentry.list_issues("anvil", scope="read")
        self.assertFalse(res["ok"])
        self.assertIn("SENTRY_API_TOKEN not set", res["error"])
        m.assert_not_called()


class TestGetIssue(unittest.TestCase):
    def test_happy_path(self) -> None:
        issue = {"id": "42", "title": "t"}
        captured: dict = {}

        def fake_urlopen(req, *a, **kw):
            captured["url"] = req.full_url
            return _resp(json.dumps(issue))

        with _with_token(), _patch_urlopen(side_effect=fake_urlopen):
            res = sentry.get_issue("42", scope="read")
        self.assertTrue(res["ok"])
        self.assertEqual(res["result"]["id"], "42")
        self.assertIn("/issues/42/", captured["url"])

    def test_4xx_structured_error(self) -> None:
        with _with_token(), _patch_urlopen(side_effect=_http_error(403)):
            res = sentry.get_issue("42", scope="read")
        self.assertFalse(res["ok"])
        self.assertIn("HTTP 403", res["error"])


class TestListEvents(unittest.TestCase):
    def test_happy_path(self) -> None:
        events = [{"eventID": "abc"}]
        captured: dict = {}

        def fake_urlopen(req, *a, **kw):
            captured["url"] = req.full_url
            return _resp(json.dumps(events))

        with _with_token(), _patch_urlopen(side_effect=fake_urlopen):
            res = sentry.list_events("42", scope="read")
        self.assertTrue(res["ok"])
        self.assertEqual(res["result"], events)
        self.assertIn("/issues/42/events/", captured["url"])

    def test_connection_error(self) -> None:
        with _with_token(), _patch_urlopen(
            side_effect=urllib.error.URLError("down")
        ):
            res = sentry.list_events("42", scope="read")
        self.assertFalse(res["ok"])
        self.assertIn("request failed", res["error"])


class TestScopeEnforcement(unittest.TestCase):
    def test_list_issues_under_none_refused_without_http(self) -> None:
        with _with_token(), _patch_urlopen() as m:
            res = sentry.list_issues("anvil", scope=None)
        self.assertFalse(res["ok"])
        self.assertIn("sentry scope not declared", res["error"])
        m.assert_not_called()

    def test_get_issue_under_none_refused_without_http(self) -> None:
        with _with_token(), _patch_urlopen() as m:
            res = sentry.get_issue("42", scope=None)
        self.assertFalse(res["ok"])
        self.assertIn("sentry scope not declared", res["error"])
        m.assert_not_called()

    def test_list_events_under_read_succeeds(self) -> None:
        with _with_token(), _patch_urlopen(return_value=_resp("[]")) as m:
            res = sentry.list_events("42", scope="read")
        self.assertTrue(res["ok"])
        m.assert_called_once()


if __name__ == "__main__":
    unittest.main()
