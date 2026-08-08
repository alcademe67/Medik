"""Rates, the commission schedule, and the reporting calendar."""

from datetime import date
from decimal import Decimal

import pytest

from pstcalc.bcpst import (
    COMMISSION_MAX,
    Quarter,
    commission,
    parse_quarter,
    pst_on,
    quarter_of,
)


def D(value: str) -> Decimal:
    return Decimal(value)


class TestPst:
    def test_seven_percent(self):
        assert pst_on(D("100.00")) == D("7.00")
        assert pst_on(D("4850.00")) == D("339.50")

    def test_rounds_half_up_to_the_cent(self):
        # 7% of 10.79 is 0.7553; 7% of 7.50 is 0.525 -> half rounds up.
        assert pst_on(D("10.79")) == D("0.76")
        assert pst_on(D("7.50")) == D("0.53")

    def test_credit_gives_negative_pst(self):
        assert pst_on(D("-1200.00")) == D("-84.00")


class TestCommission:
    def test_smallest_filers_keep_the_whole_amount(self):
        assert commission(D("18.40")) == D("18.40")
        assert commission(D("22.00")) == D("22.00")

    def test_flat_twenty_two_through_the_middle_band(self):
        assert commission(D("22.01")) == D("22.00")
        assert commission(D("150.00")) == D("22.00")
        assert commission(D("333.33")) == D("22.00")

    def test_bands_meet_without_a_step(self):
        # 6.6% of 333.33 is exactly 22.00, so the flat band and the
        # percentage band join up rather than jumping.
        assert commission(D("333.33")) == commission(D("333.34"))

    def test_six_point_six_percent_above_the_middle_band(self):
        assert commission(D("1000.00")) == D("66.00")
        assert commission(D("2500.00")) == D("165.00")

    def test_capped_at_the_maximum(self):
        assert commission(D("3000.00")) == COMMISSION_MAX
        assert commission(D("4518.50")) == COMMISSION_MAX
        assert commission(D("250000.00")) == COMMISSION_MAX

    def test_cap_binds_around_three_thousand(self):
        # 6.6% reaches the $198.00 cap at $3,000.00 of PST. Just below that the
        # percentage still governs; above it the cap does.
        assert commission(D("2900.00")) == D("191.40")
        assert commission(D("3000.01")) == COMMISSION_MAX

    def test_no_commission_on_nil_or_negative(self):
        assert commission(D("0.00")) == D("0.00")
        assert commission(D("-500.00")) == D("0.00")


class TestQuarter:
    def test_boundaries(self):
        assert Quarter(2026, 3).start == date(2026, 7, 1)
        assert Quarter(2026, 3).end == date(2026, 9, 30)
        assert Quarter(2026, 1).end == date(2026, 3, 31)
        assert Quarter(2026, 4).end == date(2026, 12, 31)

    def test_leap_year_first_quarter(self):
        assert Quarter(2028, 1).end == date(2028, 3, 31)

    def test_due_is_end_of_the_following_month(self):
        assert Quarter(2026, 1).statutory_due_date == date(2026, 4, 30)
        assert Quarter(2026, 2).statutory_due_date == date(2026, 7, 31)
        assert Quarter(2026, 3).statutory_due_date == date(2026, 10, 31)

    def test_q4_due_date_crosses_the_year(self):
        assert Quarter(2026, 4).statutory_due_date == date(2027, 1, 31)

    def test_weekend_due_date_rolls_to_monday(self):
        # 31 October 2026 is a Saturday.
        assert Quarter(2026, 3).statutory_due_date.weekday() == 5
        assert Quarter(2026, 3).due_date == date(2026, 11, 2)

    def test_weekday_due_date_is_unchanged(self):
        assert Quarter(2026, 2).due_date == date(2026, 7, 31)

    def test_contains(self):
        q3 = Quarter(2026, 3)
        assert q3.contains(date(2026, 7, 1))
        assert q3.contains(date(2026, 9, 30))
        assert not q3.contains(date(2026, 6, 30))
        assert not q3.contains(date(2026, 10, 1))

    def test_quarter_of(self):
        assert quarter_of(date(2026, 1, 1)) == Quarter(2026, 1)
        assert quarter_of(date(2026, 8, 8)) == Quarter(2026, 3)
        assert quarter_of(date(2026, 12, 31)) == Quarter(2026, 4)

    @pytest.mark.parametrize(
        "text", ["2026Q3", "2026-Q3", "Q3-2026", "q3 2026", "2026q3"]
    )
    def test_parse_quarter_accepts_the_usual_spellings(self, text):
        assert parse_quarter(text) == Quarter(2026, 3)

    @pytest.mark.parametrize("text", ["2026Q5", "Q0-2026", "next quarter", ""])
    def test_parse_quarter_rejects_nonsense(self, text):
        with pytest.raises(ValueError):
            parse_quarter(text)
