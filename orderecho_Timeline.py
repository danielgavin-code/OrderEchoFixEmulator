"""Order chains and their consistency checks (PURE).

Given a pile of parsed messages and one identifier, this reconstructs the life
of an order -- new, replaced, replaced again, cancelled, rejected -- and then
asks eleven questions of it.

The checks are the point. They are the specification for the assertions a
certification client will make later, so each one is a single function with a
docstring stating the rule it enforces, and none of them ever throws: a check
that cannot be evaluated says so and passes rather than taking the tool down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from orderecho_Summary import flags_for

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

_RANK = {PASS: 0, WARN: 1, FAIL: 2}

TAG_AVG_PX = 6
TAG_CL_ORD_ID = 11
TAG_CUM_QTY = 14
TAG_EXEC_ID = 17
TAG_EXEC_TRANS_TYPE = 20
TAG_LAST_PX = 31
TAG_LAST_QTY = 32
TAG_ORDER_ID = 37
TAG_ORDER_QTY = 38
TAG_ORD_STATUS = 39
TAG_ORD_TYPE = 40
TAG_ORIG_CL_ORD_ID = 41
TAG_PRICE = 44
TAG_REF_SEQ_NUM = 45
TAG_SIDE = 54
TAG_SYMBOL = 55
TAG_TEXT = 58
TAG_EXEC_TYPE = 150
TAG_LEAVES_QTY = 151

MSG_EXECUTION_REPORT = "8"
MSG_CANCEL_REJECT = "9"
MSG_REJECT = "3"
MSG_BUSINESS_REJECT = "j"
REQUEST_TYPES = ("D", "F", "G")
RESPONSE_TYPES = (MSG_EXECUTION_REPORT, MSG_CANCEL_REJECT, MSG_REJECT,
                  MSG_BUSINESS_REJECT)

#: OrdStatus values where the order is still alive.
WORKING_STATES = frozenset({"0", "1", "6", "E", "A"})
#: OrdStatus values where it is over.
TERMINAL_STATES = frozenset({"2", "4", "8", "C", "3"})
#: ExecType values that report a fill.
FILL_EXEC_TYPES = frozenset({"1", "2", "F"})

#: Placeholder OrderID a cancel reject uses when it knows of no order.
NO_ORDER_ID = {"NONE", "", "0", "UNKNOWN"}

EPSILON = Decimal("0.0001")


def _decimal(value):
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def _int(value):
    number = _decimal(value)
    if number is None:
        return None
    try:
        return int(number)
    except (ValueError, OverflowError):
        return None


@dataclass
class CheckResult:
    name: str
    status: str
    explanation: str
    rule: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status,
                "explanation": self.explanation, "rule": self.rule}


@dataclass
class Step:
    """One line of the timeline, already named and formatted."""

    message: object
    replay: bool = False

    def as_dict(self, dictionary=None) -> dict:
        msg = self.message
        version = msg.begin_string

        def named(tag):
            value = msg.get(tag)
            if value is None or dictionary is None:
                return value
            name = dictionary.enum_name(tag, value, version)
            return f"{value} ({name})" if name else value

        last_qty = msg.get(TAG_LAST_QTY)
        last_px = msg.get(TAG_LAST_PX)
        last = (f"{last_qty}@{last_px}"
                if last_qty and last_qty not in ("0", "0.00") else None)
        return {
            "ts": msg.ts.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z") if msg.ts else None,
            "direction": msg.direction,
            "session": msg.session,
            "seq": msg.seq,
            "msg_type": msg.msg_type,
            "msg_type_name": (dictionary.enum_name(35, msg.msg_type, version)
                              if dictionary else None),
            "exec_type": named(TAG_EXEC_TYPE),
            "ord_status": named(TAG_ORD_STATUS),
            "order_qty": msg.get(TAG_ORDER_QTY),
            "last": last,
            "cum_qty": msg.get(TAG_CUM_QTY),
            "leaves_qty": msg.get(TAG_LEAVES_QTY),
            "avg_px": msg.get(TAG_AVG_PX),
            "cl_ord_id": msg.get(TAG_CL_ORD_ID),
            "orig_cl_ord_id": msg.get(TAG_ORIG_CL_ORD_ID),
            "order_id": msg.get(TAG_ORDER_ID),
            "text": msg.get(TAG_TEXT),
            "replay": self.replay,
            "injected": msg.injected,
            "suspect": msg.suspect,
            "bad_checksum": msg.bad_checksum,
            "bad_length": msg.bad_length,
            "poss_dup": msg.poss_dup,
            "flags": flags_for(msg),
            "index": msg.index,
        }


@dataclass
class Chain:
    """Every message that belongs to one order."""

    seed: str
    steps: list = field(default_factory=list)
    ids: set = field(default_factory=set)
    order_ids: set = field(default_factory=set)
    checks: list = field(default_factory=list)

    @property
    def messages(self) -> list:
        return [step.message for step in self.steps]

    @property
    def reports(self) -> list:
        """ExecutionReports, ignoring replays (spec check 8)."""
        return [step.message for step in self.steps
                if step.message.msg_type == MSG_EXECUTION_REPORT
                and not step.replay]

    @property
    def verdict(self) -> str:
        if not self.checks:
            return PASS
        return max((check.status for check in self.checks),
                   key=lambda status: _RANK.get(status, 0))

    @property
    def exit_code(self) -> int:
        return _RANK.get(self.verdict, 0)

    def as_dict(self, dictionary=None) -> dict:
        return {
            "seed": self.seed,
            "cl_ord_ids": sorted(self.ids),
            "order_ids": sorted(self.order_ids),
            "steps": [step.as_dict(dictionary) for step in self.steps],
            "checks": [check.as_dict() for check in self.checks],
            "verdict": self.verdict,
        }


# ------------------------------------------------------------ chain building


def _identity_ids(message) -> set:
    ids = set()
    for tag in (TAG_CL_ORD_ID, TAG_ORIG_CL_ORD_ID):
        value = message.get(tag)
        if value:
            ids.add(value)
    return ids


def _order_id_of(message):
    value = message.get(TAG_ORDER_ID)
    if value and value.upper() not in NO_ORDER_ID:
        return value
    return None


def build_chain(messages, cl_ord_id: str | None = None,
                order_id: str | None = None) -> Chain:
    """Collect every message belonging to one order.

    Starts from a ClOrdID or an OrderID and follows both links a FIX order
    chain has: `41` pointing back to the ClOrdID it replaces, and a shared
    `37`.  Following both directions means any id in the chain finds all of
    it, which is what someone reading a log actually has.
    """
    messages = list(messages)
    wanted_ids = {cl_ord_id} if cl_ord_id else set()
    wanted_orders = {order_id} if order_id else set()

    chosen: dict = {}
    changed = True
    while changed:
        changed = False
        for index, message in enumerate(messages):
            if index in chosen:
                continue
            ids = _identity_ids(message)
            own_order = _order_id_of(message)
            if (ids & wanted_ids) or (own_order and own_order in wanted_orders):
                chosen[index] = message
                if ids - wanted_ids:
                    wanted_ids |= ids
                    changed = True
                if own_order and own_order not in wanted_orders:
                    wanted_orders.add(own_order)
                    changed = True

    _add_session_rejects(messages, chosen)

    ordered = [chosen[index] for index in sorted(chosen)]
    steps = _mark_replays(ordered)
    chain = Chain(
        seed=cl_ord_id or order_id or "",
        steps=steps,
        ids=wanted_ids,
        order_ids=wanted_orders,
    )
    chain.checks = run_checks(chain)
    return chain


def _add_session_rejects(messages, chosen: dict) -> None:
    """Pull in Rejects that answer a request already in the chain.

    A 35=3 or 35=j carries no ClOrdID, so nothing links it to an order except
    the sequence number it refers to -- but it is a perfectly good answer to a
    request, and check 10 has to be able to see it.
    """
    wanted = {}
    for message in chosen.values():
        if message.msg_type in REQUEST_TYPES and message.seq is not None:
            wanted.setdefault((message.session, message.seq), message)
    if not wanted:
        return
    for index, message in enumerate(messages):
        if index in chosen:
            continue
        if message.msg_type not in (MSG_REJECT, MSG_BUSINESS_REJECT):
            continue
        ref_seq = _int(message.get(TAG_REF_SEQ_NUM))
        if ref_seq is None:
            continue
        if (message.session, ref_seq) in wanted or \
                (None, ref_seq) in wanted:
            chosen[index] = message


def _mark_replays(messages) -> list:
    """A PossDup report of something already seen is a replay, not news."""
    steps = []
    seen_exec_ids: set = set()
    seen_keys: set = set()
    for message in messages:
        replay = False
        exec_id = message.get(TAG_EXEC_ID)
        key = (message.msg_type, message.seq)
        if message.poss_dup:
            if exec_id and exec_id in seen_exec_ids:
                replay = True
            elif not exec_id and key in seen_keys:
                replay = True
        if exec_id:
            seen_exec_ids.add(exec_id)
        seen_keys.add(key)
        steps.append(Step(message=message, replay=replay))
    return steps


# ------------------------------------------------------------------- checks


def check_cum_qty_never_decreases(chain: Chain) -> CheckResult:
    """CumQty (14) may only ever go up across a chain's reports."""
    rule = "CumQty never decreases"
    previous = None
    for message in chain.reports:
        cum = _decimal(message.get(TAG_CUM_QTY))
        if cum is None:
            continue
        if previous is not None and cum < previous:
            return CheckResult(
                "cum_qty_monotonic", FAIL,
                f"CumQty went backwards, {previous} -> {cum}, at "
                f"{_where(message)}", rule)
        previous = cum
    if previous is None:
        return CheckResult("cum_qty_monotonic", PASS,
                           "no reports carried a CumQty", rule)
    return CheckResult("cum_qty_monotonic", PASS,
                       f"CumQty rose to {previous} without ever falling", rule)


def check_working_quantities(chain: Chain) -> CheckResult:
    """While an order is working, CumQty + LeavesQty == OrderQty."""
    rule = "working states: CumQty + LeavesQty == OrderQty"
    checked = 0
    for message in chain.reports:
        status = message.get(TAG_ORD_STATUS)
        if status not in WORKING_STATES:
            continue
        cum = _decimal(message.get(TAG_CUM_QTY))
        leaves = _decimal(message.get(TAG_LEAVES_QTY))
        order_qty = _decimal(message.get(TAG_ORDER_QTY))
        if None in (cum, leaves, order_qty):
            continue
        checked += 1
        if cum + leaves != order_qty:
            return CheckResult(
                "working_quantities", FAIL,
                f"39={status}: CumQty {cum} + LeavesQty {leaves} = "
                f"{cum + leaves}, but OrderQty is {order_qty}, at "
                f"{_where(message)}", rule)
    return CheckResult(
        "working_quantities", PASS,
        f"{checked} working report(s) balanced" if checked
        else "no working reports to check", rule)


def check_terminal_quantities(chain: Chain) -> CheckResult:
    """A terminal report leaves nothing open; a fill has filled it all."""
    rule = "terminal states: LeavesQty == 0, and 39=2 implies CumQty == OrderQty"
    checked = 0
    for message in chain.reports:
        status = message.get(TAG_ORD_STATUS)
        if status not in TERMINAL_STATES:
            continue
        leaves = _decimal(message.get(TAG_LEAVES_QTY))
        if leaves is None:
            continue
        checked += 1
        if leaves != 0:
            return CheckResult(
                "terminal_quantities", FAIL,
                f"39={status} is terminal but LeavesQty is {leaves}, at "
                f"{_where(message)}", rule)
        if status == "2":
            cum = _decimal(message.get(TAG_CUM_QTY))
            order_qty = _decimal(message.get(TAG_ORDER_QTY))
            if None not in (cum, order_qty) and cum != order_qty:
                return CheckResult(
                    "terminal_quantities", FAIL,
                    f"39=2 (Filled) with CumQty {cum} but OrderQty "
                    f"{order_qty}, at {_where(message)}", rule)
    return CheckResult(
        "terminal_quantities", PASS,
        f"{checked} terminal report(s) consistent" if checked
        else "no terminal reports to check", rule)


def check_fill_quantities_sum(chain: Chain) -> CheckResult:
    """The fills add up to the final CumQty."""
    rule = "sum of LastQty over fills == final CumQty"
    total = Decimal(0)
    fills = 0
    final_cum = None
    for message in chain.reports:
        if message.get(TAG_EXEC_TYPE) in FILL_EXEC_TYPES:
            last = _decimal(message.get(TAG_LAST_QTY))
            if last is not None:
                total += last
                fills += 1
        cum = _decimal(message.get(TAG_CUM_QTY))
        if cum is not None:
            final_cum = cum
    if not fills:
        return CheckResult("fill_quantities_sum", PASS,
                           "no fills in this chain", rule)
    if final_cum is None:
        return CheckResult("fill_quantities_sum", PASS,
                           "no CumQty to compare the fills against", rule)
    if total != final_cum:
        return CheckResult(
            "fill_quantities_sum", FAIL,
            f"{fills} fill(s) totalling {total} but the last CumQty is "
            f"{final_cum}", rule)
    return CheckResult("fill_quantities_sum", PASS,
                       f"{fills} fill(s) totalling {total} match CumQty", rule)


def check_avg_px(chain: Chain) -> CheckResult:
    """AvgPx is the quantity-weighted mean of the fills, to 0.0001."""
    rule = "AvgPx == sum(LastQty x LastPx) / CumQty, within 0.0001"
    notional = Decimal(0)
    quantity = Decimal(0)
    last_report = None
    for message in chain.reports:
        if message.get(TAG_EXEC_TYPE) in FILL_EXEC_TYPES:
            last_qty = _decimal(message.get(TAG_LAST_QTY))
            last_px = _decimal(message.get(TAG_LAST_PX))
            if last_qty is None or last_px is None:
                continue
            notional += last_qty * last_px
            quantity += last_qty
            last_report = message
    if quantity == 0 or last_report is None:
        return CheckResult("avg_px", PASS, "no fills to average", rule)
    stated = _decimal(last_report.get(TAG_AVG_PX))
    if stated is None:
        return CheckResult("avg_px", PASS, "no AvgPx reported", rule)
    expected = notional / quantity
    if abs(expected - stated) > EPSILON:
        return CheckResult(
            "avg_px", FAIL,
            f"AvgPx reported {stated} but the fills average "
            f"{expected.quantize(Decimal('0.000001'))}, at "
            f"{_where(last_report)}", rule)
    return CheckResult(
        "avg_px", PASS,
        f"AvgPx {stated} matches the fills to within {EPSILON}", rule)


def check_exec_ids_unique(chain: Chain) -> CheckResult:
    """Every ExecutionReport in a chain has its own ExecID."""
    rule = "ExecIDs are unique within the chain"
    seen: dict = {}
    for message in chain.reports:
        exec_id = message.get(TAG_EXEC_ID)
        if not exec_id:
            continue
        if exec_id in seen:
            return CheckResult(
                "exec_ids_unique", FAIL,
                f"ExecID {exec_id} used twice, at {_where(seen[exec_id])} and "
                f"{_where(message)}", rule)
        seen[exec_id] = message
    return CheckResult("exec_ids_unique", PASS,
                       f"{len(seen)} ExecID(s), all distinct", rule)


def check_order_id_constant(chain: Chain) -> CheckResult:
    """One order keeps one OrderID, however often its ClOrdID changes."""
    rule = "OrderID is constant across the chain"
    found = []
    for message in chain.messages:
        order_id = _order_id_of(message)
        if order_id and order_id not in found:
            found.append(order_id)
    if len(found) > 1:
        return CheckResult(
            "order_id_constant", FAIL,
            f"the chain carries {len(found)} OrderIDs: {', '.join(found)}",
            rule)
    return CheckResult(
        "order_id_constant", PASS,
        f"OrderID {found[0]} throughout" if found
        else "no OrderID on any message", rule)


def check_nothing_after_terminal(chain: Chain) -> CheckResult:
    """Once an order is done, nothing more happens to it.

    A replayed PossDup copy of an earlier report is not a new event, so it is
    shown in the timeline but ignored here.
    """
    rule = "no state change after a terminal report (replays excepted)"
    terminal = None
    for step in chain.steps:
        message = step.message
        if message.msg_type != MSG_EXECUTION_REPORT or step.replay:
            continue
        status = message.get(TAG_ORD_STATUS)
        if terminal is not None:
            return CheckResult(
                "nothing_after_terminal", FAIL,
                f"39={terminal.get(TAG_ORD_STATUS)} at {_where(terminal)} was "
                f"terminal, but another report (39={status}) followed at "
                f"{_where(message)}", rule)
        if status in TERMINAL_STATES:
            terminal = message
    return CheckResult(
        "nothing_after_terminal", PASS,
        f"terminal 39={terminal.get(TAG_ORD_STATUS)} was the last word"
        if terminal is not None else "the order never reached a terminal state",
        rule)


def check_version_rules(chain: Chain) -> CheckResult:
    """4.4 fills are 150=F and carry no tag 20; 4.2 fills are 150=1 or 2."""
    rule = "ExecType and ExecTransType match the message's FIX version"
    for message in chain.reports:
        version = message.begin_string or ""
        exec_type = message.get(TAG_EXEC_TYPE)
        if version == "FIX.4.4":
            if message.get(TAG_EXEC_TRANS_TYPE) is not None:
                return CheckResult(
                    "version_rules", FAIL,
                    f"FIX.4.4 report carries tag 20, which was removed in "
                    f"4.4, at {_where(message)}", rule)
            if exec_type in ("1", "2"):
                return CheckResult(
                    "version_rules", FAIL,
                    f"FIX.4.4 fill uses 150={exec_type}; 4.4 reports trades "
                    f"as 150=F, at {_where(message)}", rule)
        elif version == "FIX.4.2":
            if exec_type == "F":
                return CheckResult(
                    "version_rules", FAIL,
                    f"FIX.4.2 fill uses 150=F, which arrived in 4.4; 4.2 "
                    f"reports 150=1 or 150=2, at {_where(message)}", rule)
    return CheckResult("version_rules", PASS,
                       "every report matches its version's conventions", rule)


def check_requests_answered(chain: Chain) -> CheckResult:
    """Every order request in the chain got some answer."""
    rule = "each D/F/G is answered by an ER, a cancel reject or a Reject"
    requests = [message for message in chain.messages
                if message.msg_type in REQUEST_TYPES]
    if not requests:
        return CheckResult("requests_answered", PASS,
                           "no order requests in this chain", rule)

    answered_ids = set()
    referenced_seqs = set()
    for message in chain.messages:
        if message.msg_type not in RESPONSE_TYPES:
            continue
        for tag in (TAG_CL_ORD_ID, TAG_ORIG_CL_ORD_ID):
            value = message.get(tag)
            if value:
                answered_ids.add(value)
        ref_seq = _int(message.get(TAG_REF_SEQ_NUM))
        if ref_seq is not None:
            referenced_seqs.add(ref_seq)

    unanswered = []
    for request in requests:
        cl_ord_id = request.get(TAG_CL_ORD_ID)
        if cl_ord_id and cl_ord_id in answered_ids:
            continue
        if request.seq is not None and request.seq in referenced_seqs:
            continue
        unanswered.append(request)

    if unanswered:
        listed = ", ".join(
            f"35={message.msg_type} seq={message.seq} "
            f"11={message.get(TAG_CL_ORD_ID)}" for message in unanswered
        )
        return CheckResult(
            "requests_answered", WARN,
            f"{len(unanswered)} request(s) with no response in this log: "
            f"{listed}", rule)
    return CheckResult("requests_answered", PASS,
                       f"all {len(requests)} request(s) answered", rule)


def check_framing_intact(chain: Chain) -> CheckResult:
    """No message in the chain arrived with a broken CheckSum or BodyLength."""
    rule = "every message in the chain is correctly framed (9 and 10 agree)"
    broken = [message for message in chain.messages
              if message.bad_checksum or message.bad_length]
    if not broken:
        return CheckResult("framing_intact", PASS,
                           f"all {len(chain.messages)} message(s) correctly "
                           f"framed", rule)
    listed = ", ".join(
        f"{'bad CheckSum' if message.bad_checksum else 'bad BodyLength'} "
        f"at {_where(message)}" for message in broken)
    return CheckResult(
        "framing_intact", WARN,
        f"{len(broken)} message(s) with broken framing, so what they say "
        f"cannot be trusted: {listed}", rule)


#: One function per rule, in the order the spec lists them.
CHECKS = (
    check_cum_qty_never_decreases,
    check_working_quantities,
    check_terminal_quantities,
    check_fill_quantities_sum,
    check_avg_px,
    check_exec_ids_unique,
    check_order_id_constant,
    check_nothing_after_terminal,
    check_version_rules,
    check_requests_answered,
    check_framing_intact,
)


def run_checks(chain: Chain) -> list:
    """Every check, never raising: a check that blows up is itself a FAIL."""
    results = []
    for check in CHECKS:
        try:
            results.append(check(chain))
        except Exception as exc:  # pragma: no cover - defensive
            results.append(CheckResult(
                getattr(check, "__name__", "check"), FAIL,
                f"the check itself failed: {type(exc).__name__}: {exc}"))
    return results


def _where(message) -> str:
    """Point at a message in a way a human can find in the file."""
    bits = []
    if message.seq is not None:
        bits.append(f"seq={message.seq}")
    exec_id = message.get(TAG_EXEC_ID)
    if exec_id:
        bits.append(f"17={exec_id}")
    if message.source and message.source_line_no:
        bits.append(f"{message.source}:{message.source_line_no}")
    return " ".join(bits) or f"message #{message.index}"
