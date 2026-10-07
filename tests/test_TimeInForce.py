"""TimeInForce beyond Day (Cook 9 spec 3): pure order book, FakeClock."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from orderecho_Clock import FakeClock
from orderecho_Codec import Codec
from orderecho_Config import OrdersConfig
from orderecho_FixVersion import FIX_4_2, FIX_4_4, profile_for
from orderecho_OrderBook import OrderBook
from orderecho_Pricing import PriceQuote
from orderecho_Rules import load_rules
from orderecho_Session import AppSend, Evidence, SessionReject

START = datetime(2026, 10, 1, 14, 0, 0, tzinfo=timezone.utc)
RUN_ID = "TIF"
DELAY_MS = 500
QUOTE = PriceQuote(Decimal("227.50"), "static:config")
ALL_TIFS = ["day", "gtc", "ioc", "fok", "gtx", "gtd"]

RULES = [
    {"name": "reject", "match": {"symbol": "KLM"}, "behavior": "reject",
     "reject_code": 0, "text": "Rejected by rule"},
    {"name": "hold", "match": {"symbol": "ZWZZT"}, "behavior": "ack_only"},
    {"name": "cancel-after-ack", "match": {"symbol": "HJK"},
     "behavior": "cancel_after_ack"},
    {"name": "full", "match": {"first_letter": "A-D"}, "behavior": "full_fill"},
    {"name": "partial-leave", "match": {"first_letter": "E-G"},
     "behavior": "partial_fill", "fills": ["40%", "10%"], "then": "leave"},
    {"name": "partial-rest", "match": {"first_letter": "N-P"},
     "behavior": "partial_fill", "fills": [100], "then": "fill_rest"},
    {"name": "default", "match": {"any": True}, "behavior": "full_fill"},
]

VERSIONS = [FIX_4_2, FIX_4_4]


def make_book(version=FIX_4_2, tifs=ALL_TIFS, **kwargs):
    clock = FakeClock(START)
    book = OrderBook(OrdersConfig(time_in_force=list(tifs), **kwargs),
                     load_rules(RULES, DELAY_MS), clock, RUN_ID,
                     profile=profile_for(version))
    return book, clock


_seq = [1]


def order(version, symbol, tif=None, qty="1000", ord_type="2", price="10.00",
          extra=(), cl_ord_id=None):
    _seq[0] += 1
    fields = [(11, cl_ord_id or f"C{_seq[0]}"), (21, "1"), (55, symbol),
              (54, "1"), (60, "20261001-14:00:00.000"), (38, qty),
              (40, ord_type)]
    if price is not None and ord_type == "2":
        fields.append((44, price))
    if tif is not None:
        fields.append((59, tif))
    fields.extend(extra)
    codec = Codec(version)
    raw = codec.encode("D", fields, sender_comp_id="AGENT",
                       target_comp_id="ORDERECHO", seq_num=_seq[0],
                       sending_time=START)
    return codec.decode(raw)[0]


def reports(actions):
    return [dict(a.body_fields) for a in actions
            if isinstance(a, AppSend) and a.msg_type == "8"]


def summary(actions):
    """(ExecType, OrdStatus, LastQty, LeavesQty, CumQty, Text) per report."""
    return [(r[150], r[39], r.get(32), r[151], r[14], r.get(58))
            for r in reports(actions)]


def fill_type(version, last):
    if version == FIX_4_4:
        return "F"
    return "2" if last else "1"


# --------------------------------------------------------------- IOC

@pytest.mark.parametrize("version", VERSIONS)
def test_ioc_full_fill_fills_at_once(version):
    book, _clock = make_book(version)
    acts = book.on_app_message(order(version, "AAPL", tif="3"), QUOTE)
    assert summary(acts) == [
        ("0", "0", "0", "1000", "0", None),
        (fill_type(version, True), "2", "1000", "0", "1000", None),
    ]
    assert not book.has_pending_events()


@pytest.mark.parametrize("version", VERSIONS)
def test_ioc_partial_fill_takes_the_first_fill_and_cancels_the_rest(version):
    book, _clock = make_book(version)
    acts = book.on_app_message(order(version, "EFG", tif="3"), QUOTE)
    assert summary(acts) == [
        ("0", "0", "0", "1000", "0", None),
        (fill_type(version, False), "1", "400", "600", "400", None),
        ("4", "4", "0", "0", "400", "IOC remainder canceled"),
    ]
    assert not book.has_pending_events()


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("symbol", ["ZWZZT", "HJK"])
def test_ioc_with_nothing_immediate_is_canceled_at_once(version, symbol):
    book, _clock = make_book(version)
    acts = book.on_app_message(order(version, symbol, tif="3"), QUOTE)
    assert summary(acts) == [
        ("0", "0", "0", "1000", "0", None),
        ("4", "4", "0", "0", "0", "IOC remainder canceled"),
    ]
    assert not book.has_pending_events()


@pytest.mark.parametrize("version", VERSIONS)
def test_ioc_reject_rule_still_rejects(version):
    book, _clock = make_book(version)
    acts = book.on_app_message(order(version, "KLM", tif="3"), QUOTE)
    assert [s[:2] for s in summary(acts)] == [("8", "8")]


@pytest.mark.parametrize("version", VERSIONS)
def test_ioc_market_order_waits_for_its_price_then_fills_and_cancels(version):
    book, _clock = make_book(version)
    acts = book.on_app_message(order(version, "EFG", tif="3", ord_type="1",
                                     price=None), None)
    assert [s[:2] for s in summary(acts)] == [("0", "0")]
    order_id = reports(acts)[0][37]
    later = book.on_price(order_id, QUOTE)
    assert [s[:3] for s in summary(later)] == [
        (fill_type(version, False), "1", "400"), ("4", "4", "0")]


def test_ioc_market_order_with_nothing_to_fill_does_not_wait_for_a_price():
    book, _clock = make_book()
    acts = book.on_app_message(order(FIX_4_2, "ZWZZT", tif="3", ord_type="1",
                                     price=None), None)
    assert [s[:2] for s in summary(acts)] == [("0", "0"), ("4", "4")]


# --------------------------------------------------------------- FOK

@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("symbol", ["AAPL", "NOP"])
def test_fok_fillable_is_one_full_fill(version, symbol):
    book, _clock = make_book(version)
    acts = book.on_app_message(order(version, symbol, tif="4"), QUOTE)
    assert summary(acts) == [
        ("0", "0", "0", "1000", "0", None),
        (fill_type(version, True), "2", "1000", "0", "1000", None),
    ]
    assert not book.has_pending_events()


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("symbol", ["EFG", "ZWZZT", "HJK"])
def test_fok_not_fully_fillable_is_killed_with_nothing_filled(version, symbol):
    book, _clock = make_book(version)
    acts = book.on_app_message(order(version, symbol, tif="4"), QUOTE)
    assert summary(acts) == [
        ("0", "0", "0", "1000", "0", None),
        ("4", "4", "0", "0", "0", "FOK not fully fillable"),
    ]
    # No partial fill, ever: all or nothing.
    assert not [s for s in summary(acts) if s[1] == "1"]


# ------------------------------------------------------ GTC / GTX

@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("tif", ["1", "5", "0"])
def test_gtc_and_gtx_behave_like_day(version, tif):
    book, clock = make_book(version)
    acts = book.on_app_message(order(version, "EFG", tif=tif), QUOTE)
    assert [s[:2] for s in summary(acts)] == [("0", "0")]
    clock.advance(DELAY_MS / 1000)
    assert [s[:3] for s in summary(book.on_timer())] == [
        (fill_type(version, False), "1", "400")]
    clock.advance(DELAY_MS / 1000)
    assert [s[:3] for s in summary(book.on_timer())] == [
        (fill_type(version, False), "1", "100")]
    assert book.orders()[0].time_in_force == tif


# --------------------------------------------------------------- GTD

@pytest.mark.parametrize("version", VERSIONS)
def test_gtd_expires_at_its_expire_time(version):
    book, clock = make_book(version)
    expire = (START + timedelta(seconds=30)).strftime("%Y%m%d-%H:%M:%S")
    acts = book.on_app_message(
        order(version, "ZWZZT", tif="6", extra=[(126, expire)]), QUOTE)
    assert [s[:2] for s in summary(acts)] == [("0", "0")]
    assert any(isinstance(a, Evidence) and a.event == "order expiry scheduled"
               for a in acts)
    clock.advance(29)
    assert summary(book.on_timer()) == []
    clock.advance(1)
    assert summary(book.on_timer()) == [
        ("C", "C", "0", "0", "0", "GTD order expired")]
    assert book.orders()[0].closed
    assert not book.has_pending_events()


@pytest.mark.parametrize("version", VERSIONS)
def test_gtd_expiry_drops_fills_still_scheduled(version):
    book, clock = make_book(version)
    expire = (START + timedelta(milliseconds=700)).strftime(
        "%Y%m%d-%H:%M:%S.%f")[:-3]
    book.on_app_message(order(version, "EFG", tif="6", extra=[(126, expire)]),
                        QUOTE)
    clock.advance(0.5)
    assert [s[1] for s in summary(book.on_timer())] == ["1"]   # first fill
    clock.advance(0.2)
    acts = book.on_timer()
    assert [s[:2] for s in summary(acts)] == [("C", "C")]
    clock.advance(10)
    assert summary(book.on_timer()) == []                      # second fill dropped
    assert [e.event for e in acts if isinstance(e, Evidence)].count(
        "scheduled events dropped") == 1


def test_gtd_with_expire_date_lives_through_that_utc_day():
    book, clock = make_book()
    book.on_app_message(order(FIX_4_2, "ZWZZT", tif="6",
                              extra=[(432, "20261001")]), QUOTE)
    clock.advance(timedelta(hours=9, minutes=59).total_seconds())
    assert summary(book.on_timer()) == []
    clock.advance(60)                                # past 23:59:59.999
    assert [s[:2] for s in summary(book.on_timer())] == [("C", "C")]


@pytest.mark.parametrize("version", VERSIONS)
def test_gtd_without_an_expiry_is_a_session_reject_naming_126(version):
    book, _clock = make_book(version)
    acts = book.on_app_message(order(version, "ZWZZT", tif="6"), QUOTE)
    assert len(acts) == 1 and isinstance(acts[0], SessionReject)
    assert acts[0].ref_tag == 126
    assert acts[0].reason_code == "1"
    assert book.orders() == []


def test_gtd_with_a_malformed_expiry_is_a_data_format_reject():
    book, _clock = make_book()
    acts = book.on_app_message(order(FIX_4_2, "ZWZZT", tif="6",
                                     extra=[(126, "tomorrow")]), QUOTE)
    assert isinstance(acts[0], SessionReject)
    assert (acts[0].ref_tag, acts[0].reason_code) == (126, "6")


# -------------------------------------------- the default: Day only

@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("tif", ["1", "3", "4", "5", "6"])
def test_the_default_config_still_accepts_day_only(version, tif):
    """Nothing changes unless orders.time_in_force says so."""
    clock = FakeClock(START)
    book = OrderBook(OrdersConfig(), load_rules(RULES, DELAY_MS), clock,
                     RUN_ID, profile=profile_for(version))
    acts = book.on_app_message(order(version, "AAPL", tif=tif), QUOTE)
    rows = reports(acts)
    assert [(r[150], r[39], r[58]) for r in rows] == [
        ("8", "8", "TimeInForce not supported")]


def test_a_partial_list_accepts_only_what_it_names():
    book, _clock = make_book(tifs=["day", "ioc"])
    assert summary(book.on_app_message(order(FIX_4_2, "ZWZZT", tif="3"),
                                       QUOTE))[-1][:2] == ("4", "4")
    assert [r[58] for r in reports(book.on_app_message(
        order(FIX_4_2, "ZWZZT", tif="4"), QUOTE))] == ["TimeInForce not supported"]


# --------------------------------------- orders.ioc_fok_ack (Cook 9.1, Q2)

@pytest.mark.parametrize("version", VERSIONS)
def test_without_the_ack_an_ioc_answers_with_its_outcome_first(version):
    book, _clock = make_book(version, ioc_fok_ack=False)
    acts = book.on_app_message(order(version, "EFG", tif="3"), QUOTE)
    assert summary(acts) == [
        (fill_type(version, False), "1", "400", "600", "400", None),
        ("4", "4", "0", "0", "400", "IOC remainder canceled"),
    ]
    assert "ack skipped" in [a.event for a in acts if isinstance(a, Evidence)]


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("symbol,expected", [
    ("AAPL", [("2", "1000")]),           # filled
    ("EFG", [("4", "0")]),               # killed
])
def test_without_the_ack_a_fok_answers_with_its_outcome_first(version, symbol,
                                                               expected):
    book, _clock = make_book(version, ioc_fok_ack=False)
    acts = book.on_app_message(order(version, symbol, tif="4"), QUOTE)
    assert [(s[1], s[4]) for s in summary(acts)] == expected


def test_pending_new_still_comes_first_without_the_ack():
    book, _clock = make_book(FIX_4_4, ioc_fok_ack=False,
                             send_pending_new=True)
    acts = book.on_app_message(order(FIX_4_4, "ZWZZT", tif="3"), QUOTE)
    assert [s[:2] for s in summary(acts)] == [("A", "A"), ("4", "4")]


def test_the_ack_switch_leaves_other_time_in_force_alone():
    book, _clock = make_book(FIX_4_2, ioc_fok_ack=False)
    acts = book.on_app_message(order(FIX_4_2, "ZWZZT", tif="1"), QUOTE)
    assert [s[:2] for s in summary(acts)] == [("0", "0")]


def test_ioc_fok_ack_defaults_on_and_loads_from_yaml(tmp_path):
    from orderecho_Config import load_config
    assert OrdersConfig().ioc_fok_ack is True
    import yaml
    base = yaml.safe_load(open("config/orderecho.yaml"))
    base.setdefault("orders", {})["ioc_fok_ack"] = False
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(base))
    assert load_config(str(path)).orders.ioc_fok_ack is False
    base["orders"]["ioc_fok_ack"] = "nope"
    path.write_text(yaml.safe_dump(base))
    with pytest.raises(Exception):
        load_config(str(path))
