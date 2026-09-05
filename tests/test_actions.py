"""
Tests for the individual building blocks that talk to GitHub:
read_yaml_file, write_yaml_file, graphql, discussion_closed, close_discussion,
reopen_discussion, post_comment, do_ban, do_unban, do_strike, clear_strikes.

These use `patch_requests` (raw MagicMocks) rather than the FakeGitHub
double, because the point here is to pin down exactly what each function
sends and how it reacts to a specific response — one call at a time.
"""

from __future__ import annotations

import base64

import pytest
import requests
import yaml

from conftest import make_response


# ─── read_yaml_file / write_yaml_file ──────────────────────────────────────


class TestReadYamlFile:
    def test_existing_file_is_parsed(self, load_moderate, patch_requests):
        m = load_moderate()
        mocks = patch_requests(m)

        content = {"moderators": ["bob"]}
        encoded = base64.b64encode(yaml.dump(content).encode()).decode()
        mocks["get"].return_value = make_response(
            json_data={"content": encoded, "sha": "abc123"}
        )

        parsed, sha = m.read_yaml_file("some/path.yml")

        assert parsed == content
        assert sha == "abc123"
        mocks["get"].assert_called_once()
        called_url = mocks["get"].call_args.kwargs["url"]
        assert called_url == f"https://api.github.com/repos/{m.REPO}/contents/some/path.yml"
        assert mocks["get"].call_args.kwargs["headers"] == m.HEADERS

    def test_comment_only_yaml_normalises_to_empty_dict(self, load_moderate, patch_requests):
        # yaml.safe_load on a comments-only file returns None; read_yaml_file
        # must normalise that to {} so callers can safely do .get(...) on it.
        m = load_moderate()
        mocks = patch_requests(m)
        encoded = base64.b64encode(b"# just a comment\n").decode()
        mocks["get"].return_value = make_response(json_data={"content": encoded, "sha": "s1"})

        parsed, sha = m.read_yaml_file("empty.yml")
        assert parsed == {}
        assert sha == "s1"

    def test_missing_file_with_first_write_will_create_returns_empty(self, load_moderate, patch_requests):
        m = load_moderate()
        mocks = patch_requests(m)
        mocks["get"].return_value = make_response(status_code=404)

        parsed, sha = m.read_yaml_file("nope.yml", firstWriteWillCreate=True)

        assert parsed == {}
        assert sha is None
        # early-return path: must not try to raise_for_status()/json() a 404
        mocks["get"].return_value.raise_for_status.assert_not_called()

    def test_missing_file_without_first_write_will_create_raises(self, load_moderate, patch_requests):
        m = load_moderate()
        mocks = patch_requests(m)
        mocks["get"].return_value = make_response(
            status_code=404, raise_exc=requests.HTTPError("404")
        )

        with pytest.raises(requests.HTTPError):
            m.read_yaml_file("nope.yml", firstWriteWillCreate=False)

    def test_server_error_propagates(self, load_moderate, patch_requests):
        m = load_moderate()
        mocks = patch_requests(m)
        mocks["get"].return_value = make_response(
            status_code=500, raise_exc=requests.HTTPError("500")
        )

        with pytest.raises(requests.HTTPError):
            m.read_yaml_file("whatever.yml")


class TestWriteYamlFile:
    def test_round_trips_content_and_returns_new_sha(self, load_moderate, patch_requests):
        m = load_moderate()
        mocks = patch_requests(m)
        mocks["put"].return_value = make_response(json_data={"content": {"sha": "new-sha"}})

        content = {"alice": {"reason": "spam"}}
        new_sha = m.write_yaml_file("banned.yml", content, "old-sha", "ban: alice")

        assert new_sha == "new-sha"
        mocks["put"].assert_called_once()
        _, kwargs = mocks["put"].call_args
        assert kwargs["json"]["message"] == "ban: alice"
        assert kwargs["json"]["sha"] == "old-sha"
        round_tripped = yaml.safe_load(base64.b64decode(kwargs["json"]["content"]))
        assert round_tripped == content

    def test_omits_sha_when_creating_a_new_file(self, load_moderate, patch_requests):
        m = load_moderate()
        mocks = patch_requests(m)
        mocks["put"].return_value = make_response(json_data={"content": {"sha": "first-sha"}})

        m.write_yaml_file("banned.yml", {"alice": {}}, None, "ban: alice")

        _, kwargs = mocks["put"].call_args
        assert "sha" not in kwargs["json"]

    def test_error_response_raises(self, load_moderate, patch_requests):
        m = load_moderate()
        mocks = patch_requests(m)
        mocks["put"].return_value = make_response(
            status_code=409, raise_exc=requests.HTTPError("409 conflict")
        )

        with pytest.raises(requests.HTTPError):
            m.write_yaml_file("banned.yml", {}, "stale-sha", "msg")


# ─── graphql / discussion_closed ──────────────────────────────────────────


class TestGraphql:
    def test_returns_data_on_success(self, load_moderate, patch_requests):
        m = load_moderate()
        mocks = patch_requests(m)
        mocks["post"].return_value = make_response(json_data={"data": {"ok": True}})

        result = m.graphql("query {}", {"id": "123"})

        assert result == {"ok": True}
        _, kwargs = mocks["post"].call_args
        assert kwargs["json"]["variables"] == {"id": "123"}

    def test_raises_runtime_error_on_graphql_errors(self, load_moderate, patch_requests):
        m = load_moderate()
        mocks = patch_requests(m)
        mocks["post"].return_value = make_response(
            json_data={"errors": [{"message": "bad node id"}]}
        )

        with pytest.raises(RuntimeError, match="bad node id"):
            m.graphql("query {}", {})

    def test_http_error_propagates(self, load_moderate, patch_requests):
        m = load_moderate()
        mocks = patch_requests(m)
        mocks["post"].return_value = make_response(
            status_code=401, raise_exc=requests.HTTPError("bad token")
        )

        with pytest.raises(requests.HTTPError):
            m.graphql("query {}", {})


class TestDiscussionClosed:
    @pytest.mark.parametrize("closed", [True, False])
    def test_reflects_the_api_value(self, load_moderate, patch_requests, closed):
        m = load_moderate()
        mocks = patch_requests(m)
        mocks["post"].return_value = make_response(json_data={"data": {"node": {"closed": closed}}})

        assert m.discussion_closed("D_1") is closed


# ─── close_discussion / reopen_discussion (idempotency) ───────────────────


class TestCloseDiscussion:
    def test_already_closed_discussion_is_left_alone(self, load_moderate, patch_requests):
        m = load_moderate()
        mocks = patch_requests(m)
        mocks["post"].return_value = make_response(json_data={"data": {"node": {"closed": True}}})

        m.close_discussion("D_1", "RESOLVED")

        # Only the "is it closed" query should have fired - no mutation.
        assert mocks["post"].call_count == 1
        (_, kwargs) = mocks["post"].call_args
        assert "closeDiscussion" not in kwargs["json"]["query"]

    def test_open_discussion_gets_closed(self, load_moderate, patch_requests):
        m = load_moderate()
        mocks = patch_requests(m)
        mocks["post"].side_effect = [
            make_response(json_data={"data": {"node": {"closed": False}}}),
            make_response(json_data={"data": {"closeDiscussion": {"discussion": {"closed": True}}}}),
        ]

        m.close_discussion("D_1", "RESOLVED")

        assert mocks["post"].call_count == 2
        mutation_call = mocks["post"].call_args_list[1]
        assert "closeDiscussion" in mutation_call.kwargs["json"]["query"]
        assert mutation_call.kwargs["json"]["variables"]["reason"] == "RESOLVED"


class TestReopenDiscussion:
    def test_already_open_discussion_is_left_alone(self, load_moderate, patch_requests):
        m = load_moderate()
        mocks = patch_requests(m)
        mocks["post"].return_value = make_response(json_data={"data": {"node": {"closed": False}}})

        m.reopen_discussion("D_1")

        assert mocks["post"].call_count == 1

    def test_closed_discussion_gets_reopened(self, load_moderate, patch_requests):
        m = load_moderate()
        mocks = patch_requests(m)
        mocks["post"].side_effect = [
            make_response(json_data={"data": {"node": {"closed": True}}}),
            make_response(json_data={"data": {"reopenDiscussion": {"discussion": {"closed": False}}}}),
        ]

        m.reopen_discussion("D_1")

        assert mocks["post"].call_count == 2
        mutation_call = mocks["post"].call_args_list[1]
        assert "reopenDiscussion" in mutation_call.kwargs["json"]["query"]


class TestPostComment:
    def test_sends_discussion_id_and_body(self, load_moderate, patch_requests):
        m = load_moderate()
        mocks = patch_requests(m)
        mocks["post"].return_value = make_response(
            json_data={"data": {"addDiscussionComment": {"comment": {"id": "C_1"}}}}
        )

        m.post_comment("D_1", "hello world")

        _, kwargs = mocks["post"].call_args
        assert kwargs["json"]["variables"] == {"discussionId": "D_1", "body": "hello world"}


# ─── do_ban / do_unban / do_strike / clear_strikes ────────────────────────


class TestDoBan:
    def test_records_ban_and_writes_file(self, load_moderate, patch_requests):
        m = load_moderate()
        mocks = patch_requests(m)
        mocks["put"].return_value = make_response(json_data={"content": {"sha": "s2"}})

        banned = {}
        m.do_ban("mallory", "bob", "spamming", banned, None, {}, None)

        assert banned["mallory"]["reason"] == "spamming"
        assert banned["mallory"]["banned_by"] == "bob"
        assert "timestamp" in banned["mallory"]
        mocks["put"].assert_called_once()

    def test_also_clears_existing_strikes_when_enabled(self, load_moderate, patch_requests):
        m = load_moderate(STRIKES_TO_BAN="3")
        mocks = patch_requests(m)
        mocks["put"].return_value = make_response(json_data={"content": {"sha": "s2"}})

        banned = {}
        strikes = {"mallory": 2}
        m.do_ban("mallory", "bob", "spamming", banned, None, strikes, "strikes-sha")

        assert "mallory" not in strikes
        # one PUT for banned.yml, one PUT for strikes.yml
        assert mocks["put"].call_count == 2

    def test_does_not_touch_strikes_file_when_feature_disabled(self, load_moderate, patch_requests):
        m = load_moderate()  # STRIKES_TO_BAN unset
        mocks = patch_requests(m)
        mocks["put"].return_value = make_response(json_data={"content": {"sha": "s2"}})

        banned = {}
        m.do_ban("mallory", "bob", "spamming", banned, None, {}, None)

        # only banned.yml gets written
        assert mocks["put"].call_count == 1


class TestDoUnban:
    def test_unbanning_a_banned_user(self, load_moderate, patch_requests):
        m = load_moderate()
        mocks = patch_requests(m)
        mocks["put"].return_value = make_response(json_data={"content": {"sha": "s2"}})

        banned = {"mallory": {"reason": "spam", "banned_by": "bob", "timestamp": "t"}}
        result = m.do_unban("mallory", banned, "old-sha", {}, None)

        assert result is True
        assert "mallory" not in banned
        mocks["put"].assert_called_once()
        mocks["post"].assert_not_called()

    def test_unbanning_a_user_who_is_not_banned_posts_a_comment(self, load_moderate, patch_requests):
        m = load_moderate()
        mocks = patch_requests(m)
        mocks["post"].return_value = make_response(
            json_data={"data": {"addDiscussionComment": {"comment": {"id": "C_1"}}}}
        )

        result = m.do_unban("nobody", {}, None, {}, None)

        assert result is False
        mocks["put"].assert_not_called()
        mocks["post"].assert_called_once()
        _, kwargs = mocks["post"].call_args
        assert "not in the banned list" in kwargs["json"]["variables"]["body"]


class TestDoStrike:
    def test_noop_when_strikes_disabled(self, load_moderate, patch_requests):
        m = load_moderate()  # STRIKES_TO_BAN unset -> STRIKES_ENABLED False
        mocks = patch_requests(m)

        m.do_strike("carol", {}, None, {}, None)

        mocks["put"].assert_not_called()
        mocks["post"].assert_not_called()

    def test_increments_count_and_posts_warning(self, load_moderate, patch_requests):
        m = load_moderate(STRIKES_TO_BAN="3")
        mocks = patch_requests(m)
        mocks["put"].return_value = make_response(json_data={"content": {"sha": "s2"}})
        mocks["post"].return_value = make_response(
            json_data={"data": {"addDiscussionComment": {"comment": {"id": "C_1"}}}}
        )

        strikes = {"carol": 1}
        m.do_strike("carol", strikes, "sha1", {}, None)

        assert strikes["carol"] == 2
        _, kwargs = mocks["post"].call_args
        assert "2/3" in kwargs["json"]["variables"]["body"]
        assert "@carol" not in kwargs["json"]["variables"]["body"].split("issued to ")[0]

    def test_manual_strike_credits_the_actor(self, load_moderate, patch_requests):
        m = load_moderate(GITHUB_ACTOR="bob", STRIKES_TO_BAN="5")
        mocks = patch_requests(m)
        mocks["put"].return_value = make_response(json_data={"content": {"sha": "s2"}})
        mocks["post"].return_value = make_response(
            json_data={"data": {"addDiscussionComment": {"comment": {"id": "C_1"}}}}
        )

        m.do_strike("carol", {}, None, {}, None, automatedStrike=False)

        _, kwargs = mocks["post"].call_args
        assert "@bob." in kwargs["json"]["variables"]["body"]

    def test_automated_strike_is_attributed_to_system(self, load_moderate, patch_requests):
        m = load_moderate(STRIKES_TO_BAN="5")
        mocks = patch_requests(m)
        mocks["put"].return_value = make_response(json_data={"content": {"sha": "s2"}})
        mocks["post"].return_value = make_response(
            json_data={"data": {"addDiscussionComment": {"comment": {"id": "C_1"}}}}
        )

        m.do_strike("carol", {}, None, {}, None, automatedStrike=True)

        _, kwargs = mocks["post"].call_args
        assert "__system__." in kwargs["json"]["variables"]["body"]

    def test_reaching_threshold_triggers_ban_and_close(self, load_moderate, patch_requests):
        m = load_moderate(STRIKES_TO_BAN="2")
        mocks = patch_requests(m)
        mocks["put"].return_value = make_response(json_data={"content": {"sha": "s2"}})
        mocks["post"].side_effect = [
            make_response(json_data={"data": {"addDiscussionComment": {"comment": {"id": "C_1"}}}}),  # strike warning
            make_response(json_data={"data": {"addDiscussionComment": {"comment": {"id": "C_2"}}}}),  # ban notice
            make_response(json_data={"data": {"node": {"closed": False}}}),  # close_discussion's is-it-closed check
            make_response(json_data={"data": {"closeDiscussion": {"discussion": {"closed": True}}}}),  # the close mutation
        ]

        banned = {}
        strikes = {"carol": 1}
        m.do_strike("carol", strikes, "sha1", banned, None)

        assert strikes.get("carol") is None  # do_ban clears strikes on ban
        assert "carol" in banned
        assert banned["carol"]["banned_by"] == "__system__"
        assert mocks["post"].call_count == 4

    def test_below_threshold_does_not_ban(self, load_moderate, patch_requests):
        m = load_moderate(STRIKES_TO_BAN="5")
        mocks = patch_requests(m)
        mocks["put"].return_value = make_response(json_data={"content": {"sha": "s2"}})
        mocks["post"].return_value = make_response(
            json_data={"data": {"addDiscussionComment": {"comment": {"id": "C_1"}}}}
        )

        banned = {}
        m.do_strike("carol", {"carol": 1}, "sha1", banned, None)

        assert banned == {}
        assert mocks["post"].call_count == 1  # only the warning comment


class TestClearStrikes:
    def test_noop_when_strikes_disabled(self, load_moderate, patch_requests):
        m = load_moderate()
        mocks = patch_requests(m)
        strikes = {"carol": 2}

        m.clear_strikes("carol", strikes, "sha1")

        assert strikes == {"carol": 2}  # untouched
        mocks["put"].assert_not_called()

    def test_noop_when_target_has_no_strikes(self, load_moderate, patch_requests):
        m = load_moderate(STRIKES_TO_BAN="3")
        mocks = patch_requests(m)

        m.clear_strikes("carol", {}, None)

        mocks["put"].assert_not_called()

    def test_removes_target_and_writes_file(self, load_moderate, patch_requests):
        m = load_moderate(STRIKES_TO_BAN="3")
        mocks = patch_requests(m)
        mocks["put"].return_value = make_response(json_data={"content": {"sha": "s2"}})

        strikes = {"carol": 2, "dave": 1}
        m.clear_strikes("carol", strikes, "sha1")

        assert strikes == {"dave": 1}
        mocks["put"].assert_called_once()