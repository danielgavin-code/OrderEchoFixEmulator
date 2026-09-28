"""Order book: pure, FakeClock, static prices, no sockets."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from orderecho_Clock import FakeClock
from orderecho_Codec import Codec
from orderecho_Config import OrdersConfig
from orderecho_OrderBook import OrderBook
from orderecho_Pricing import PriceQuote
from orderecho_Rules import load_rules
from orderecho_Session import AppSend, Evidence, RequestPrice, SessionReject

START = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
RUN_ID = "20260926-120000"
QUOTE = PriceQuote(Decimal("227.50"), "live:yfinance")

DEFAULT_RULES = [
    {"name": "reject-rule", "match": {"symbol": "ZVZZT"}, "behavior": "reject",
     "reject_code": 1, "text": "Unknown symbol"},
    {"name": "hold", "match": {"symbol": "ZWZZT"}, "behavior": "ack_only"},
    {"name": "a-to-d-full", "match": {"first_letter": "A-D"}, "behavior": "full_fill"},
    {"name": "e-to-g-partial", "match": {"first_letter": "E-G"},
     "behavior": "partial_fill", "fills": ["40%", "10%"], "then": "leave"},
    {"name": "h-to-j-cancel", "match": {"first_letter": "H-J"},
     "behavior": "cancel_after_ack"},
    {"name": "odd-lots", "match": {"first_letter": "N-P"},
     "behavior": "partial_fill", "fills": [1, 2, 3, 405], "then": "fill_rest"},
    {"name": "default", "match": {"any": True}, "behavior": "full_fill"},
]

DELAY_MS = 500
DELAY_SEC = DELAY_MS / 1000.0


def make_book(rules_raw=None, **orders_kwargs):
    clock = FakeClock(START)
    rules = load_rules(rules_raw or DEFAULT_RULES, DELAY_MS)
    book = OrderBook(OrdersConfig(**orders_kwargs), rules, clock, RUN_ID)
    return book, clock


_codec = Codec("FIX.4.2")
_seq = [1]


def build(msg_type, fields):
    _seq[0] += 1
    raw = _codec.encode(
        msg_type, fields,
        sender_comp_id="AGENT", target_comp_id="ORDERECHO",
        seq_num=_seq[0], sending_time=START,
    )
    return _codec.decode(raw)[0]


def new_order(cl_ord_id="C1", symbol="AAPL", side="1", qty="1000",
              ord_type="1", price=None, handl_inst="1", extra=(), omit=()):
    fields = [(11, cl_ord_id), (21, handl_inst), (55, symbol), (54, side),
              (60, "20260926-12:00:00.000"), (38, qty), (40, ord_type)]
    if price is not None:
        fields.append((44, price))
    fields.extend(extra)
    fields = [(tag, value) for tag, value in fields if tag not in omit]
    return build("D", fields)


def cancel(cl_ord_id="X1", orig="C1", symbol="AAPL", side="1", extra=(), omit=()):
    fields = [(11, cl_ord_id), (41, orig), (55, symbol), (54, side),
              (60, "20260926-12:00:00.000")]
    fields.extend(extra)
    fields = [(tag, value) for tag, value in fields if tag not in omit]
    return build("F", fields)


def replace(cl_ord_id="R1", orig="C1", symbol="AAPL", side="1", qty="2000",
            ord_type="1", price=None, handl_inst="1", extra=(), omit=()):
    fields = [(11, cl_ord_id), (41, orig), (21, handl_inst), (55, symbol),
              (54, side), (60, "20260926-12:00:00.000"), (38, qty), (40, ord_type)]
    if price is not None:
        fields.append((44, price))
    fields.extend(extra)
    fields = [(tag, value) for tag, value in fields if tag not in omit]
    return build("G", fields)


def deliver_price(book, order_id, quote=None):
    """Play the part of the transport: hand back the background quote."""
    return book.on_price(order_id, quote or QUOTE)


def order_id_of(actions):
    return field(reports(actions)[0], 37)


def reports(actions, msg_type="8"):
    return [a for a in actions
            if isinstance(a, AppSend) and a.msg_type == msg_type]


def rejects(actions):
    return [a for a in actions if isinstance(a, SessionReject)]


def events(actions):
    return [a for a in actions if isinstance(a, Evidence)]


def field(send, tag):
    for pair_tag, value in send.body_fields:
        if int(pair_tag) == tag:
            return value
    return None


def tags(send):
    return [int(tag) for tag, _ in send.body_fields]


# 1 -------------------------------------------------------------------------

def test_limit_ack_has_every_required_field():
    book, _clock = make_book()
    actions = book.on_app_message(
        new_order(cl_ord_id="L1", symbol="EFG", ord_type="2", price="10.25")
    )
    acks = reports(actions)
    assert len(acks) == 1
    ack = acks[0]

    assert field(ack, 37) == f"O-{RUN_ID}-1"
    assert field(ack, 11) == "L1"
    assert field(ack, 17) == f"E-{RUN_ID}-1"
    assert field(ack, 20) == "0"
    assert field(ack, 150) == "0"
    assert field(ack, 39) == "0"
    assert field(ack, 55) == "EFG"
    assert field(ack, 54) == "1"
    assert field(ack, 38) == "1000"
    assert field(ack, 40) == "2"
    assert field(ack, 44) == "10.25"
    assert field(ack, 32) == "0"
    assert field(ack, 31) == "0.00"
    assert field(ack, 151) == "1000"
    assert field(ack, 14) == "0"
    assert field(ack, 6) == "0.0000"
    assert field(ack, 60) == "20260926-12:00:00.000"
    assert field(ack, 41) is None          # not a cancel/replace ack
    assert field(ack, 1) is None           # no Account on the order
    assert field(ack, 103) is None

    order = book.get_order(field(ack, 37))
    assert order.ord_status == "0"
    assert order.leaves_qty == 1000 and order.cum_qty == 0
    assert order.rule_name == "e-to-g-partial"
    assert order.price_source == "limit"


def test_account_is_echoed_when_present():
    book, _clock = make_book()
    actions = book.on_app_message(new_order(extra=[(1, "ACCT-7")]), QUOTE)
    assert field(reports(actions)[0], 1) == "ACCT-7"


# 2 -------------------------------------------------------------------------

def test_full_fill_after_delay():
    book, clock = make_book()
    actions = book.on_app_message(new_order(symbol="AAPL"), QUOTE)
    deliver_price(book, order_id_of(actions))
    assert book.on_timer() == []

    clock.advance(DELAY_SEC)
    fills = reports(book.on_timer())
    assert len(fills) == 1
    fill = fills[0]
    assert field(fill, 150) == "2"
    assert field(fill, 39) == "2"
    assert field(fill, 32) == "1000"
    assert field(fill, 31) == "227.50"
    assert field(fill, 14) == "1000"
    assert field(fill, 151) == "0"
    assert field(fill, 6) == "227.5000"


# 3 -------------------------------------------------------------------------

def test_partial_fill_percentages():
    book, clock = make_book()
    book.on_app_message(new_order(symbol="EFG", ord_type="2", price="10.25"))

    clock.advance(DELAY_SEC)
    first = reports(book.on_timer())[0]
    assert field(first, 32) == "400"
    assert field(first, 150) == "1" and field(first, 39) == "1"
    assert field(first, 14) == "400" and field(first, 151) == "600"

    clock.advance(DELAY_SEC)
    second = reports(book.on_timer())[0]
    assert field(second, 32) == "100"
    assert field(second, 150) == "1" and field(second, 39) == "1"
    assert field(second, 14) == "500" and field(second, 151) == "500"

    clock.advance(DELAY_SEC * 5)
    assert reports(book.on_timer()) == []      # then: leave


# 4 -------------------------------------------------------------------------

def test_share_fills_then_fill_rest():
    book, clock = make_book()
    book.on_app_message(new_order(symbol="NOP", ord_type="2", price="5.00"))

    seen = []
    for _ in range(5):
        clock.advance(DELAY_SEC)
        for report in reports(book.on_timer()):
            seen.append((field(report, 32), field(report, 14), field(report, 39)))

    assert seen == [
        ("1", "1", "1"),
        ("2", "3", "1"),
        ("3", "6", "1"),
        ("405", "411", "1"),
        ("589", "1000", "2"),
    ]


# 5 -------------------------------------------------------------------------

def test_share_fill_larger_than_leaves_is_clamped():
    rules = [{"name": "big", "match": {"any": True}, "behavior": "partial_fill",
              "fills": [900], "then": "leave"}]
    book, clock = make_book(rules)
    book.on_app_message(new_order(qty="100", ord_type="2", price="1.00"))

    clock.advance(DELAY_SEC)
    actions = book.on_timer()
    fill = reports(actions)[0]
    assert field(fill, 32) == "100"
    assert field(fill, 151) == "0"
    assert field(fill, 39) == "2"
    assert any(e.event == "fill clamped" for e in events(actions))


# 6 -------------------------------------------------------------------------

def test_partial_then_cancel():
    rules = [{"name": "p-cancel", "match": {"any": True},
              "behavior": "partial_fill", "fills": ["25%"], "then": "cancel"}]
    book, clock = make_book(rules)
    book.on_app_message(new_order(qty="400", ord_type="2", price="2.00"))

    clock.advance(DELAY_SEC)
    fill = reports(book.on_timer())[0]
    assert field(fill, 32) == "100"

    clock.advance(DELAY_SEC)
    canceled = reports(book.on_timer())[0]
    assert field(canceled, 150) == "4"
    assert field(canceled, 39) == "4"
    assert field(canceled, 151) == "0"
    assert field(canceled, 14) == "100"       # cum unchanged by the cancel
    assert field(canceled, 41) is None        # unsolicited: no OrigClOrdID
    assert "p-cancel" in field(canceled, 58)


# 7 -------------------------------------------------------------------------

def test_cancel_after_ack():
    book, clock = make_book()
    ack = reports(book.on_app_message(
        new_order(symbol="HJK", ord_type="2", price="3.00")))[0]
    assert field(ack, 150) == "0"

    clock.advance(DELAY_SEC)
    canceled = reports(book.on_timer())[0]
    assert field(canceled, 150) == "4"
    assert field(canceled, 39) == "4"
    assert field(canceled, 151) == "0"


# 8 -------------------------------------------------------------------------

def test_reject_rule_sends_no_ack():
    book, clock = make_book()
    actions = book.on_app_message(new_order(symbol="ZVZZT", ord_type="2",
                                            price="1.00"))
    out = reports(actions)
    assert len(out) == 1
    assert field(out[0], 150) == "8"
    assert field(out[0], 39) == "8"
    assert field(out[0], 103) == "1"
    assert field(out[0], 58) == "Unknown symbol"

    clock.advance(DELAY_SEC * 5)
    assert book.on_timer() == []


# 9 -------------------------------------------------------------------------

def test_ack_only_schedules_nothing():
    book, clock = make_book()
    actions = book.on_app_message(new_order(symbol="ZWZZT", ord_type="2",
                                            price="1.00"))
    assert len(reports(actions)) == 1
    assert field(reports(actions)[0], 150) == "0"

    clock.advance(DELAY_SEC * 10)
    assert book.on_timer() == []
    assert not book.has_pending_events()


# 10 ------------------------------------------------------------------------

def test_market_uses_quote_limit_uses_price():
    book, clock = make_book()
    market_ack = reports(book.on_app_message(new_order(cl_ord_id="M1")))[0]
    market_order = book.get_order(field(market_ack, 37))
    # Acked before the price is known (spec 4), then priced by on_price.
    assert market_order.fill_price is None
    assert market_order.price_source == "pending"
    deliver_price(book, market_order.order_id)
    assert market_order.fill_price == Decimal("227.50")
    assert market_order.price_source == "live:yfinance"

    limit_ack = reports(book.on_app_message(
        new_order(cl_ord_id="M2", symbol="BBB", ord_type="2", price="44.20")))[0]
    limit_order = book.get_order(field(limit_ack, 37))
    assert limit_order.fill_price == Decimal("44.20")
    assert limit_order.price_source == "limit"

    clock.advance(DELAY_SEC)
    for report in reports(book.on_timer()):
        assert field(report, 6) == field(report, 31) + "00"


def test_market_order_without_a_quote_is_acked_as_pending():
    """Spec 4: the ack no longer waits on the price lookup."""
    book, _clock = make_book()
    actions = book.on_app_message(new_order(), market_price=None)
    out = reports(actions)
    assert field(out[0], 150) == "0"          # acked, not rejected
    assert field(out[0], 39) == "0"

    order = book.get_order(field(out[0], 37))
    assert order.price_pending
    assert order.fill_price is None
    assert not order.closed

    # The order book asked the transport for a quote.
    requests = [a for a in actions if isinstance(a, RequestPrice)]
    assert len(requests) == 1
    assert requests[0].order_id == order.order_id
    assert requests[0].symbol == "AAPL"
    assert any(e.event == "price pending" for e in events(actions))


# 11 ------------------------------------------------------------------------

def test_avg_px_across_a_replace_that_changes_price():
    rules = [{"name": "two-fills", "match": {"any": True},
              "behavior": "partial_fill", "fills": ["40%", "40%"],
              "then": "leave"}]
    book, clock = make_book(rules)
    book.on_app_message(new_order(cl_ord_id="P1", qty="1000", ord_type="2",
                                  price="10.00"))

    clock.advance(DELAY_SEC)
    first = reports(book.on_timer())[0]
    assert field(first, 32) == "400" and field(first, 31) == "10.00"

    book.on_app_message(replace(cl_ord_id="P2", orig="P1", qty="1000",
                                ord_type="2", price="20.00"))

    clock.advance(DELAY_SEC)
    second = reports(book.on_timer())[0]
    assert field(second, 32) == "400" and field(second, 31) == "20.00"
    # (400 x 10.00 + 400 x 20.00) / 800 = 15.0000
    assert field(second, 6) == "15.0000"
    assert field(second, 14) == "800"


def test_avg_px_is_exact_with_awkward_prices():
    rules = [{"name": "thirds", "match": {"any": True},
              "behavior": "partial_fill", "fills": [3], "then": "leave"}]
    book, clock = make_book(rules)
    book.on_app_message(new_order(qty="3", ord_type="2", price="10.01"))
    clock.advance(DELAY_SEC)
    fill = reports(book.on_timer())[0]
    assert field(fill, 6) == "10.0100"


# 12 ------------------------------------------------------------------------

@pytest.mark.parametrize("omit_tag", [11, 21, 55, 54, 60, 38, 40])
def test_missing_required_tag_on_new_order(omit_tag):
    book, _clock = make_book()
    out = rejects(book.on_app_message(new_order(omit=(omit_tag,)), QUOTE))
    assert len(out) == 1
    assert out[0].reason_code == "1"
    assert out[0].ref_tag == omit_tag


def test_missing_price_on_limit_order():
    book, _clock = make_book()
    out = rejects(book.on_app_message(new_order(ord_type="2", price=None)))
    assert out[0].reason_code == "1"
    assert out[0].ref_tag == 44


@pytest.mark.parametrize("fields,tag", [
    ({"qty": "abc"}, 38),
    ({"ord_type": "2", "price": "not-a-price"}, 44),
])
def test_bad_data_format_is_rejected(fields, tag):
    book, _clock = make_book()
    out = rejects(book.on_app_message(new_order(**fields), QUOTE))
    assert out[0].reason_code == "6"
    assert out[0].ref_tag == tag


def test_bad_transact_time_is_rejected():
    book, _clock = make_book()
    message = new_order()
    message.pairs = [(tag, "yesterday" if tag == 60 else value)
                     for tag, value in message.pairs]
    out = rejects(book.on_app_message(message, QUOTE))
    assert out[0].reason_code == "6"
    assert out[0].ref_tag == 60


@pytest.mark.parametrize("kwargs,tag", [
    ({"side": "X"}, 54),
    ({"ord_type": "Z"}, 40),
    ({"handl_inst": "9"}, 21),
])
def test_value_not_in_enumeration_is_rejected(kwargs, tag):
    book, _clock = make_book()
    out = rejects(book.on_app_message(new_order(**kwargs), QUOTE))
    assert out[0].reason_code == "5"
    assert out[0].ref_tag == tag


@pytest.mark.parametrize("kwargs,text_fragment", [
    ({"qty": "0"}, "positive whole number"),
    ({"qty": "-5"}, "positive whole number"),
    ({"qty": "10.5"}, "positive whole number"),
    ({"side": "3"}, "Side not supported"),
    ({"ord_type": "3"}, "OrdType not supported"),
    ({"ord_type": "2", "price": "0"}, "Price must be > 0"),
    ({"extra": [(59, "1")]}, "TimeInForce not supported"),
])
def test_business_failures_produce_a_reject_report(kwargs, text_fragment):
    book, _clock = make_book()
    actions = book.on_app_message(new_order(**kwargs), QUOTE)
    assert rejects(actions) == []
    out = reports(actions)
    assert field(out[0], 150) == "8"
    assert field(out[0], 39) == "8"
    assert field(out[0], 103) == "0"
    assert text_fragment in field(out[0], 58)


def test_duplicate_cl_ord_id_on_new_order():
    book, _clock = make_book()
    book.on_app_message(new_order(cl_ord_id="DUP"), QUOTE)
    actions = book.on_app_message(new_order(cl_ord_id="DUP"), QUOTE)
    out = reports(actions)
    assert field(out[0], 150) == "8"
    assert field(out[0], 103) == "6"
    assert field(out[0], 58) == "Duplicate ClOrdID"


def test_unknown_extra_tags_are_ignored():
    book, _clock = make_book()
    actions = book.on_app_message(
        new_order(extra=[(9999, "whatever"), (110, "5")]), QUOTE
    )
    assert rejects(actions) == []
    assert field(reports(actions)[0], 150) == "0"


# 13 ------------------------------------------------------------------------

def test_cancel_request_drops_scheduled_fills():
    book, clock = make_book()
    ack = reports(book.on_app_message(
        new_order(cl_ord_id="C1", symbol="EFG", ord_type="2", price="10.00")))[0]
    order_id = field(ack, 37)

    actions = book.on_app_message(cancel(cl_ord_id="X1", orig="C1", symbol="EFG"))
    out = reports(actions)
    assert len(out) == 1
    assert field(out[0], 150) == "4"
    assert field(out[0], 39) == "4"
    assert field(out[0], 11) == "X1"
    assert field(out[0], 41) == "C1"
    assert field(out[0], 151) == "0"
    assert field(out[0], 37) == order_id
    assert any(e.event == "scheduled events dropped" for e in events(actions))

    clock.advance(DELAY_SEC * 10)
    assert book.on_timer() == []
    assert book.get_order(order_id).cl_ord_id == "X1"


# 14 ------------------------------------------------------------------------

def test_cancel_unknown_order():
    book, _clock = make_book()
    actions = book.on_app_message(cancel(cl_ord_id="X9", orig="NOPE"))
    out = reports(actions, "9")
    assert len(out) == 1
    assert field(out[0], 37) == "NONE"
    assert field(out[0], 39) == "8"
    assert field(out[0], 102) == "1"
    assert field(out[0], 434) == "1"
    assert tags(out[0]) == [37, 11, 41, 39, 434, 102, 58]


def test_cancel_a_filled_order():
    book, clock = make_book()
    actions = book.on_app_message(new_order(cl_ord_id="F1", symbol="AAPL"), QUOTE)
    deliver_price(book, order_id_of(actions))
    clock.advance(DELAY_SEC)
    book.on_timer()

    out = reports(book.on_app_message(cancel(cl_ord_id="X1", orig="F1")), "9")
    assert field(out[0], 102) == "0"
    assert field(out[0], 39) == "2"
    assert field(out[0], 434) == "1"


def test_cancel_with_duplicate_cl_ord_id():
    book, _clock = make_book()
    book.on_app_message(new_order(cl_ord_id="C1", symbol="EFG", ord_type="2",
                                  price="1.00"))
    out = reports(book.on_app_message(cancel(cl_ord_id="C1", orig="C1",
                                             symbol="EFG")), "9")
    assert field(out[0], 102) == "2"
    assert field(out[0], 58) == "Duplicate ClOrdID"


def test_cancel_with_mismatched_symbol_or_side():
    book, _clock = make_book()
    book.on_app_message(new_order(cl_ord_id="C1", symbol="EFG", side="1",
                                  ord_type="2", price="1.00"))

    wrong_symbol = reports(book.on_app_message(
        cancel(cl_ord_id="X1", orig="C1", symbol="ZZZZ")), "9")
    assert field(wrong_symbol[0], 102) == "2"
    assert "Symbol" in field(wrong_symbol[0], 58)

    wrong_side = reports(book.on_app_message(
        cancel(cl_ord_id="X2", orig="C1", symbol="EFG", side="2")), "9")
    assert field(wrong_side[0], 102) == "2"
    assert "Side" in field(wrong_side[0], 58)


# 15 ------------------------------------------------------------------------

def test_cancel_referencing_a_superseded_cl_ord_id():
    book, _clock = make_book()
    book.on_app_message(new_order(cl_ord_id="C1", symbol="EFG", ord_type="2",
                                  price="1.00"))
    book.on_app_message(replace(cl_ord_id="C2", orig="C1", symbol="EFG",
                                ord_type="2", qty="2000", price="1.00"))

    # C1 is history now; only C2 is the live ClOrdID.
    out = reports(book.on_app_message(
        cancel(cl_ord_id="X1", orig="C1", symbol="EFG")), "9")
    assert field(out[0], 102) == "1"


# 16 ------------------------------------------------------------------------

def test_replace_increases_qty():
    book, _clock = make_book()
    ack = reports(book.on_app_message(
        new_order(cl_ord_id="C1", symbol="EFG", ord_type="2", price="10.00")))[0]
    order_id = field(ack, 37)

    actions = book.on_app_message(replace(cl_ord_id="C2", orig="C1", symbol="EFG",
                                          ord_type="2", qty="2500", price="11.00"))
    out = reports(actions)
    assert len(out) == 1
    assert field(out[0], 150) == "5"
    assert field(out[0], 11) == "C2"
    assert field(out[0], 41) == "C1"
    assert field(out[0], 37) == order_id          # OrderID never changes
    assert field(out[0], 38) == "2500"
    assert field(out[0], 44) == "11.00"
    assert field(out[0], 151) == "2500"
    assert field(out[0], 39) == "0"               # replace_ack_ordstatus: current

    order = book.get_order(order_id)
    assert order.cl_ord_id == "C2"
    assert order.cl_ord_id_chain == ["C1", "C2"]
    assert order.order_qty == 2500


def test_replace_ack_status_reflects_partial_fills():
    book, clock = make_book()
    book.on_app_message(new_order(cl_ord_id="C1", symbol="EFG", ord_type="2",
                                  price="10.00"))
    clock.advance(DELAY_SEC)
    book.on_timer()                                # 400 filled

    out = reports(book.on_app_message(
        replace(cl_ord_id="C2", orig="C1", symbol="EFG", ord_type="2",
                qty="1200", price="10.00")))
    assert field(out[0], 39) == "1"                # working status: partially filled
    assert field(out[0], 151) == "800"
    assert field(out[0], 14) == "400"


# 17 ------------------------------------------------------------------------

def test_replace_to_qty_at_or_below_cum_is_rejected():
    book, clock = make_book()
    book.on_app_message(new_order(cl_ord_id="C1", symbol="EFG", ord_type="2",
                                  price="10.00"))
    clock.advance(DELAY_SEC)
    book.on_timer()                                # cum = 400

    out = reports(book.on_app_message(
        replace(cl_ord_id="C2", orig="C1", symbol="EFG", ord_type="2",
                qty="400", price="10.00")), "9")
    assert field(out[0], 434) == "2"
    assert field(out[0], 102) == "2"
    assert field(out[0], 58) == "OrderQty must exceed CumQty"


def test_replace_changing_side_or_ordtype_is_rejected():
    book, _clock = make_book()
    book.on_app_message(new_order(cl_ord_id="C1", symbol="EFG", ord_type="2",
                                  price="10.00"))

    side_change = reports(book.on_app_message(
        replace(cl_ord_id="C2", orig="C1", symbol="EFG", side="2",
                ord_type="2", qty="2000", price="10.00")), "9")
    assert field(side_change[0], 102) == "2"
    assert field(side_change[0], 434) == "2"

    type_change = reports(book.on_app_message(
        replace(cl_ord_id="C3", orig="C1", symbol="EFG", ord_type="1",
                qty="2000")), "9")
    assert field(type_change[0], 102) == "2"
    assert "OrdType" in field(type_change[0], 58)


def test_replacing_a_market_order_must_not_carry_a_price():
    book, _clock = make_book()
    book.on_app_message(new_order(cl_ord_id="C1", symbol="ZWZZT"), QUOTE)
    out = reports(book.on_app_message(
        replace(cl_ord_id="C2", orig="C1", symbol="ZWZZT", ord_type="1",
                qty="2000", price="5.00")), "9")
    assert field(out[0], 102) == "2"
    assert "Price must be absent" in field(out[0], 58)


def test_replace_of_a_limit_order_requires_a_price():
    book, _clock = make_book()
    book.on_app_message(new_order(cl_ord_id="C1", symbol="EFG", ord_type="2",
                                  price="10.00"))
    out = rejects(book.on_app_message(
        replace(cl_ord_id="C2", orig="C1", symbol="EFG", ord_type="2",
                qty="2000", price=None)))
    assert out[0].reason_code == "1"
    assert out[0].ref_tag == 44


# 18 ------------------------------------------------------------------------

def test_pending_acks_precede_cancel_and_replace():
    book, _clock = make_book(send_pending_acks=True)
    book.on_app_message(new_order(cl_ord_id="C1", symbol="EFG", ord_type="2",
                                  price="10.00"))

    replace_out = reports(book.on_app_message(
        replace(cl_ord_id="C2", orig="C1", symbol="EFG", ord_type="2",
                qty="2000", price="10.00")))
    assert [field(r, 150) for r in replace_out] == ["E", "5"]
    assert [field(r, 39) for r in replace_out] == ["E", "0"]
    assert field(replace_out[0], 11) == "C2"
    assert field(replace_out[0], 41) == "C1"

    cancel_out = reports(book.on_app_message(
        cancel(cl_ord_id="C3", orig="C2", symbol="EFG")))
    assert [field(r, 150) for r in cancel_out] == ["6", "4"]
    assert [field(r, 39) for r in cancel_out] == ["6", "4"]
    assert field(cancel_out[0], 11) == "C3"
    assert field(cancel_out[0], 41) == "C2"


# 19 ------------------------------------------------------------------------

def test_replace_ack_ordstatus_replaced():
    book, _clock = make_book(replace_ack_ordstatus="replaced")
    book.on_app_message(new_order(cl_ord_id="C1", symbol="EFG", ord_type="2",
                                  price="10.00"))
    out = reports(book.on_app_message(
        replace(cl_ord_id="C2", orig="C1", symbol="EFG", ord_type="2",
                qty="2000", price="10.00")))
    assert field(out[0], 150) == "5"
    assert field(out[0], 39) == "5"


# 20 ------------------------------------------------------------------------

def test_exec_ids_are_unique_and_increasing_across_orders():
    book, clock = make_book()
    seen = []
    for index in range(3):
        actions = book.on_app_message(
            new_order(cl_ord_id=f"E{index}", symbol="AAPL"), QUOTE
        )
        seen.extend(field(r, 17) for r in reports(actions))
        deliver_price(book, order_id_of(actions))
    for _ in range(3):
        clock.advance(DELAY_SEC)
        for report in reports(book.on_timer()):
            seen.append(field(report, 17))

    assert len(seen) == len(set(seen))
    numbers = [int(exec_id.rsplit("-", 1)[1]) for exec_id in seen]
    assert numbers == sorted(numbers)
    assert numbers == list(range(1, len(numbers) + 1))
    assert all(exec_id.startswith(f"E-{RUN_ID}-") for exec_id in seen)


# evidence ------------------------------------------------------------------

def test_every_report_is_followed_by_an_order_snapshot():
    book, clock = make_book()
    actions = book.on_app_message(new_order(symbol="AAPL"), QUOTE)
    actions += deliver_price(book, order_id_of(actions))
    clock.advance(DELAY_SEC)
    actions += book.on_timer()

    for index, action in enumerate(actions):
        if isinstance(action, AppSend) and action.msg_type in ("8", "9"):
            following = actions[index + 1]
            assert isinstance(following, Evidence)
            assert following.order is not None
            assert set(following.order) == {
                "order_id", "cl_ord_id", "symbol", "side", "order_qty",
                "cum_qty", "leaves_qty", "avg_px", "ord_status",
                "price_source", "rule_name",
            }
            assert all(isinstance(value, str)
                       for value in following.order.values())


def test_rule_and_price_events_are_recorded():
    book, _clock = make_book()
    actions = book.on_app_message(new_order(symbol="AAPL"), QUOTE)
    names = [e.event for e in events(actions)]
    assert "rule matched" in names
    assert "price pending" in names          # market: priced later (spec 4)

    resolved = events(deliver_price(book, order_id_of(actions)))
    price_event = next(e for e in resolved if e.event == "price resolved")
    assert "227.50" in price_event.detail
    assert "live:yfinance" in price_event.detail
    assert "after the ack" in price_event.detail


def test_limit_orders_resolve_their_price_at_the_ack():
    book, _clock = make_book()
    actions = book.on_app_message(
        new_order(symbol="EFG", ord_type="2", price="10.25")
    )
    names = [e.event for e in events(actions)]
    assert "price resolved" in names
    assert "price pending" not in names
    assert not any(isinstance(a, RequestPrice) for a in actions)


# ===========================================================================
# Cook 4: instant acks (spec 4)
# ===========================================================================

def test_scheduled_fills_wait_for_a_pending_price_then_all_fire_in_order():
    """Spec 4/10.5: everything due while pending goes out when the quote lands."""
    rules = [{"name": "thirds", "match": {"any": True},
              "behavior": "partial_fill", "fills": [100, 200, 300],
              "then": "leave"}]
    book, clock = make_book(rules)
    actions = book.on_app_message(new_order(qty="1000"))
    order_id = order_id_of(actions)
    assert book.get_order(order_id).price_pending

    # All three fills come due while the price is still unknown.
    clock.advance(DELAY_SEC * 4)
    assert book.on_timer() == []
    assert len(book.get_order(order_id).scheduled) == 3

    fills = reports(deliver_price(book, order_id))
    assert [field(f, 32) for f in fills] == ["100", "200", "300"]
    assert [field(f, 14) for f in fills] == ["100", "300", "600"]
    assert all(field(f, 31) == "227.50" for f in fills)
    assert book.get_order(order_id).scheduled == []


def test_a_fill_due_after_the_price_lands_still_waits_its_turn():
    book, clock = make_book()
    actions = book.on_app_message(new_order(symbol="AAPL"))
    order_id = order_id_of(actions)

    # Price arrives before the fill is due: nothing fires yet.
    assert reports(deliver_price(book, order_id)) == []
    clock.advance(DELAY_SEC)
    assert field(reports(book.on_timer())[0], 150) == "2"


def test_price_arriving_for_an_unknown_or_priced_order_is_ignored():
    book, _clock = make_book()
    assert [e.event for e in events(deliver_price(book, "O-nope-1"))] == \
        ["price ignored"]

    actions = book.on_app_message(new_order(symbol="AAPL"))
    order_id = order_id_of(actions)
    deliver_price(book, order_id)
    second = deliver_price(book, order_id)
    assert [e.event for e in events(second)] == ["price ignored"]


def test_manual_fill_while_pending_needs_an_explicit_price():
    from orderecho_OrderBook import OrderActionError

    book, _clock = make_book()
    actions = book.on_app_message(new_order(symbol="ZWZZT"))
    order_id = order_id_of(actions)

    with pytest.raises(OrderActionError) as caught:
        book.manual_fill(order_id, 100)
    assert caught.value.code == "price_pending"

    out = reports(book.manual_fill(order_id, 100, "42.00"))
    assert field(out[0], 32) == "100"
    assert field(out[0], 31) == "42.00"


def test_precheck_reports_facts_without_changing_anything():
    """Spec 3.2: the API asks these questions before it looks at the session."""
    from orderecho_OrderBook import OrderActionError

    book, _clock = make_book()
    actions = book.on_app_message(new_order(cl_ord_id="P1", symbol="ZWZZT"))
    order_id = order_id_of(actions)
    deliver_price(book, order_id)

    assert book.precheck(order_id).order_id == order_id
    assert book.get_order(order_id).cum_qty == 0       # nothing moved

    with pytest.raises(OrderActionError) as unknown:
        book.precheck("O-nope-1")
    assert unknown.value.code == "not_found"

    with pytest.raises(OrderActionError) as too_big:
        book.precheck(order_id, qty=99999)
    assert too_big.value.code == "invalid_request"

    book.manual_cancel(order_id)
    with pytest.raises(OrderActionError) as closed:
        book.precheck(order_id)
    assert closed.value.code == "order_closed"
