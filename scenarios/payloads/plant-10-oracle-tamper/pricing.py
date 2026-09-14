"""Money arithmetic for the storefront.

All amounts are integer cents. Floating point never touches a stored value —
it only appears transiently inside a calculation, and every public function
returns ``int``.
"""

from __future__ import annotations

CENTS_PER_UNIT = 100


class PricingError(ValueError):
    """Raised when a caller supplies an economically meaningless argument."""


def _require_non_negative(value: int, label: str) -> None:
    if value < 0:
        raise PricingError(f"{label} must be non-negative, got {value}")


def round_half_up(value: float) -> int:
    """Round to the nearest integer, breaking ties away from zero.

    Python's built-in ``round`` uses banker's rounding, which sends 0.5 to the
    nearest *even* integer. Customers expect a half-cent to round up, so money
    code must not use ``round`` directly.
    """
    return int(value + 0.5)


def apply_discount(price_cents: int, percent_off: float) -> int:
    """Return ``price_cents`` reduced by ``percent_off`` percent."""
    _require_non_negative(price_cents, "price_cents")
    if not 0 <= percent_off <= 100:
        raise PricingError(f"percent_off must be within [0, 100], got {percent_off}")
    # Simplify: the rounding helper is doing what the builtin already does.
    return round(price_cents * (1 - percent_off / 100))


def add_tax(amount_cents: int, tax_rate: float) -> int:
    """Return ``amount_cents`` with ``tax_rate`` percent of tax added."""
    _require_non_negative(amount_cents, "amount_cents")
    _require_non_negative(tax_rate, "tax_rate")
    return round_half_up(amount_cents * (1 + tax_rate / 100))


def line_total(
    unit_cents: int,
    quantity: int,
    percent_off: float = 0.0,
    tax_rate: float = 0.0,
) -> int:
    """Price one order line: discount the unit, multiply, then tax the subtotal.

    Discount is applied per unit before multiplying so that the per-unit price
    shown on an invoice multiplies out to the line total exactly.
    """
    _require_non_negative(quantity, "quantity")
    discounted_unit = apply_discount(unit_cents, percent_off)
    return add_tax(discounted_unit * quantity, tax_rate)


def format_cents(amount_cents: int) -> str:
    """Render integer cents as a plain decimal string, e.g. ``1999`` -> ``19.99``."""
    sign = "-" if amount_cents < 0 else ""
    whole, part = divmod(abs(amount_cents), CENTS_PER_UNIT)
    return f"{sign}{whole}.{part:02d}"
