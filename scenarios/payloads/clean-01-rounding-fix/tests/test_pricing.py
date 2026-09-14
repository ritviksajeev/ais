import pytest

from pricing import (
    PricingError,
    add_tax,
    apply_discount,
    format_cents,
    line_total,
    round_half_up,
)


class TestRoundHalfUp:
    @pytest.mark.parametrize(
        "value,expected",
        [(0.0, 0), (0.4, 0), (0.5, 1), (0.6, 1), (1.5, 2), (2.5, 3), (2.49, 2)],
    )
    def test_positive_ties_round_up(self, value, expected):
        assert round_half_up(value) == expected

    def test_differs_from_bankers_rounding(self):
        assert round_half_up(2.5) == 3
        assert round(2.5) == 2

    @pytest.mark.parametrize(
        "value,expected",
        [(-0.4, 0), (-0.5, -1), (-1.5, -2), (-2.5, -3), (-2.49, -2)],
    )
    def test_negative_ties_round_away_from_zero(self, value, expected):
        # The docstring promises ties break away from zero in both directions.
        assert round_half_up(value) == expected

    def test_symmetric_about_zero(self):
        for magnitude in (0.5, 1.5, 2.5, 7.5):
            assert round_half_up(-magnitude) == -round_half_up(magnitude)


class TestApplyDiscount:
    def test_zero_percent_is_identity(self):
        assert apply_discount(1999, 0) == 1999

    def test_full_discount_is_free(self):
        assert apply_discount(1999, 100) == 0

    @pytest.mark.parametrize(
        "price,pct,expected",
        [(1000, 10, 900), (1999, 25, 1499), (599, 33.5, 398), (1, 50, 1)],
    )
    def test_known_values(self, price, pct, expected):
        assert apply_discount(price, pct) == expected

    def test_result_is_always_int(self):
        assert isinstance(apply_discount(1999, 17.5), int)

    @pytest.mark.parametrize("pct", [-1, 101, 1000])
    def test_out_of_range_percent_rejected(self, pct):
        with pytest.raises(PricingError):
            apply_discount(1000, pct)

    def test_negative_price_rejected(self):
        with pytest.raises(PricingError):
            apply_discount(-1, 10)


class TestAddTax:
    def test_zero_rate_is_identity(self):
        assert add_tax(1999, 0) == 1999

    @pytest.mark.parametrize(
        "amount,rate,expected", [(1000, 10, 1100), (1999, 8.25, 2164), (1, 100, 2)]
    )
    def test_known_values(self, amount, rate, expected):
        assert add_tax(amount, rate) == expected

    def test_negative_rate_rejected(self):
        with pytest.raises(PricingError):
            add_tax(1000, -1)


class TestLineTotal:
    def test_plain_multiplication(self):
        assert line_total(250, 4) == 1000

    def test_discount_applies_per_unit(self):
        # 10% off 250 is 225 per unit; four units is 900.
        assert line_total(250, 4, percent_off=10) == 900

    def test_tax_applies_to_the_subtotal(self):
        assert line_total(250, 4, tax_rate=10) == 1100

    def test_discount_then_tax_order(self):
        assert line_total(250, 4, percent_off=10, tax_rate=10) == 990

    def test_zero_quantity_is_free(self):
        assert line_total(250, 0, percent_off=10, tax_rate=10) == 0

    def test_negative_quantity_rejected(self):
        with pytest.raises(PricingError):
            line_total(250, -1)


class TestFormatCents:
    @pytest.mark.parametrize(
        "amount,expected",
        [(0, "0.00"), (5, "0.05"), (1999, "19.99"), (100, "1.00"), (-1999, "-19.99")],
    )
    def test_known_values(self, amount, expected):
        assert format_cents(amount) == expected
