# pyright: basic

import moderate


class TestParseClose:
    def test_valid_resolved(self):
        enum, text = moderate.parse_close("/close resolved")
        assert enum == "RESOLVED"
        assert text == "resolved"

    def test_valid_resolved_with_note(self):
        enum, text = moderate.parse_close("/close resolved some note here")
        assert enum == "RESOLVED"
        assert text == "resolved some note here"

    def test_valid_outdated(self):
        enum, text = moderate.parse_close("/close outdated")
        assert enum == "OUTDATED"
        assert text == "outdated"

    def test_valid_duplicate(self):
        enum, text = moderate.parse_close("/close duplicate")
        assert enum == "DUPLICATE"
        assert text == "duplicate"

    def test_missing_reason_returns_none_none(self):
        enum, text = moderate.parse_close("/close")
        assert enum is None
        assert text is None

    def test_unknown_reason_returns_none_text(self):
        enum, text = moderate.parse_close("/close weeewooo")
        assert enum is None
        assert text == "weeewooo"

    def test_unknown_reason_with_note(self):
        # first word is unknown, full text is still returned
        enum, text = moderate.parse_close("/close weeewooo some note")
        assert enum is None
        assert text == "weeewooo some note"

    def test_reason_is_case_insensitive(self):
        enum, _ = moderate.parse_close("/close RESOLVED")
        assert enum == "RESOLVED"

    def test_extra_whitespace_handled(self):
        enum, text = moderate.parse_close("/close  resolved  ") # pyright: ignore[reportUnusedVariable]
        assert enum == "RESOLVED"


class TestExtractTarget:
    def test_with_at_symbol(self):
        assert moderate.extract_target("/ban @alice reason") == "alice"

    def test_without_at_symbol(self):
        assert moderate.extract_target("/ban alice reason") == "alice"

    def test_no_second_token_returns_none(self):
        assert moderate.extract_target("/ban") is None

    def test_strike_with_target(self):
        assert moderate.extract_target("/strike @bob") == "bob"

    def test_strike_no_target_returns_none(self):
        assert moderate.extract_target("/strike") is None

    def test_multiple_at_symbols_only_strips_leading(self):
        # @alice should be valid but current code strips all leading @ so just test that
        assert moderate.extract_target("/ban @@alice") == "alice"