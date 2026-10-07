"""Price sources.

A quote is an *input* to the pure order book, never something the order book
fetches for itself: it is resolved once, at order arrival, and recorded in the
evidence log, so a run can be reproduced exactly from what was captured.

Nothing here ever raises.  A live lookup that fails for any reason falls back
to the static table and says so in the quote's source, because an emulator
that stops taking orders because the internet blinked is useless.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

SOURCE_STATIC_CONFIG = "static:config"
SOURCE_STATIC_FALLBACK = "static:fallback"
SOURCE_LIVE_YFINANCE = "live:yfinance"
SOURCE_LIMIT = "limit"

CENTS = Decimal("0.01")


@dataclass(frozen=True)
class PriceQuote:
    price: Decimal
    source: str

    def __str__(self) -> str:
        return f"{self.price} ({self.source})"


def to_price(value) -> Decimal | None:
    """Coerce to a 2dp Decimal, or None if it is not a usable price."""
    if value is None or isinstance(value, bool):
        return None
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not price.is_finite() or price <= 0:
        return None
    return price.quantize(CENTS)


class StaticPriceSource:
    """Prices from the config table, with a default for unlisted symbols."""

    def __init__(self, static_prices: dict | None = None,
                 default: Decimal | None = None) -> None:
        self.static_prices = {}
        for symbol, value in (static_prices or {}).items():
            price = to_price(value)
            if price is not None:
                self.static_prices[str(symbol)] = price
        self.default = to_price(default) or Decimal("100.00")

    def static_price(self, symbol: str) -> Decimal:
        return self.static_prices.get(symbol, self.default)

    def get(self, symbol: str) -> PriceQuote:
        return PriceQuote(self.static_price(symbol), SOURCE_STATIC_CONFIG)

    def fallback(self, symbol: str) -> PriceQuote:
        return PriceQuote(self.static_price(symbol), SOURCE_STATIC_FALLBACK)


class YFinancePriceSource:
    """Live prices from yfinance, cached, falling back to the static table.

    yfinance is imported lazily so that `mode: static` works on a machine that
    does not have it installed.
    """

    def __init__(self, static_source: StaticPriceSource, clock,
                 cache_seconds: float = 300, engine_log=None,
                 negative_cache_seconds: float = 0) -> None:
        # 0 = no negative cache.  The engine passes the config's value
        # (pricing.negative_cache_seconds, default 300).
        self.static_source = static_source
        self.clock = clock
        self.cache_seconds = float(cache_seconds)
        self.negative_cache_seconds = float(negative_cache_seconds)
        self.engine_log = engine_log
        self._cache: dict[str, tuple] = {}
        #: Symbols whose live lookup failed, with the fallback quote we gave
        #: and when: for negative_cache_seconds the next order is answered at
        #: once instead of waiting for the same failure again.
        self._negative: dict[str, tuple] = {}

    def static_price(self, symbol: str) -> Decimal:
        return self.static_source.static_price(symbol)

    def fallback(self, symbol: str, reason: str = "") -> PriceQuote:
        self._warn(symbol, reason)
        quote = self.static_source.fallback(symbol)
        if self.negative_cache_seconds > 0:
            self._negative[symbol] = (quote, self.clock.now())
        return quote

    def _negatively_cached(self, symbol: str) -> PriceQuote | None:
        entry = self._negative.get(symbol)
        if entry is None:
            return None
        quote, stored_at = entry
        if (self.clock.now() - stored_at).total_seconds() >= \
                self.negative_cache_seconds:
            del self._negative[symbol]
            return None
        return quote

    def peek(self, symbol: str) -> PriceQuote | None:
        """A cached answer, good or negative, without any lookup."""
        return self._cached(symbol) or self._negatively_cached(symbol)

    def _warn(self, symbol: str, reason: str) -> None:
        if self.engine_log is not None and reason:
            try:
                self.engine_log.warning(
                    f"Price lookup for {symbol} failed ({reason}); "
                    f"falling back to {self.static_source.static_price(symbol)} "
                    f"[{SOURCE_STATIC_FALLBACK}]"
                )
            except Exception:  # logging must never break pricing
                pass

    def _cached(self, symbol: str) -> PriceQuote | None:
        entry = self._cache.get(symbol)
        if entry is None:
            return None
        quote, stored_at = entry
        if (self.clock.now() - stored_at).total_seconds() >= self.cache_seconds:
            del self._cache[symbol]
            return None
        return quote

    def get(self, symbol: str) -> PriceQuote:
        """Blocking lookup.  Never raises; always returns a usable quote."""
        cached = self.peek(symbol)
        if cached is not None:
            return cached

        try:
            import yfinance  # imported lazily, and only in live mode
        except Exception as exc:
            return self.fallback(symbol, f"yfinance unavailable: {exc}")

        def note(text: str) -> None:
            if self.engine_log is not None:
                try:
                    self.engine_log.debug(f"yfinance stdout: {text}")
                except Exception:
                    pass

        try:
            with muted_stdout(note):
                ticker = yfinance.Ticker(symbol)
                raw = ticker.fast_info["last_price"]
        except Exception as exc:
            return self.fallback(symbol, f"{type(exc).__name__}: {exc}")

        price = to_price(raw)
        if price is None:
            return self.fallback(symbol, f"unusable price {raw!r}")

        quote = PriceQuote(price, SOURCE_LIVE_YFINANCE)
        self._cache[symbol] = (quote, self.clock.now())
        self._negative.pop(symbol, None)
        return quote


def build_price_source(config, clock, engine_log=None):
    """Create the price source named by the config's pricing section."""
    pricing = config.pricing
    static_source = StaticPriceSource(pricing.static, pricing.static_default)
    if pricing.mode == "static":
        return static_source
    return YFinancePriceSource(
        static_source,
        clock,
        cache_seconds=pricing.cache_seconds,
        engine_log=engine_log,
        negative_cache_seconds=getattr(pricing, "negative_cache_seconds", 300),
    )


# ------------------------------------------------------------ keeping quiet
#
# yfinance is chatty.  Its "Cookie fetch from fc.yahoo.com failed" line is a
# logging warning on the 'yfinance' logger, which -- having no handler of its
# own -- reaches the terminal through logging's lastResort stderr handler.  We
# give it a handler that forwards into our engine log at DEBUG and stop it
# propagating, so it is recorded but never printed.
#
# Anything it writes with print() is handled separately, by a stdout proxy
# that mutes *only the calling thread*.  A plain contextlib.redirect_stdout
# would silence the whole process for the duration, swallowing the console
# echo other tasks are producing at the same time.

YFINANCE_LOGGERS = ("yfinance", "peewee")


class _EngineLogHandler(logging.Handler):
    """Forwards another library's log records into our engine log at DEBUG."""

    def __init__(self, engine_log, prefix: str) -> None:
        super().__init__(level=logging.DEBUG)
        self.engine_log = engine_log
        self.prefix = prefix

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.engine_log.debug(
                f"{self.prefix} [{record.levelname}] {record.getMessage()}"
            )
        except Exception:
            pass


def quiet_third_party_logging(engine_log, logger_names=YFINANCE_LOGGERS) -> list:
    """Route noisy third-party loggers into the engine log at DEBUG."""
    handlers = []
    for name in logger_names:
        logger = logging.getLogger(name)
        for existing in list(logger.handlers):
            if isinstance(existing, _EngineLogHandler):
                logger.removeHandler(existing)
        handler = _EngineLogHandler(engine_log, name)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False      # never reach root / lastResort / stderr
        handlers.append(handler)
    return handlers


class _ThreadMutedStdout:
    """A stdout stand-in that swallows writes from muted threads only.

    Every other thread's output is passed straight through to the stream this
    wraps, so muting a price lookup never costs us a log line elsewhere.
    """

    def __init__(self, wrapped) -> None:
        self._wrapped = wrapped
        self._local = threading.local()
        self.sink = None
        self.depth = 0

    @property
    def muted(self) -> bool:
        return getattr(self._local, "muted", False)

    def mute(self, muted: bool) -> None:
        self._local.muted = muted

    def write(self, text):
        if self.muted:
            captured = text.strip()
            if captured and self.sink is not None:
                try:
                    self.sink(captured)
                except Exception:
                    pass
            return len(text)
        return self._wrapped.write(text)

    def flush(self):
        if not self.muted:
            return self._wrapped.flush()
        return None

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


_mute_lock = threading.Lock()


@contextlib.contextmanager
def muted_stdout(sink=None):
    """Mute print() from the calling thread for the duration of the block."""
    with _mute_lock:
        proxy = sys.stdout
        installed_here = not isinstance(proxy, _ThreadMutedStdout)
        if installed_here:
            proxy = _ThreadMutedStdout(sys.stdout)
            sys.stdout = proxy
        if sink is not None:
            proxy.sink = sink
        proxy.depth += 1
    proxy.mute(True)
    try:
        yield proxy
    finally:
        proxy.mute(False)
        with _mute_lock:
            proxy.depth -= 1
            if proxy.depth <= 0 and sys.stdout is proxy:
                sys.stdout = proxy._wrapped


#: Warmed up at startup so the first real order does not pay for yfinance's
#: cookie/crumb negotiation.
WARMUP_SYMBOL = "SPY"


def warm_up(source, symbols=None, engine_log=None) -> None:
    """Prime the live price cache in a background thread.

    Fire and forget: results are discarded and any failure is only logged,
    because nothing about startup may depend on the network being reachable.
    Warming the symbols the rules are likely to see is what stops the price
    band from being skipped on the first limit order of a run.
    """
    if isinstance(source, StaticPriceSource):
        return None
    if symbols is None:
        symbols = [WARMUP_SYMBOL]
    elif isinstance(symbols, str):
        symbols = [symbols]
    symbols = [symbol for symbol in symbols if symbol]
    if not symbols:
        return None

    def run():
        for symbol in symbols:
            try:
                quote = source.get(symbol)
                if engine_log is not None:
                    engine_log.info(
                        f"Price warm-up: {symbol} {quote.price} "
                        f"({quote.source})"
                    )
            except Exception as exc:   # the source should not raise anyway
                if engine_log is not None:
                    engine_log.warning(
                        f"Price warm-up for {symbol} failed: "
                        f"{type(exc).__name__}: {exc}"
                    )

    thread = threading.Thread(target=run, name="price-warmup", daemon=True)
    thread.start()
    return thread


async def resolve_quote(source, symbol: str, timeout_sec: float) -> PriceQuote:
    """Resolve a quote without blocking the event loop.

    A static source answers inline; a live one is run in a worker thread and
    given `timeout_sec` to answer before we fall back.  Timers keep ticking
    throughout, and this never raises.
    """
    if isinstance(source, StaticPriceSource):
        return source.get(symbol)
    peek = getattr(source, "peek", None)
    if callable(peek):
        cached = peek(symbol)
        if cached is not None:
            # Answered at once: a good quote, or a recent failure we will
            # not wait for again (pricing.negative_cache_seconds).
            return cached
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(source.get, symbol), timeout_sec
        )
    except asyncio.TimeoutError:
        return source.fallback(symbol, f"timed out after {timeout_sec}s")
    except Exception as exc:  # pragma: no cover - defensive
        return source.fallback(symbol, f"{type(exc).__name__}: {exc}")
