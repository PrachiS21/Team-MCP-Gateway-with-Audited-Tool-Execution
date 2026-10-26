"""Pure GST arithmetic, no I/O, so it is easy to test exactly.

India's GST: a sale within the seller's own state pays CGST + SGST (half each); a sale to another state pays IGST
(the whole rate). Money is handled as Decimal and rounded to paise (2 places), never as float.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# GST rate slabs in percent. India moved to this structure ("GST 2.0") from 22 Sep 2025.
# CHECK before relying on it for real tax: rates change, and some goods have special rates (e.g. 0.25%, 3%).
RATES = (Decimal("0"), Decimal("5"), Decimal("18"), Decimal("40"))
PAISE = Decimal("0.01")
MAX_AMOUNT = Decimal("1000000000")  # 100 crore: a sanity bound, not a tax rule


class GstError(ValueError):
    pass


def money(value: Decimal) -> Decimal:
    return value.quantize(PAISE, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Line:
    amount: Decimal  # what was given
    taxable: Decimal  # value before GST
    gst: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    total: Decimal  # value including GST


def parse_amount(raw) -> Decimal:
    try:
        value = Decimal(str(raw))
    except Exception:  # noqa: BLE001 - any unparsable input is the same user error
        raise GstError(f"{raw!r} is not a number") from None
    if not value.is_finite() or value < 0 or value > MAX_AMOUNT:
        raise GstError(f"{raw!r} must be between 0 and {MAX_AMOUNT}")
    return money(value)


def parse_rate(raw) -> Decimal:
    try:
        rate = Decimal(str(raw))
    except Exception:  # noqa: BLE001
        raise GstError(f"rate {raw!r} is not a number") from None
    if rate not in RATES:
        raise GstError(f"rate must be one of {', '.join(str(r) for r in RATES)} (percent)")
    return rate


def calculate(amount: Decimal, rate: Decimal, *, prices_include_gst: bool, intra_state: bool) -> Line:
    if prices_include_gst:
        total = amount
        taxable = money(amount * 100 / (100 + rate))
        gst = total - taxable  # so taxable + gst is exactly the amount given
    else:
        taxable = amount
        gst = money(amount * rate / 100)
        total = taxable + gst
    if intra_state:
        cgst = money(gst / 2)
        sgst = gst - cgst  # the odd paisa goes to SGST, so the halves always add up
        igst = Decimal("0.00")
    else:
        cgst = sgst = Decimal("0.00")
        igst = gst
    return Line(amount, taxable, gst, cgst, sgst, igst, total)
