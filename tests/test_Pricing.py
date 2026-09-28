"""Price sources.  No test here touches the network."""

import asyncio
import sys
import types
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from orderecho_Clock import FakeClock
from orderecho_Pricing import (
    WARMUP_SYMBOL,
    SOURCE_LIVE_YFINANCE,
    SOURCE_STATIC_CONFIG,
    SOURCE_STATIC_FALLBACK,
    PriceQuote,
    StaticPriceSource,
    YFinancePriceSource,
    resolve_quote,
    warm_up,
)

START = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)


class FakeLog:
    def __init__(self):
        self.warnings = []
        self.infos = []
        self.debugs = []

    def warning(self, message):
        self.warnings.append(message)

    def info(self, message):
        self.infos.append(message)

    def debug(self, message):
        self.debugs.append(message)


def install_fake_yfinance(monkeypatch, last_price, calls=None, raises=None):
    """Put a fake `yfinance` module in sys.modules for the lazy import."""
    module = types.ModuleType("yfinance")

    class FastInfo(dict):
        pass

    class Ticker:
        def __init__(self, symbol):
            if calls is not None:
                calls.append(symbol)
            if raises is not None:
                raise raises
            self.symbol = symbol

        @property
        def fast_info(self):
            return {"last_price": last_price}

    module.Ticker = Ticker
    monkeypatch.setitem(sys.modules, "yfinance", module)
    return module


def make_live(monkeypatch, last_price, calls=None, raises=None, cache_seconds=300):
    install_fake_yfinance(monkeypatch, last_price, calls, raises)
    clock = FakeClock(START)
    log = FakeLog()
    static = StaticPriceSource({"AAPL": "227.50"}, "100.00")
    source = YFinancePriceSource(static, clock, cache_seconds=cache_seconds,
                                 engine_log=log)
    return source, clock, log


# --- static ----------------------------------------------------------------

def test_static_lookup_and_default():
    source = StaticPriceSource({"AAPL": "227.50", "MSFT": 400}, "100.00")
    apple = source.get("AAPL")
    assert apple == PriceQuote(Decimal("227.50"), SOURCE_STATIC_CONFIG)
    assert source.get("MSFT").price == Decimal("400.00")
    assert source.get("UNLISTED").price == Decimal("100.00")
    assert source.get("UNLISTED").source == SOURCE_STATIC_CONFIG


def test_static_default_when_no_default_configured():
    source = StaticPriceSource({}, None)
    assert source.get("ANY").price == Decimal("100.00")


def test_static_prices_are_quantized_to_cents():
    source = StaticPriceSource({"ODD": "1.239"}, "100")
    assert str(source.get("ODD").price) == "1.24"


def test_static_source_ignores_unusable_entries():
    source = StaticPriceSource({"BAD": "not-a-price", "ZERO": 0, "OK": 5}, "100")
    assert source.get("BAD").price == Decimal("100.00")
    assert source.get("ZERO").price == Decimal("100.00")
    assert source.get("OK").price == Decimal("5.00")


# --- live ------------------------------------------------------------------

def test_yfinance_success(monkeypatch):
    source, _clock, log = make_live(monkeypatch, 231.129)
    quote = source.get("AAPL")
    assert quote.source == SOURCE_LIVE_YFINANCE
    assert quote.price == Decimal("231.13")        # rounded to 2dp
    assert log.warnings == []


def test_yfinance_result_is_cached(monkeypatch):
    calls = []
    source, clock, _log = make_live(monkeypatch, 231.00, calls=calls)

    assert source.get("AAPL").price == Decimal("231.00")
    assert source.get("AAPL").price == Decimal("231.00")
    assert calls == ["AAPL"]                        # only one real lookup

    clock.advance(299)
    source.get("AAPL")
    assert calls == ["AAPL"]

    clock.advance(2)                                # past cache_seconds
    source.get("AAPL")
    assert calls == ["AAPL", "AAPL"]


def test_cache_is_per_symbol(monkeypatch):
    calls = []
    source, _clock, _log = make_live(monkeypatch, 10.00, calls=calls)
    source.get("AAA")
    source.get("BBB")
    source.get("AAA")
    assert calls == ["AAA", "BBB"]


@pytest.mark.parametrize("bad_price", [None, 0, -1, "nonsense", float("nan")])
def test_unusable_price_falls_back(monkeypatch, bad_price):
    source, _clock, log = make_live(monkeypatch, bad_price)
    quote = source.get("AAPL")
    assert quote.source == SOURCE_STATIC_FALLBACK
    assert quote.price == Decimal("227.50")         # from the static table
    assert len(log.warnings) == 1
    assert "AAPL" in log.warnings[0]


def test_exception_falls_back_and_never_raises(monkeypatch):
    source, _clock, log = make_live(monkeypatch, 100, raises=RuntimeError("boom"))
    quote = source.get("UNLISTED")
    assert quote.source == SOURCE_STATIC_FALLBACK
    assert quote.price == Decimal("100.00")         # the static default
    assert "RuntimeError" in log.warnings[0]
    assert "boom" in log.warnings[0]


def test_missing_yfinance_falls_back(monkeypatch):
    monkeypatch.setitem(sys.modules, "yfinance", None)
    clock = FakeClock(START)
    log = FakeLog()
    source = YFinancePriceSource(StaticPriceSource({"AAPL": "227.50"}, "100"),
                                 clock, engine_log=log)
    quote = source.get("AAPL")
    assert quote.source == SOURCE_STATIC_FALLBACK
    assert log.warnings


def test_failures_are_not_cached(monkeypatch):
    calls = []
    source, _clock, _log = make_live(monkeypatch, None, calls=calls)
    source.get("AAPL")
    source.get("AAPL")
    assert calls == ["AAPL", "AAPL"]    # a fallback must not pin the price


# --- resolve_quote ---------------------------------------------------------

async def test_resolve_quote_with_a_static_source():
    source = StaticPriceSource({"AAPL": "227.50"}, "100")
    quote = await resolve_quote(source, "AAPL", 3)
    assert quote == PriceQuote(Decimal("227.50"), SOURCE_STATIC_CONFIG)


async def test_resolve_quote_runs_a_live_source_off_the_event_loop(monkeypatch):
    source, _clock, _log = make_live(monkeypatch, 50.00)
    quote = await resolve_quote(source, "AAPL", 3)
    assert quote.source == SOURCE_LIVE_YFINANCE
    assert quote.price == Decimal("50.00")


async def test_resolve_quote_times_out_into_the_fallback(monkeypatch):
    source, _clock, log = make_live(monkeypatch, 50.00)

    def slow(symbol):
        import time
        time.sleep(1.0)
        return PriceQuote(Decimal("1.00"), SOURCE_LIVE_YFINANCE)

    monkeypatch.setattr(source, "get", slow)
    quote = await resolve_quote(source, "AAPL", 0.05)
    assert quote.source == SOURCE_STATIC_FALLBACK
    assert quote.price == Decimal("227.50")
    assert "timed out" in log.warnings[0]


async def test_timers_keep_running_during_a_slow_lookup(monkeypatch):
    """The lookup must not block the event loop."""
    source, _clock, _log = make_live(monkeypatch, 50.00)
    ticks = []

    def slow(symbol):
        import time
        time.sleep(0.3)
        return PriceQuote(Decimal("7.00"), SOURCE_LIVE_YFINANCE)

    monkeypatch.setattr(source, "get", slow)

    async def ticker():
        while True:
            await asyncio.sleep(0.05)
            ticks.append(1)

    task = asyncio.create_task(ticker())
    quote = await resolve_quote(source, "AAPL", 3)
    task.cancel()

    assert quote.price == Decimal("7.00")
    assert len(ticks) >= 3


# --- warm-up (spec 3.1) ----------------------------------------------------

def test_warm_up_returns_immediately_and_runs_in_the_background(monkeypatch):
    """Startup must never wait on the network."""
    import threading
    import time

    source, _clock, log = make_live(monkeypatch, 100.0)
    started = threading.Event()
    asked = []

    def slow(symbol):
        asked.append(symbol)
        started.set()
        time.sleep(0.4)
        return PriceQuote(Decimal("1.00"), SOURCE_LIVE_YFINANCE)

    monkeypatch.setattr(source, "get", slow)

    begin = time.monotonic()
    thread = warm_up(source, engine_log=log)
    elapsed = time.monotonic() - begin

    assert elapsed < 0.2                 # did not block on the slow lookup
    assert started.wait(3.0)             # but really did run
    assert thread.daemon                 # and will never hold up an exit
    thread.join(3.0)
    assert asked == [WARMUP_SYMBOL] == ["SPY"]
    assert any("SPY" in line for line in log.infos)


def test_warm_up_is_a_no_op_in_static_mode():
    source = StaticPriceSource({"AAPL": "227.50"}, "100")
    assert warm_up(source) is None


def test_warm_up_never_raises(monkeypatch):
    source, _clock, log = make_live(monkeypatch, 100.0)

    def explode(symbol):
        raise RuntimeError("no network at all")

    monkeypatch.setattr(source, "get", explode)
    thread = warm_up(source, engine_log=log)
    thread.join(3.0)
    assert any("warm-up" in line and "no network" in line
               for line in log.warnings)


def test_warm_up_tolerates_having_no_logger(monkeypatch):
    source, _clock, _log = make_live(monkeypatch, 100.0)
    thread = warm_up(source)
    thread.join(3.0)
    assert not thread.is_alive()


# --- keeping yfinance quiet (spec 3.3, test 10.3) --------------------------

def make_noisy_yfinance(monkeypatch, last_price=123.45):
    """A fake yfinance that is chatty in both of the ways the real one is."""
    import logging as _logging

    module = types.ModuleType("yfinance")

    class Ticker:
        def __init__(self, symbol):
            _logging.getLogger("yfinance").warning(
                "Cookie fetch from fc.yahoo.com failed (ConnectionError), "
                "continuing without it"
            )
            print("yfinance chatter on stdout")
            self.symbol = symbol

        @property
        def fast_info(self):
            return {"last_price": last_price}

    module.Ticker = Ticker
    monkeypatch.setitem(sys.modules, "yfinance", module)
    return module


def test_yfinance_noise_never_reaches_the_terminal(monkeypatch, capsys):
    from orderecho_Pricing import quiet_third_party_logging

    log = FakeLog()
    quiet_third_party_logging(log)
    make_noisy_yfinance(monkeypatch)

    source = YFinancePriceSource(
        StaticPriceSource({}, "100"), FakeClock(START), engine_log=log
    )
    quote = source.get("AAPL")
    captured = capsys.readouterr()

    assert quote.price == Decimal("123.45")
    assert quote.source == SOURCE_LIVE_YFINANCE

    # Nothing of yfinance's reached the console, on either stream.
    assert "Cookie fetch" not in captured.out
    assert "Cookie fetch" not in captured.err
    assert "chatter on stdout" not in captured.out
    assert "chatter on stdout" not in captured.err

    # Both kinds of noise were kept, at DEBUG, in the engine log.
    assert any("Cookie fetch" in line for line in log.debugs)
    assert any("chatter on stdout" in line for line in log.debugs)
    assert log.warnings == []


def test_muting_one_thread_does_not_silence_the_others(capsys):
    """The whole point of not using redirect_stdout globally."""
    import threading

    from orderecho_Pricing import muted_stdout

    swallowed = []
    inside = threading.Event()
    release = threading.Event()

    def muted_worker():
        with muted_stdout(swallowed.append):
            inside.set()
            print("this must not be printed")
            release.wait(5.0)

    worker = threading.Thread(target=muted_worker)
    worker.start()
    assert inside.wait(5.0)
    print("this must still be printed")      # main thread, not muted
    release.set()
    worker.join(5.0)

    captured = capsys.readouterr()
    assert "this must still be printed" in captured.out
    assert "this must not be printed" not in captured.out
    assert swallowed == ["this must not be printed"]


def test_stdout_is_restored_after_muting(capsys):
    from orderecho_Pricing import muted_stdout

    before = sys.stdout
    with muted_stdout():
        pass
    assert sys.stdout is before
