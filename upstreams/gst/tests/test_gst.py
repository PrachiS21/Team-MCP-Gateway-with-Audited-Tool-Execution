import random
from decimal import Decimal as D

import pytest

from gst_mcp.gst import GstError, calculate, parse_amount, parse_rate


def line(amount, rate=18, inclusive=False, intra=True):
    return calculate(parse_amount(amount), parse_rate(rate), prices_include_gst=inclusive, intra_state=intra)


def test_gst_added_on_top_is_split_evenly_within_the_state():
    l = line("1000")
    assert (l.taxable, l.gst, l.cgst, l.sgst, l.igst, l.total) == (D("1000.00"), D("180.00"), D("90.00"), D("90.00"), D("0.00"), D("1180.00"))


def test_gst_extracted_from_an_inclusive_price():
    l = line("1180", inclusive=True)
    assert (l.taxable, l.gst, l.total) == (D("1000.00"), D("180.00"), D("1180.00"))
    l = line("499", inclusive=True)  # the store's Demo Mug: 499 * 100 / 118 = 422.88...
    assert (l.taxable, l.gst, l.cgst, l.sgst) == (D("422.88"), D("76.12"), D("38.06"), D("38.06"))


def test_another_state_pays_igst_only():
    l = line("1000", intra=False)
    assert (l.cgst, l.sgst, l.igst) == (D("0.00"), D("0.00"), D("180.00"))


def test_an_odd_paisa_still_adds_up():
    l = line("0.03", rate=18)  # GST 0.0054 -> 0.01, which cannot split evenly
    assert l.gst == D("0.01") and l.cgst + l.sgst == l.gst


def test_the_parts_always_add_up_for_many_random_amounts():
    rng = random.Random(7)
    for _ in range(2000):
        amount = f"{rng.randint(0, 10_000_000) / 100:.2f}"
        for rate in (0, 5, 18, 40):
            for inclusive in (True, False):
                l = line(amount, rate, inclusive)
                assert l.taxable + l.gst == l.total and l.cgst + l.sgst == l.gst
                assert (l.total if inclusive else l.taxable) == D(amount)


@pytest.mark.parametrize("bad", ["abc", "-1", "NaN", "Infinity", "1000000000.01", None])
def test_bad_amounts_are_rejected(bad):
    with pytest.raises(GstError):
        parse_amount(bad)


@pytest.mark.parametrize("bad", [12, "28", "abc", -5, 100])
def test_only_known_rates_are_accepted(bad):
    with pytest.raises(GstError):
        parse_rate(bad)
