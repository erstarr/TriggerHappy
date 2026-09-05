"""
Tests for the pure-ish parsing helpers: extract_username, parse_close,
complies_format, get_whole_line_after_command_from_comment_body.

These still need `load_moderate` (rather than a bare `import moderate`)
because the module can't even be imported without the required env vars —
see the note at the top of conftest.py.
"""

import pytest


class TestExtractUsername:
    def test_at_prefixed_username(self, load_moderate):
        m = load_moderate()
        assert m.extract_username("unban @carol") == "carol"

    def test_plain_username_no_at(self, load_moderate):
        m = load_moderate()
        assert m.extract_username("unban carol") == "carol"

    def test_missing_username_returns_none(self, load_moderate):
        m = load_moderate()
        assert m.extract_username("unban") is None

    def test_empty_body_returns_none(self, load_moderate):
        m = load_moderate()
        assert m.extract_username("") is None

    def test_trailing_tokens_are_ignored_past_second(self, load_moderate):
        m = load_moderate()
        # maxsplit=2 -> ["strike-target", "@carol", "please and thank you"]
        assert m.extract_username("strike-target @carol please and thank you") == "carol"

    def test_collapses_repeated_spaces(self, load_moderate):
        m = load_moderate()
        assert m.extract_username("unban    @carol") == "carol"

    def test_lstrip_removes_all_leading_at_signs(self, load_moderate):
        # lstrip("@") strips every leading '@', not just one -- documenting
        # actual str.lstrip semantics here since it's an easy thing to
        # misremember as "strip one prefix character".
        m = load_moderate()
        assert m.extract_username("unban @@carol") == "carol"

    def test_does_not_strip_at_signs_inside_the_name(self, load_moderate):
        m = load_moderate()
        assert m.extract_username("unban weird@name") == "weird@name"


class TestParseClose:
    @pytest.mark.parametrize(
        "reason_word,enum_value",
        [
            ("resolved", "RESOLVED"),
            ("RESOLVED", "RESOLVED"),
            ("Resolved", "RESOLVED"),
            ("outdated", "OUTDATED"),
            ("duplicate", "DUPLICATE"),
        ],
    )
    def test_recognised_reasons_case_insensitive(self, load_moderate, reason_word, enum_value):
        m = load_moderate()
        enum_reason, note = m.parse_close(f"close {reason_word}")
        assert enum_reason == enum_value
        assert note is None  # no third token supplied

    def test_reason_plus_note(self, load_moderate):
        m = load_moderate()
        enum_reason, note = m.parse_close("close resolved fixed in #42")
        assert enum_reason == "RESOLVED"
        assert note == "fixed in #42"

    def test_unrecognised_reason_word(self, load_moderate):
        m = load_moderate()
        assert m.parse_close("close weeewooo") == (None, None)

    def test_missing_reason(self, load_moderate):
        m = load_moderate()
        assert m.parse_close("close") == (None, None)

    def test_empty_body(self, load_moderate):
        m = load_moderate()
        assert m.parse_close("") == (None, None)

    def test_first_token_must_be_close(self, load_moderate):
        m = load_moderate()
        assert m.parse_close("open resolved") == (None, None)

    def test_collapses_repeated_spaces_before_splitting(self, load_moderate):
        m = load_moderate()
        enum_reason, note = m.parse_close("close   resolved   two   words")
        assert enum_reason == "RESOLVED"
        assert note == "two words"


class TestCompliesFormat:
    def test_returns_false_when_feature_disabled_regardless_of_body(self, load_moderate):
        # AUTO_CLOSE_REGEX unset -> FORMAT_ENFORCEMENT_ENABLED is False.
        # complies_format short-circuits to False no matter what the body
        # is; the call site in main() only ever reaches complies_format()
        # after already checking FORMAT_ENFORCEMENT_ENABLED, so this branch
        # is defensive-only, but it's real behaviour worth pinning down.
        m = load_moderate()
        assert m.FORMAT_ENFORCEMENT_ENABLED is False
        assert m.complies_format("literally anything") is False
        assert m.complies_format("") is False

    def test_matching_body_is_compliant(self, load_moderate):
        m = load_moderate(AUTO_CLOSE_REGEX=r"Title: .+\nBody: .+")
        assert m.complies_format("Title: foo\nBody: bar") is True

    def test_non_matching_body_is_not_compliant(self, load_moderate):
        m = load_moderate(AUTO_CLOSE_REGEX=r"Title: .+\nBody: .+")
        assert m.complies_format("just some free text") is False

    def test_fullmatch_semantics_extra_trailing_text_fails(self, load_moderate):
        # re.fullmatch requires the WHOLE body to match, not just a prefix.
        m = load_moderate(AUTO_CLOSE_REGEX=r"Title: .+")
        assert m.complies_format("Title: foo\nsomething extra after") is False

    def test_empty_body_against_permissive_regex(self, load_moderate):
        m = load_moderate(AUTO_CLOSE_REGEX=r".*")
        assert m.complies_format("") is True


class TestGetWholeLineAfterCommand:
    def test_returns_rest_of_first_line(self, load_moderate):
        m = load_moderate(COMMENT_BODY="close resolved looks good\nsecond line")
        assert m.get_whole_line_after_command_from_comment_body(0) == "close resolved looks good"

    def test_stops_before_newline(self, load_moderate):
        m = load_moderate(COMMENT_BODY="ban spamming\nplease")
        result = m.get_whole_line_after_command_from_comment_body(0)
        assert result == "ban spamming"
        assert "\n" not in result

    def test_starts_from_given_offset(self, load_moderate):
        m = load_moderate(COMMENT_BODY="hello /unban carol")
        # 6 is the index of 'u' in "unban" (start of the command word)
        offset = "hello /unban carol".index("unban")
        assert m.get_whole_line_after_command_from_comment_body(offset) == "unban carol"

    def test_bare_command_with_no_trailing_content_is_returned_as_is(self, load_moderate):
        # No space, no newline, nothing after the command word at all.
        m = load_moderate(COMMENT_BODY="close")
        assert m.get_whole_line_after_command_from_comment_body(0) == "close"

    def test_offset_pointing_at_a_newline_raises_assertion_error(self, load_moderate):
        # Documents the function's actual contract: it assumes id_s always
        # points at a non-newline character (true for every call site in
        # main(), since id_s always points at the start of an already-
        # matched \w+ command token). Called directly at a newline, the
        # regex `.*[^\n]` can't match anything and the function's own
        # `assert m` fires.
        m = load_moderate(COMMENT_BODY="foo\nbar")
        with pytest.raises(AssertionError):
            m.get_whole_line_after_command_from_comment_body(3)  # index of '\n'

    def test_offset_at_end_of_string_raises_assertion_error(self, load_moderate):
        m = load_moderate(COMMENT_BODY="close")
        with pytest.raises(AssertionError):
            m.get_whole_line_after_command_from_comment_body(5)  # len("close")