"""
Shared fixtures for the triggerhappy test suite.

IMPORTANT — read this before writing more tests against moderate.py:

src/moderate.py reads ALL of its configuration from os.environ at *import
time* — TOKEN, REPO, ACTOR, EVENT, STRIKES_ENABLED, FORMAT_REGEX, etc. are
plain module-level constants, not values read inside functions. Two
consequences:

  1. `import moderate` raises KeyError immediately unless GITHUB_TOKEN,
     GITHUB_REPOSITORY, GITHUB_ACTOR, GITHUB_EVENT_NAME, DISCUSSION_NODE_ID
     and DISCUSSION_AUTHOR are already set in the environment.
  2. Calling monkeypatch.setenv(...) after the module has been imported
     does NOT change moderate.ACTOR / moderate.STRIKES_ENABLED / etc. for
     the rest of the process — they were fixed at import time.

So every test that needs a specific env-var combination (a different actor,
strikes on vs off, format enforcement on vs off, ...) must go through the
`load_moderate` fixture below, which sets env vars and then reloads the
module. Never `import moderate` directly at the top of a test file.
"""

from __future__ import annotations

import base64
import importlib
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests
import yaml

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Env vars moderate.py reads via os.environ[...] — must always be present.
REQUIRED_ENV = {
    "GITHUB_TOKEN": "test-token",
    "GITHUB_REPOSITORY": "erstarr/triggerhappy",
    "GITHUB_ACTOR": "alice",
    "GITHUB_EVENT_NAME": "discussion",
    "DISCUSSION_NODE_ID": "D_test123",
    "DISCUSSION_AUTHOR": "carol",
}

# Every env var moderate.py ever looks at (required + optional), so
# load_moderate can guarantee a clean slate between tests.
ALL_MODERATE_ENV_KEYS = [
    *REQUIRED_ENV.keys(),
    "DISCUSSION_BODY",
    "COMMENT_BODY",
    "CONFIG_PATH",
    "BANNED_PATH",
    "STRIKES_PATH",
    "STRIKES_TO_BAN",
    "AUTO_CLOSE_REGEX",
    "STRIKE_PER_NONFORMAT",
]


@pytest.fixture
def load_moderate(monkeypatch):
    """
    Factory fixture: load_moderate(**overrides) sets env vars (REQUIRED_ENV
    plus your overrides) and (re)imports `moderate`, returning the module.

    Every call starts from a clean slate — no leakage between tests, and no
    leakage from whatever a previous test in the same file set.
    """

    def _load(**overrides: str):
        for key in ALL_MODERATE_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)

        env = {**REQUIRED_ENV, **overrides}
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))

        if "moderate" in sys.modules:
            module = importlib.reload(sys.modules["moderate"])
        else:
            module = importlib.import_module("moderate")
        return module

    return _load


def make_response(
    json_data: dict[str, Any] | None = None,
    status_code: int = 200,
    raise_exc: Exception | None = None,
) -> MagicMock:
    """A MagicMock standing in for requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.json.return_value = {} if json_data is None else json_data
    if raise_exc is not None:
        resp.raise_for_status.side_effect = raise_exc
    else:
        resp.raise_for_status.return_value = None
    return resp


@pytest.fixture
def patch_requests(monkeypatch):
    """
    patch_requests(module) -> {"get": MagicMock, "post": MagicMock, "put": MagicMock}

    Fine-grained mocking for tests that want to control exactly what one
    call returns and assert on exact call args (used in test_actions.py).
    For multi-call end-to-end flows (used in test_main.py), use
    `fake_github` instead — chaining .side_effect lists by call order is
    brittle and breaks the moment implementation order shifts slightly.
    """

    def _patch(module):
        mocks = {
            "get": MagicMock(name="requests.get"),
            "post": MagicMock(name="requests.post"),
            "put": MagicMock(name="requests.put"),
        }
        monkeypatch.setattr(module.requests, "get", mocks["get"])
        monkeypatch.setattr(module.requests, "post", mocks["post"])
        monkeypatch.setattr(module.requests, "put", mocks["put"])
        return mocks

    return _patch


class FakeGitHub:
    """
    A minimal in-memory stand-in for the slice of the GitHub REST (Contents)
    + GraphQL APIs that moderate.py talks to.

    Keeps real YAML files in memory (so read_yaml_file/write_yaml_file round
    trip for real) and a `closed` flag for the one discussion under test, and
    answers requests.get/post/put the way the real API would — including 404
    for missing files and query-shape dispatch for GraphQL. This lets
    test_main.py assert on *outcomes* (was the discussion closed? what's in
    banned.yml now? what comments got posted?) instead of hard-coding the
    exact sequence/count of HTTP calls, which is what real integration tests
    should look like.
    """

    def __init__(
        self,
        files: dict[str, dict[str, Any]] | None = None,
        discussion_closed: bool = False,
    ):
        self._files: dict[str, tuple[dict[str, Any], str]] = {}
        self._sha_counter = 0
        for path, content in (files or {}).items():
            self._put_file(path, content)

        self.discussion_closed = discussion_closed
        self.comments: list[str] = []
        self.close_reasons: list[str] = []
        self.reopen_calls: int = 0

    def _next_sha(self) -> str:
        self._sha_counter += 1
        return f"sha-{self._sha_counter}"

    def _put_file(self, path: str, content: dict[str, Any]) -> str:
        sha = self._next_sha()
        self._files[path] = (content, sha)
        return sha

    def file(self, path: str) -> dict[str, Any]:
        """Current contents of a fake file (for post-call assertions)."""
        return self._files[path][0] if path in self._files else {}

    # --- Contents API --------------------------------------------------

    def get(self, url: str, headers=None, **kwargs) -> MagicMock:
        path = url.split("/contents/", 1)[1]
        if path not in self._files:
            return make_response(status_code=404)
        content, sha = self._files[path]
        encoded = base64.b64encode(
            yaml.dump(content, default_flow_style=False, allow_unicode=True).encode()
        ).decode()
        return make_response(json_data={"content": encoded, "sha": sha})

    def put(self, url: str, headers=None, json: dict[str, Any] | None = None, **kwargs) -> MagicMock:
        assert json is not None
        path = url.split("/contents/", 1)[1]
        decoded = yaml.safe_load(base64.b64decode(json["content"])) or {}
        new_sha = self._put_file(path, decoded)
        return make_response(json_data={"content": {"sha": new_sha}})

    # --- GraphQL ---------------------------------------------------------

    def post(self, url: str, headers=None, json: dict[str, Any] | None = None, **kwargs) -> MagicMock:
        assert json is not None
        query = json["query"]
        variables = json["variables"]

        if "closeDiscussion" in query:
            self.discussion_closed = True
            self.close_reasons.append(variables["reason"])
            return make_response(
                json_data={"data": {"closeDiscussion": {"discussion": {"id": variables["id"], "closed": True}}}}
            )

        if "reopenDiscussion" in query:
            self.discussion_closed = False
            self.reopen_calls += 1
            return make_response(
                json_data={"data": {"reopenDiscussion": {"discussion": {"id": variables["id"], "closed": False}}}}
            )

        if "addDiscussionComment" in query:
            self.comments.append(variables["body"])
            return make_response(
                json_data={"data": {"addDiscussionComment": {"comment": {"id": f"C_{len(self.comments)}"}}}}
            )

        if "node(id: $id)" in query:
            return make_response(json_data={"data": {"node": {"closed": self.discussion_closed}}})

        raise AssertionError(f"FakeGitHub received an unrecognised GraphQL query:\n{query}")


@pytest.fixture
def fake_github(monkeypatch):
    """
    fake_github(module, files=..., discussion_closed=...) -> FakeGitHub

    Wires a FakeGitHub instance's get/post/put in as module.requests'
    methods and returns it so the test can seed state and assert on
    outcomes afterwards.
    """

    def _wire(module, files: dict[str, dict[str, Any]] | None = None, discussion_closed: bool = False) -> FakeGitHub:
        fake = FakeGitHub(files=files, discussion_closed=discussion_closed)
        monkeypatch.setattr(module.requests, "get", fake.get)
        monkeypatch.setattr(module.requests, "post", fake.post)
        monkeypatch.setattr(module.requests, "put", fake.put)
        return fake

    return _wire