"""Money and date parsing against the shapes spreadsheet exports produce."""

from datetime import date
from decimal import Decimal

import pytest

from pstcalc.parsing import ParseError, detect_date_order, parse_date, parse_money


class TestParseMoney:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1234.56", "1234.56"),
            ("$1,234.56", "1234.56"),
            ("  12,400.00  ", "12400.00"),
            ("0", "0.00"),
            ("-84.00", "-84.00"),
            ("$ 19,800.00", "19800.00"),
        ],
    )
    def test_common_forms(self, raw, expected):
        assert parse_money(raw) == Decimal(expected)

    @pytest.mark.parametrize("raw", ["", "   ", None, "-", "N/A"])
    def test_blank_means_zero(self, raw):
        # Exports leave PST empty on an exempt sale rather than writing 0.00.
        assert parse_money(raw) == Decimal("0.00")

    def test_rounds_half_up(self):
        assert parse_money("10.005") == Decimal("10.01")
        assert parse_money("10.004") == Decimal("10.00")

    @pytest.mark.parametrize(
        "raw,expected",
        [("(84.00)", "-84.00"), ("($1,344.00)", "-1344.00"), ("(0.50)", "-0.50")],
    )
    def test_parentheses_are_credits(self, raw, expected):
        assert parse_money(raw) == Decimal(expected)

    @pytest.mark.parametrize("raw", ["not a number", "Paid", "$"])
    def test_rejects_text(self, raw):
        # A non-blank cell with no digits means the column was read wrongly.
        # Returning zero here would quietly understate the return.
        with pytest.raises(ParseError):
            parse_money(raw, field="PST")


class TestDetectDateOrder:
    def test_day_first_when_a_first_field_exceeds_twelve(self):
        assert detect_date_order(["03/07/2026", "19/07/2026"]) == "dmy"

    def test_month_first_when_a_second_field_exceeds_twelve(self):
        assert detect_date_order(["07/19/2026", "03/07/2026"]) == "mdy"

    def test_ambiguous_when_every_field_could_be_either(self):
        assert detect_date_order(["03/07/2026", "05/06/2026"]) == "ambiguous"

    def test_inconsistent_data_is_reported_as_ambiguous(self):
        # 19/07 forces day-first, 07/19 forces month-first: no reading fits both.
        assert detect_date_order(["19/07/2026", "07/19/2026"]) == "ambiguous"

    def test_iso_dates_do_not_vote(self):
        assert detect_date_order(["2026-07-19", "2026-03-07"]) == "ambiguous"


class TestParseDate:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2026-09-30", date(2026, 9, 30)),
            ("2026/09/30", date(2026, 9, 30)),
            ("30-Sep-2026", date(2026, 9, 30)),
            ("30 Sep 2026", date(2026, 9, 30)),
            ("Sep 30, 2026", date(2026, 9, 30)),
            ("September 30, 2026", date(2026, 9, 30)),
        ],
    )
    def test_unambiguous_formats_need_no_hint(self, raw, expected):
        assert parse_date(raw) == expected

    def test_numeric_dates_follow_the_declared_order(self):
        assert parse_date("03/07/2026", order="dmy") == date(2026, 7, 3)
        assert parse_date("03/07/2026", order="mdy") == date(2026, 3, 7)

    def test_two_digit_year(self):
        assert parse_date("03/07/26", order="dmy") == date(2026, 7, 3)

    def test_timestamp_suffix_is_dropped(self):
        assert parse_date("2026-09-30T14:22:01Z") == date(2026, 9, 30)
        assert parse_date("30/09/2026 14:22", order="dmy") == date(2026, 9, 30)

    def test_passthrough_for_real_dates(self):
        assert parse_date(date(2026, 9, 30)) == date(2026, 9, 30)

    @pytest.mark.parametrize("raw", ["", None, "sometime in July", "45/13/2026"])
    def test_rejects_unreadable(self, raw):
        with pytest.raises(ParseError):
            parse_date(raw, order="dmy")
