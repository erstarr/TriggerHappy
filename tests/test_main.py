# pyright: basic

from unittest.mock import patch

import moderate


# ─── Fixtures ─────────────────────────────────────────────────────────────────

SAMPLE_CONFIG = {
    "closers":    ["closer-user"],
    "strikers":   ["striker-user"],
    "moderators": ["mod-user"],
}

SAMPLE_BANNED_USERS = {
    "banned-user": {
        "reason":    "was bad",
        "banned_by": "mod-user",
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
}

SAMPLE_STRIKES = {"some-user": 1}


def make_read_file(config=None, banned=None, strikes=None):
    """
    Returns a side_effect for read_file.
    Routes by filename fragment since the paths are predictable.
    """
    _config  = config  if config  is not None else SAMPLE_CONFIG
    _banned  = banned  if banned  is not None else {}
    _strikes = strikes if strikes is not None else {}

    def _inner(path: str):
        if "config"  in path: return _config,  "sha-config"
        if "banned"  in path: return _banned,  "sha-banned"
        if "strikes" in path: return _strikes, "sha-strikes"
        return {}, None

    return _inner


# ─── Discussion opened ─────────────────────────────────────────────────────────

class TestDiscussionOpenedEvent:
    def test_banned_user_discussion_is_closed(self):
        with patch("moderate.EVENT", "discussion"), \
             patch("moderate.ACTOR", "banned-user"), \
             patch("moderate.read_file", side_effect=make_read_file(banned=SAMPLE_BANNED_USERS)), \
             patch("moderate.close_discussion") as mock_close, \
             patch("moderate.post_comment") as mock_comment:
            moderate.main()
            mock_close.assert_called_once_with(moderate.DISCUSSION_NODE_ID, "RESOLVED")
            mock_comment.assert_called_once()
            assert "banned-user" in mock_comment.call_args[0][1]

    def test_non_banned_user_passes_through(self):
        with patch("moderate.EVENT", "discussion"), \
             patch("moderate.ACTOR", "innocent-user"), \
             patch("moderate.read_file", side_effect=make_read_file()), \
             patch("moderate.close_discussion") as mock_close:
            moderate.main()
            mock_close.assert_not_called()


# ─── /close ───────────────────────────────────────────────────────────────────

class TestCloseCommand:
    def test_unauthorised_user_silently_ignored(self):
        with patch("moderate.EVENT", "discussion_comment"), \
             patch("moderate.ACTOR", "random-user"), \
             patch("moderate.COMMENT_BODY", "/close resolved"), \
             patch("moderate.read_file", side_effect=make_read_file()), \
             patch("moderate.close_discussion") as mock_close, \
             patch("moderate.post_comment") as mock_comment:
            moderate.main()
            mock_close.assert_not_called()
            mock_comment.assert_not_called()

    def test_closer_can_close(self):
        with patch("moderate.EVENT", "discussion_comment"), \
             patch("moderate.ACTOR", "closer-user"), \
             patch("moderate.COMMENT_BODY", "/close resolved some reason"), \
             patch("moderate.read_file", side_effect=make_read_file()), \
             patch("moderate.close_discussion") as mock_close, \
             patch("moderate.post_comment"):
            moderate.main()
            mock_close.assert_called_once_with(moderate.DISCUSSION_NODE_ID, "RESOLVED")

    def test_moderator_can_close(self):
        with patch("moderate.EVENT", "discussion_comment"), \
             patch("moderate.ACTOR", "mod-user"), \
             patch("moderate.COMMENT_BODY", "/close outdated"), \
             patch("moderate.read_file", side_effect=make_read_file()), \
             patch("moderate.close_discussion") as mock_close, \
             patch("moderate.post_comment"):
            moderate.main()
            mock_close.assert_called_once_with(moderate.DISCUSSION_NODE_ID, "OUTDATED")

    def test_missing_reason_posts_usage(self):
        with patch("moderate.EVENT", "discussion_comment"), \
             patch("moderate.ACTOR", "closer-user"), \
             patch("moderate.COMMENT_BODY", "/close"), \
             patch("moderate.read_file", side_effect=make_read_file()), \
             patch("moderate.close_discussion") as mock_close, \
             patch("moderate.post_comment") as mock_comment:
            moderate.main()
            mock_close.assert_not_called()
            assert "Usage" in mock_comment.call_args[0][1]

    def test_unknown_reason_posts_error(self):
        with patch("moderate.EVENT", "discussion_comment"), \
             patch("moderate.ACTOR", "closer-user"), \
             patch("moderate.COMMENT_BODY", "/close weeewooo"), \
             patch("moderate.read_file", side_effect=make_read_file()), \
             patch("moderate.close_discussion") as mock_close, \
             patch("moderate.post_comment") as mock_comment:
            moderate.main()
            mock_close.assert_not_called()
            assert "weeewooo" in mock_comment.call_args[0][1]


# ─── /strike ──────────────────────────────────────────────────────────────────

class TestStrikeCommand:
    def test_strikes_disabled_is_noop(self):
        with patch("moderate.EVENT", "discussion_comment"), \
             patch("moderate.ACTOR", "striker-user"), \
             patch("moderate.COMMENT_BODY", "/strike"), \
             patch("moderate.STRIKES_ENABLED", False), \
             patch("moderate.read_file", side_effect=make_read_file()), \
             patch("moderate.do_strike") as mock_strike:
            moderate.main()
            mock_strike.assert_not_called()

    def test_unauthorised_user_ignored(self):
        with patch("moderate.EVENT", "discussion_comment"), \
             patch("moderate.ACTOR", "random-user"), \
             patch("moderate.COMMENT_BODY", "/strike @alice"), \
             patch("moderate.STRIKES_ENABLED", True), \
             patch("moderate.read_file", side_effect=make_read_file()), \
             patch("moderate.do_strike") as mock_strike:
            moderate.main()
            mock_strike.assert_not_called()

    def test_already_banned_target_ignored(self):
        with patch("moderate.EVENT", "discussion_comment"), \
             patch("moderate.ACTOR", "striker-user"), \
             patch("moderate.COMMENT_BODY", "/strike @banned-user"), \
             patch("moderate.STRIKES_ENABLED", True), \
             patch("moderate.read_file", side_effect=make_read_file(banned=SAMPLE_BANNED_USERS)), \
             patch("moderate.do_strike") as mock_strike:
            moderate.main()
            mock_strike.assert_not_called()

    def test_defaults_to_discussion_author_when_no_target(self):
        with patch("moderate.EVENT", "discussion_comment"), \
             patch("moderate.ACTOR", "striker-user"), \
             patch("moderate.COMMENT_BODY", "/strike"), \
             patch("moderate.DISCUSSION_AUTHOR", "discussion-author"), \
             patch("moderate.STRIKES_ENABLED", True), \
             patch("moderate.read_file", side_effect=make_read_file(strikes=SAMPLE_STRIKES)), \
             patch("moderate.do_strike") as mock_strike:
            moderate.main()
            mock_strike.assert_called_once()
            assert mock_strike.call_args[0][0] == "discussion-author"

    def test_explicit_target_is_used(self):
        with patch("moderate.EVENT", "discussion_comment"), \
             patch("moderate.ACTOR", "striker-user"), \
             patch("moderate.COMMENT_BODY", "/strike @alice"), \
             patch("moderate.STRIKES_ENABLED", True), \
             patch("moderate.read_file", side_effect=make_read_file(strikes=SAMPLE_STRIKES)), \
             patch("moderate.do_strike") as mock_strike:
            moderate.main()
            mock_strike.assert_called_once()
            assert mock_strike.call_args[0][0] == "alice"


# ─── /ban ─────────────────────────────────────────────────────────────────────

class TestBanCommand:
    def test_non_moderator_ignored(self):
        with patch("moderate.EVENT", "discussion_comment"), \
             patch("moderate.ACTOR", "striker-user"), \
             patch("moderate.COMMENT_BODY", "/ban @alice spamming"), \
             patch("moderate.read_file", side_effect=make_read_file()), \
             patch("moderate.do_ban") as mock_ban:
            moderate.main()
            mock_ban.assert_not_called()

    def test_missing_target_posts_usage(self):
        with patch("moderate.EVENT", "discussion_comment"), \
             patch("moderate.ACTOR", "mod-user"), \
             patch("moderate.COMMENT_BODY", "/ban"), \
             patch("moderate.read_file", side_effect=make_read_file()), \
             patch("moderate.do_ban") as mock_ban, \
             patch("moderate.post_comment") as mock_comment:
            moderate.main()
            mock_ban.assert_not_called()
            assert "Usage" in mock_comment.call_args[0][1]

    def test_missing_reason_posts_usage(self):
        with patch("moderate.EVENT", "discussion_comment"), \
             patch("moderate.ACTOR", "mod-user"), \
             patch("moderate.COMMENT_BODY", "/ban @alice"), \
             patch("moderate.read_file", side_effect=make_read_file()), \
             patch("moderate.do_ban") as mock_ban, \
             patch("moderate.post_comment") as mock_comment:
            moderate.main()
            mock_ban.assert_not_called()
            assert "Usage" in mock_comment.call_args[0][1]

    def test_valid_ban_calls_do_ban_and_closes(self):
        with patch("moderate.EVENT", "discussion_comment"), \
             patch("moderate.ACTOR", "mod-user"), \
             patch("moderate.COMMENT_BODY", "/ban @alice spamming the forums"), \
             patch("moderate.read_file", side_effect=make_read_file()), \
             patch("moderate.do_ban") as mock_ban, \
             patch("moderate.post_comment"), \
             patch("moderate.close_discussion") as mock_close:
            moderate.main()
            mock_ban.assert_called_once()
            assert mock_ban.call_args[0][0] == "alice"
            assert mock_ban.call_args[0][2] == "spamming the forums"
            mock_close.assert_called_once_with(moderate.DISCUSSION_NODE_ID, "RESOLVED")


# ─── /unban ───────────────────────────────────────────────────────────────────

class TestUnbanCommand:
    def test_non_moderator_ignored(self):
        with patch("moderate.EVENT", "discussion_comment"), \
             patch("moderate.ACTOR", "closer-user"), \
             patch("moderate.COMMENT_BODY", "/unban @banned-user"), \
             patch("moderate.read_file", side_effect=make_read_file(banned=SAMPLE_BANNED_USERS)), \
             patch("moderate.do_unban") as mock_unban:
            moderate.main()
            mock_unban.assert_not_called()

    def test_missing_target_posts_usage(self):
        with patch("moderate.EVENT", "discussion_comment"), \
             patch("moderate.ACTOR", "mod-user"), \
             patch("moderate.COMMENT_BODY", "/unban"), \
             patch("moderate.read_file", side_effect=make_read_file()), \
             patch("moderate.do_unban") as mock_unban, \
             patch("moderate.post_comment") as mock_comment:
            moderate.main()
            mock_unban.assert_not_called()
            assert "Usage" in mock_comment.call_args[0][1]

    def test_valid_unban_calls_do_unban(self):
        with patch("moderate.EVENT", "discussion_comment"), \
             patch("moderate.ACTOR", "mod-user"), \
             patch("moderate.COMMENT_BODY", "/unban @banned-user"), \
             patch("moderate.read_file", side_effect=make_read_file(banned=SAMPLE_BANNED_USERS)), \
             patch("moderate.do_unban") as mock_unban, \
             patch("moderate.post_comment"):
            moderate.main()
            mock_unban.assert_called_once()
            assert mock_unban.call_args[0][0] == "banned-user"