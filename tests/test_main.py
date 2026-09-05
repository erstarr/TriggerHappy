"""
End-to-end tests for main(), covering the discussion-opened flow and every
/command in the discussion_comment flow, including permission checks.

These use `fake_github` (an in-memory GitHub double) rather than exact
side_effect call sequences: main() makes several HTTP calls per command and
hard-coding call order/count here would make the suite brittle against
harmless implementation reshuffling. Instead we seed CONFIG_PATH /
BANNED_PATH / STRIKES_PATH contents up front and assert on outcomes: was
the discussion closed, what's in banned.yml now, what got commented.
"""

import pytest

CONFIG_PATH = ".github/discussion_moderator/config.yml"
BANNED_PATH = ".github/discussion_moderator/banned.yml"
STRIKES_PATH = ".github/discussion_moderator/strikes.yml"

DEFAULT_CONFIG = {
    "closers": ["allison"],
    "strikers": ["alice"],
    "moderators": ["bob"],
}


# ─── discussion opened ─────────────────────────────────────────────────────


class TestDiscussionOpenedEvent:
    def test_banned_author_is_auto_closed(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion",
            GITHUB_ACTOR="mallory",
        )
        gh = fake_github(
            m,
            files={
                CONFIG_PATH: DEFAULT_CONFIG,
                BANNED_PATH: {"mallory": {"reason": "spam", "banned_by": "bob", "timestamp": "t"}},
            },
        )

        m.main()

        assert gh.discussion_closed is True
        assert gh.close_reasons == ["RESOLVED"]
        assert len(gh.comments) == 1
        assert "not permitted to open discussions" in gh.comments[0]
        assert "spam" in gh.comments[0]

    def test_unbanned_author_with_no_format_enforcement_is_untouched(self, load_moderate, fake_github):
        m = load_moderate(GITHUB_EVENT_NAME="discussion", GITHUB_ACTOR="dave")
        gh = fake_github(m, files={CONFIG_PATH: DEFAULT_CONFIG, BANNED_PATH: {}})

        m.main()

        assert gh.discussion_closed is False
        assert gh.comments == []

    def test_format_violation_closes_and_comments(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion",
            GITHUB_ACTOR="dave",
            AUTO_CLOSE_REGEX=r"Title: .+",
            DISCUSSION_BODY="this does not match the required format",
        )
        gh = fake_github(m, files={CONFIG_PATH: DEFAULT_CONFIG, BANNED_PATH: {}})

        m.main()

        assert gh.discussion_closed is True
        assert gh.close_reasons == ["RESOLVED"]
        assert any("format is NOT optional" in c for c in gh.comments)

    def test_format_violation_with_strikes_enabled_also_strikes_author(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion",
            GITHUB_ACTOR="dave",
            AUTO_CLOSE_REGEX=r"Title: .+",
            STRIKE_PER_NONFORMAT="true",
            STRIKES_TO_BAN="3",
            DISCUSSION_BODY="nope",
        )
        gh = fake_github(
            m,
            files={CONFIG_PATH: DEFAULT_CONFIG, BANNED_PATH: {}, STRIKES_PATH: {}},
        )

        m.main()

        assert gh.file(STRIKES_PATH).get("dave") == 1
        assert any("Strike **1/3**" in c for c in gh.comments)

    def test_format_compliant_body_is_left_alone(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion",
            GITHUB_ACTOR="dave",
            AUTO_CLOSE_REGEX=r"Title: .+",
            DISCUSSION_BODY="Title: a perfectly formatted discussion",
        )
        gh = fake_github(m, files={CONFIG_PATH: DEFAULT_CONFIG, BANNED_PATH: {}})

        m.main()

        assert gh.discussion_closed is False
        assert gh.comments == []


# ─── /close and /open ──────────────────────────────────────────────────────


class TestCloseCommand:
    def test_authorised_closer_can_close_with_reason(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion_comment",
            GITHUB_ACTOR="allison",
            COMMENT_BODY="/close resolved all set",
        )
        gh = fake_github(m, files={CONFIG_PATH: DEFAULT_CONFIG, BANNED_PATH: {}})

        m.main()

        assert gh.discussion_closed is True
        assert gh.close_reasons == ["RESOLVED"]
        assert any("all set" in c for c in gh.comments)

    def test_moderator_can_also_close(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion_comment",
            GITHUB_ACTOR="bob",
            COMMENT_BODY="/close duplicate",
        )
        gh = fake_github(m, files={CONFIG_PATH: DEFAULT_CONFIG, BANNED_PATH: {}})

        m.main()

        assert gh.discussion_closed is True
        assert gh.close_reasons == ["DUPLICATE"]

    def test_unauthorised_user_cannot_close(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion_comment",
            GITHUB_ACTOR="dave",
            COMMENT_BODY="/close resolved",
        )
        gh = fake_github(m, files={CONFIG_PATH: DEFAULT_CONFIG, BANNED_PATH: {}})

        m.main()

        assert gh.discussion_closed is False
        assert gh.comments == []

    def test_invalid_reason_gets_usage_message_and_no_close(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion_comment",
            GITHUB_ACTOR="allison",
            COMMENT_BODY="/close nonsense",
        )
        gh = fake_github(m, files={CONFIG_PATH: DEFAULT_CONFIG, BANNED_PATH: {}})

        m.main()

        assert gh.discussion_closed is False
        assert any("Usage:" in c for c in gh.comments)


class TestOpenCommand:
    def test_authorised_closer_can_reopen(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion_comment",
            GITHUB_ACTOR="allison",
            COMMENT_BODY="/open",
        )
        gh = fake_github(
            m, files={CONFIG_PATH: DEFAULT_CONFIG, BANNED_PATH: {}}, discussion_closed=True
        )

        m.main()

        assert gh.discussion_closed is False
        assert gh.reopen_calls == 1

    def test_unauthorised_user_cannot_reopen(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion_comment",
            GITHUB_ACTOR="dave",
            COMMENT_BODY="/open",
        )
        gh = fake_github(
            m, files={CONFIG_PATH: DEFAULT_CONFIG, BANNED_PATH: {}}, discussion_closed=True
        )

        m.main()

        assert gh.discussion_closed is True
        assert gh.reopen_calls == 0


# ─── /strike and /strike-target ───────────────────────────────────────────


class TestStrikeCommand:
    def test_striker_can_strike_discussion_author(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion_comment",
            GITHUB_ACTOR="alice",  # striker
            DISCUSSION_AUTHOR="carol",
            COMMENT_BODY="/strike",
            STRIKES_TO_BAN="3",
        )
        gh = fake_github(
            m, files={CONFIG_PATH: DEFAULT_CONFIG, BANNED_PATH: {}, STRIKES_PATH: {}}
        )

        m.main()

        assert gh.file(STRIKES_PATH).get("carol") == 1

    def test_strike_ignored_when_feature_disabled(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion_comment",
            GITHUB_ACTOR="alice",
            DISCUSSION_AUTHOR="carol",
            COMMENT_BODY="/strike",
            # STRIKES_TO_BAN intentionally unset
        )
        gh = fake_github(m, files={CONFIG_PATH: DEFAULT_CONFIG, BANNED_PATH: {}})

        m.main()

        assert gh.comments == []

    def test_unauthorised_user_cannot_strike(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion_comment",
            GITHUB_ACTOR="dave",
            DISCUSSION_AUTHOR="carol",
            COMMENT_BODY="/strike",
            STRIKES_TO_BAN="3",
        )
        gh = fake_github(
            m, files={CONFIG_PATH: DEFAULT_CONFIG, BANNED_PATH: {}, STRIKES_PATH: {}}
        )

        m.main()

        assert gh.file(STRIKES_PATH) == {}

    def test_reaching_threshold_auto_bans_and_closes(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion_comment",
            GITHUB_ACTOR="alice",
            DISCUSSION_AUTHOR="carol",
            COMMENT_BODY="/strike",
            STRIKES_TO_BAN="2",
        )
        gh = fake_github(
            m,
            files={CONFIG_PATH: DEFAULT_CONFIG, BANNED_PATH: {}, STRIKES_PATH: {"carol": 1}},
        )

        m.main()

        assert "carol" not in gh.file(STRIKES_PATH)
        assert gh.file(BANNED_PATH)["carol"]["banned_by"] == "__system__"
        assert gh.discussion_closed is True

    def test_already_banned_target_is_not_struck_again(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion_comment",
            GITHUB_ACTOR="alice",
            DISCUSSION_AUTHOR="carol",
            COMMENT_BODY="/strike",
            STRIKES_TO_BAN="3",
        )
        gh = fake_github(
            m,
            files={
                CONFIG_PATH: DEFAULT_CONFIG,
                BANNED_PATH: {"carol": {"reason": "x", "banned_by": "bob", "timestamp": "t"}},
                STRIKES_PATH: {},
            },
        )

        m.main()

        assert gh.file(STRIKES_PATH) == {}
        assert any("already banned" in c for c in gh.comments)


class TestStrikeTargetCommand:
    def test_moderator_can_strike_a_named_user(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion_comment",
            GITHUB_ACTOR="bob",
            COMMENT_BODY="/strike-target @mallory",
            STRIKES_TO_BAN="3",
        )
        gh = fake_github(
            m, files={CONFIG_PATH: DEFAULT_CONFIG, BANNED_PATH: {}, STRIKES_PATH: {}}
        )

        m.main()

        assert gh.file(STRIKES_PATH).get("mallory") == 1

    def test_missing_username_gets_usage_message(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion_comment",
            GITHUB_ACTOR="bob",
            COMMENT_BODY="/strike-target",
            STRIKES_TO_BAN="3",
        )
        gh = fake_github(
            m, files={CONFIG_PATH: DEFAULT_CONFIG, BANNED_PATH: {}, STRIKES_PATH: {}}
        )

        m.main()

        assert gh.file(STRIKES_PATH) == {}
        assert any("Usage:" in c for c in gh.comments)


# ─── /ban and /unban ───────────────────────────────────────────────────────


class TestBanCommand:
    def test_moderator_can_ban_discussion_author(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion_comment",
            GITHUB_ACTOR="bob",
            DISCUSSION_AUTHOR="mallory",
            COMMENT_BODY="/ban repeated spam",
        )
        gh = fake_github(m, files={CONFIG_PATH: DEFAULT_CONFIG, BANNED_PATH: {}})

        m.main()

        assert gh.file(BANNED_PATH)["mallory"]["reason"] == "repeated spam"
        assert gh.file(BANNED_PATH)["mallory"]["banned_by"] == "bob"
        assert gh.discussion_closed is True

    def test_non_moderator_cannot_ban(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion_comment",
            GITHUB_ACTOR="allison",  # closer, not moderator
            DISCUSSION_AUTHOR="mallory",
            COMMENT_BODY="/ban repeated spam",
        )
        gh = fake_github(m, files={CONFIG_PATH: DEFAULT_CONFIG, BANNED_PATH: {}})

        m.main()

        assert gh.file(BANNED_PATH) == {}

    def test_missing_reason_gets_usage_message(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion_comment",
            GITHUB_ACTOR="bob",
            DISCUSSION_AUTHOR="mallory",
            COMMENT_BODY="/ban",
        )
        gh = fake_github(m, files={CONFIG_PATH: DEFAULT_CONFIG, BANNED_PATH: {}})

        m.main()

        assert gh.file(BANNED_PATH) == {}
        assert any("Usage:" in c for c in gh.comments)

    def test_already_banned_author_is_not_banned_again(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion_comment",
            GITHUB_ACTOR="bob",
            DISCUSSION_AUTHOR="mallory",
            COMMENT_BODY="/ban again",
        )
        gh = fake_github(
            m,
            files={
                CONFIG_PATH: DEFAULT_CONFIG,
                BANNED_PATH: {"mallory": {"reason": "first ban", "banned_by": "bob", "timestamp": "t"}},
            },
        )

        m.main()

        assert gh.file(BANNED_PATH)["mallory"]["reason"] == "first ban"  # untouched
        assert any("already banned" in c for c in gh.comments)


class TestUnbanCommand:
    def test_moderator_can_unban(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion_comment",
            GITHUB_ACTOR="bob",
            COMMENT_BODY="/unban @mallory",
        )
        gh = fake_github(
            m,
            files={
                CONFIG_PATH: DEFAULT_CONFIG,
                BANNED_PATH: {"mallory": {"reason": "x", "banned_by": "bob", "timestamp": "t"}},
            },
        )

        m.main()

        assert gh.file(BANNED_PATH) == {}
        assert any("has been unbanned" in c for c in gh.comments)

    def test_non_moderator_cannot_unban(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion_comment",
            GITHUB_ACTOR="alice",  # striker, not moderator
            COMMENT_BODY="/unban @mallory",
        )
        gh = fake_github(
            m,
            files={
                CONFIG_PATH: DEFAULT_CONFIG,
                BANNED_PATH: {"mallory": {"reason": "x", "banned_by": "bob", "timestamp": "t"}},
            },
        )

        m.main()

        assert "mallory" in gh.file(BANNED_PATH)  # untouched

    def test_unbanning_a_user_who_isnt_banned_is_reported(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion_comment",
            GITHUB_ACTOR="bob",
            COMMENT_BODY="/unban @nobody",
        )
        gh = fake_github(m, files={CONFIG_PATH: DEFAULT_CONFIG, BANNED_PATH: {}})

        m.main()

        assert any("not in the banned list" in c for c in gh.comments)
        assert not any("has been unbanned" in c for c in gh.comments)


# ─── misc dispatch behaviour ────────────────────────────────────────────


class TestUnrecognisedInput:
    def test_comment_with_no_slash_command_is_a_noop(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion_comment",
            GITHUB_ACTOR="bob",
            COMMENT_BODY="just chatting, no commands here",
        )
        gh = fake_github(m, files={CONFIG_PATH: DEFAULT_CONFIG, BANNED_PATH: {}})

        m.main()

        assert gh.comments == []
        assert gh.discussion_closed is False

    def test_command_can_appear_mid_sentence(self, load_moderate, fake_github):
        # The comment-scanning loop looks for '/' anywhere in the body, not
        # just at the start of a line.
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion_comment",
            GITHUB_ACTOR="allison",
            COMMENT_BODY="Reviewed this, going to /close resolved now",
        )
        gh = fake_github(m, files={CONFIG_PATH: DEFAULT_CONFIG, BANNED_PATH: {}})

        m.main()

        assert gh.discussion_closed is True

    def test_first_matching_command_wins_when_body_names_two(self, load_moderate, fake_github):
        m = load_moderate(
            GITHUB_EVENT_NAME="discussion_comment",
            GITHUB_ACTOR="bob",
            DISCUSSION_AUTHOR="mallory",
            COMMENT_BODY="/open then /ban spam",
        )
        gh = fake_github(
            m, files={CONFIG_PATH: DEFAULT_CONFIG, BANNED_PATH: {}}, discussion_closed=True
        )

        m.main()

        # /open is found first and main() returns right after handling it
        assert gh.reopen_calls == 1
        assert gh.file(BANNED_PATH) == {}