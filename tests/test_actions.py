# pyright: basic

from unittest.mock import patch

import moderate


class TestClearStrikes:
    def test_strikes_disabled_is_noop(self):
        strike_counts = {"alice": 2}
        with patch("moderate.STRIKES_ENABLED", False), \
             patch("moderate.write_file") as mock_write:
            moderate.clear_strikes("alice", strike_counts, "sha123")
            mock_write.assert_not_called()
        assert "alice" in strike_counts  # dict unchanged

    def test_target_absent_is_noop(self):
        strike_counts = {"bob": 1}
        with patch("moderate.STRIKES_ENABLED", True), \
             patch("moderate.write_file") as mock_write:
            moderate.clear_strikes("alice", strike_counts, "sha123")
            mock_write.assert_not_called()

    def test_clears_and_writes(self):
        strike_counts = {"alice": 2}
        with patch("moderate.STRIKES_ENABLED", True), \
             patch("moderate.write_file") as mock_write:
            moderate.clear_strikes("alice", strike_counts, "sha123")
            assert "alice" not in strike_counts
            mock_write.assert_called_once_with(
                moderate.STRIKES_PATH,
                strike_counts,
                "sha123",
                "clear strikes: alice",
            )


class TestDoBan:
    def test_writes_entry_with_correct_fields(self):
        banned_users: dict = {}
        with patch("moderate.write_file") as mock_write, \
             patch("moderate.clear_strikes"):
            moderate.do_ban("alice", "bob", "spamming", banned_users, "sha1", {}, None)
            assert "alice" in banned_users
            assert banned_users["alice"]["reason"]    == "spamming"
            assert banned_users["alice"]["banned_by"] == "bob"
            assert "timestamp" in banned_users["alice"]
            mock_write.assert_called_once_with(
                moderate.BANNED_PATH, banned_users, "sha1", "ban: alice"
            )

    def test_calls_clear_strikes(self):
        with patch("moderate.write_file"), \
             patch("moderate.clear_strikes") as mock_clear:
            strike_counts: dict = {"alice": 1}
            moderate.do_ban("alice", "bob", "reason", {}, None, strike_counts, "sha-s")
            mock_clear.assert_called_once_with("alice", strike_counts, "sha-s")

    def test_system_auto_ban_sets_banned_by(self):
        banned_users: dict = {}
        with patch("moderate.write_file"), \
             patch("moderate.clear_strikes"):
            moderate.do_ban("alice", "__system__", "Auto-ban: reached 3 strikes", banned_users, None, {}, None)
            assert banned_users["alice"]["banned_by"] == "__system__"


class TestDoUnban:
    def test_target_not_banned_posts_message(self):
        with patch("moderate.post_comment") as mock_comment:
            moderate.do_unban("alice", {}, None, {}, None)
            mock_comment.assert_called_once()
            assert "not in the banned list" in mock_comment.call_args[0][1]

    def test_removes_user_and_writes(self):
        banned_users = {
            "alice": {"reason": "spam", "banned_by": "bob", "timestamp": "2026-01-01T00:00:00+00:00"}
        }
        with patch("moderate.write_file") as mock_write, \
             patch("moderate.clear_strikes"):
            moderate.do_unban("alice", banned_users, "sha1", {}, None)
            assert "alice" not in banned_users
            mock_write.assert_called_once_with(
                moderate.BANNED_PATH, banned_users, "sha1", "unban: alice"
            )

    def test_calls_clear_strikes_after_unban(self):
        banned_users = {
            "alice": {"reason": "spam", "banned_by": "bob", "timestamp": "2026-01-01T00:00:00+00:00"}
        }
        strike_counts = {"alice": 1}
        with patch("moderate.write_file"), \
             patch("moderate.clear_strikes") as mock_clear:
            moderate.do_unban("alice", banned_users, "sha1", strike_counts, "sha-s")
            mock_clear.assert_called_once_with("alice", strike_counts, "sha-s")


class TestDoStrike:
    def test_strikes_disabled_is_noop(self):
        with patch("moderate.STRIKES_ENABLED", False), \
             patch("moderate.write_file") as mock_write:
            moderate.do_strike("alice", {}, None, {}, None)
            mock_write.assert_not_called()

    def test_first_strike_initialises_to_one(self):
        strike_counts: dict = {}
        with patch("moderate.STRIKES_ENABLED", True), \
             patch("moderate.STRIKE_TO_BAN", 3), \
             patch("moderate.write_file"), \
             patch("moderate.post_comment"), \
             patch("moderate.do_ban") as mock_ban:
            moderate.do_strike("alice", strike_counts, "sha1", {}, None)
            assert strike_counts["alice"] == 1
            mock_ban.assert_not_called()

    def test_existing_count_increments(self):
        strike_counts = {"alice": 1}
        with patch("moderate.STRIKES_ENABLED", True), \
             patch("moderate.STRIKE_TO_BAN", 3), \
             patch("moderate.write_file"), \
             patch("moderate.post_comment"), \
             patch("moderate.do_ban") as mock_ban:
            moderate.do_strike("alice", strike_counts, "sha1", {}, None)
            assert strike_counts["alice"] == 2
            mock_ban.assert_not_called()

    def test_triggers_ban_at_threshold(self):
        strike_counts = {"alice": 2}
        banned_users: dict = {}
        with patch("moderate.STRIKES_ENABLED", True), \
             patch("moderate.STRIKE_TO_BAN", 3), \
                patch("moderate.write_file", return_value="new-sha"), \
             patch("moderate.post_comment"), \
             patch("moderate.do_ban") as mock_ban, \
             patch("moderate.close_discussion") as mock_close:
            moderate.do_strike("alice", strike_counts, "sha-s", banned_users, "sha-b")
            assert strike_counts["alice"] == 3
            mock_ban.assert_called_once_with(
                "alice", "__system__",
                "Auto-ban: reached 3 strikes",
                banned_users, "sha-b",
                strike_counts, "new-sha",
            )
            mock_close.assert_called_once_with(moderate.DISCUSSION_NODE_ID, "RESOLVED")

    def test_posts_strike_comment(self):
        with patch("moderate.STRIKES_ENABLED", True), \
             patch("moderate.STRIKE_TO_BAN", 3), \
             patch("moderate.write_file"), \
             patch("moderate.post_comment") as mock_comment, \
             patch("moderate.do_ban"):
            moderate.do_strike("alice", {}, None, {}, None)
            mock_comment.assert_called_once()
            assert "Strike" in mock_comment.call_args[0][1]
            assert "alice" in mock_comment.call_args[0][1]