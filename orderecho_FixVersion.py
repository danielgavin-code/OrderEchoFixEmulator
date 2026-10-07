"""FIX version profiles (PURE).

The order book decides *what happened*; a profile decides *how it is written*.
Everything version-specific lives here: BeginString, which tags an inbound
message must carry, which enumeration values exist, how a report renders, and
which reject code stands for a given reason.

The profile is a property of a session, never of the process: the engine runs
several sessions at once, possibly on different versions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

FIX_4_2 = "FIX.4.2"
FIX_4_4 = "FIX.4.4"


class ReportKind(str, Enum):
    """What happened to an order, independent of how FIX spells it."""

    ACK = "ACK"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILL = "FILL"
    CANCELED = "CANCELED"
    REPLACED = "REPLACED"
    PENDING_CANCEL = "PENDING_CANCEL"
    PENDING_REPLACE = "PENDING_REPLACE"
    REJECTED = "REJECTED"
    # Lifecycle states, TIF, persistence
    PENDING_NEW = "PENDING_NEW"
    DONE_FOR_DAY = "DONE_FOR_DAY"
    EXPIRED = "EXPIRED"
    #: The answer to an OrderStatusRequest (35=H): not an event, a snapshot.
    STATUS = "STATUS"


class Reason(str, Enum):
    """Why a message was rejected, independent of the code that carries it."""

    UNSUPPORTED_CHARACTERISTIC = "UNSUPPORTED_CHARACTERISTIC"
    BAD_QUANTITY = "BAD_QUANTITY"
    BAD_PRICE = "BAD_PRICE"
    DUPLICATE_CL_ORD_ID = "DUPLICATE_CL_ORD_ID"
    BAND_BREACH = "BAND_BREACH"
    NO_REFERENCE = "NO_REFERENCE"
    RULE_REJECT = "RULE_REJECT"
    UNKNOWN_ACCOUNT = "UNKNOWN_ACCOUNT"       # : orders.valid_accounts
    # Cancel / replace rejects
    CXL_TOO_LATE = "CXL_TOO_LATE"
    CXL_UNKNOWN_ORDER = "CXL_UNKNOWN_ORDER"
    CXL_BROKER_OPTION = "CXL_BROKER_OPTION"


class ResponseTo(str, Enum):
    CANCEL = "CANCEL"
    REPLACE = "REPLACE"


# --------------------------------------------------------------- neutral IO


@dataclass
class ExecReport:
    """A version-neutral ExecutionReport."""

    kind: ReportKind
    order_id: str
    cl_ord_id: str
    exec_id: str
    symbol: str
    side: str
    order_qty: str
    ord_type: str
    ord_status: str
    leaves_qty: str
    cum_qty: str
    avg_px: str
    transact_time: str
    last_qty: str = "0"
    last_px: str = "0.00"
    price: str | None = None
    orig_cl_ord_id: str | None = None
    account: str | None = None
    reason: Reason | None = None
    reason_code: str | None = None      # a rule's own code wins when set
    text: str | None = None
    #: OrdStatusReqID (790) echoed on a status answer, 4.4 only.
    ord_status_req_id: str | None = None


@dataclass
class CancelReject:
    """A version-neutral OrderCancelReject."""

    order_id: str
    cl_ord_id: str
    orig_cl_ord_id: str
    ord_status: str
    response_to: ResponseTo
    reason: Reason
    text: str | None = None


# ------------------------------------------------------------------- tags

TAG_ACCOUNT = 1
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
TAG_SIDE = 54
TAG_SYMBOL = 55
TAG_TEXT = 58
TAG_TRANSACT_TIME = 60
TAG_TIME_IN_FORCE = 59
TAG_EXPIRE_TIME = 126
TAG_EXPIRE_DATE = 432
TAG_ORD_STATUS_REQ_ID = 790
TAG_CXL_REJ_REASON = 102
TAG_ORD_REJ_REASON = 103
TAG_EXEC_TYPE = 150
TAG_LEAVES_QTY = 151
TAG_CXL_REJ_RESPONSE_TO = 434

MSG_EXECUTION_REPORT = "8"
MSG_ORDER_CANCEL_REJECT = "9"

# Shared across both versions in our scope.
HANDL_INSTS = frozenset({"1", "2", "3"})
SUPPORTED_SIDES = frozenset({"1", "2", "5", "6"})
SUPPORTED_ORD_TYPES = frozenset({"1", "2"})
ORD_TYPE_MARKET = "1"
ORD_TYPE_LIMIT = "2"
TIME_IN_FORCE_DAY = "0"

RESPONSE_TO_VALUES = {ResponseTo.CANCEL: "1", ResponseTo.REPLACE: "2"}


@dataclass
class FixVersionProfile:
    """Everything that differs between the FIX versions we speak."""

    name: str
    #: How the version is written in prose, e.g. in reject text.
    label: str
    begin_string: str
    required_tags: dict
    sides: frozenset
    ord_types: frozenset
    times_in_force: frozenset
    exec_types: dict
    reject_reasons: dict
    cancel_reject_reasons: dict
    include_exec_trans_type: bool = True
    handl_insts: frozenset = field(default=HANDL_INSTS)
    #: ExecType for an OrderStatusRequest answer; None = repeat OrdStatus (4.2).
    status_exec_type: str | None = None

    # ------------------------------------------------------------ queries

    def required_for(self, msg_type: str) -> tuple:
        return self.required_tags.get(msg_type, ())

    def exec_type_for(self, kind: ReportKind, ord_status: str = "") -> str:
        if kind is ReportKind.STATUS and self.status_exec_type is None:
            # 4.2 has no ExecType of its own for a status answer: ExecType
            # repeats the order's current status (and 20=3 says why).
            return ord_status
        if kind is ReportKind.STATUS:
            return self.status_exec_type
        return self.exec_types[kind]

    def reject_code_for(self, reason: Reason | None,
                        override: str | None = None) -> str | None:
        if override is not None:
            return override
        if reason is None:
            return None
        return self.reject_reasons.get(reason)

    def cancel_reject_code_for(self, reason: Reason) -> str:
        return self.cancel_reject_reasons[reason]

    # ------------------------------------------------------------- render

    def render_exec_report(self, report: ExecReport) -> list:
        """Neutral report -> ordered FIX body fields for this version."""
        body = [
            (TAG_ORDER_ID, report.order_id),
            (TAG_CL_ORD_ID, report.cl_ord_id),
        ]
        if report.orig_cl_ord_id is not None:
            body.append((TAG_ORIG_CL_ORD_ID, report.orig_cl_ord_id))
        body.append((TAG_EXEC_ID, report.exec_id))
        if self.include_exec_trans_type:
            # 4.2 only: ExecTransType was dropped in 4.4.  A status answer
            # is 20=3 (Status), every other report 20=0 (New).
            body.append((TAG_EXEC_TRANS_TYPE,
                         "3" if report.kind is ReportKind.STATUS else "0"))
        if report.ord_status_req_id and not self.include_exec_trans_type:
            body.append((TAG_ORD_STATUS_REQ_ID, report.ord_status_req_id))
        body.extend([
            (TAG_EXEC_TYPE, self.exec_type_for(report.kind, report.ord_status)),
            (TAG_ORD_STATUS, report.ord_status),
        ])
        if report.account:
            body.append((TAG_ACCOUNT, report.account))
        body.extend([
            (TAG_SYMBOL, report.symbol),
            (TAG_SIDE, report.side),
            (TAG_ORDER_QTY, report.order_qty),
        ])
        if report.ord_type is not None:
            # Only a status answer about an unknown order can lack it.
            body.append((TAG_ORD_TYPE, report.ord_type))
        if report.price is not None:
            body.append((TAG_PRICE, report.price))
        body.extend([
            (TAG_LAST_QTY, report.last_qty),
            (TAG_LAST_PX, report.last_px),
            (TAG_LEAVES_QTY, report.leaves_qty),
            (TAG_CUM_QTY, report.cum_qty),
            (TAG_AVG_PX, report.avg_px),
            (TAG_TRANSACT_TIME, report.transact_time),
        ])
        code = self.reject_code_for(report.reason, report.reason_code)
        if code is not None:
            body.append((TAG_ORD_REJ_REASON, code))
        if report.text:
            body.append((TAG_TEXT, report.text))
        return body

    def render_cancel_reject(self, reject: CancelReject) -> list:
        return [
            (TAG_ORDER_ID, reject.order_id),
            (TAG_CL_ORD_ID, reject.cl_ord_id if reject.cl_ord_id is not None
             else ""),
            (TAG_ORIG_CL_ORD_ID,
             reject.orig_cl_ord_id if reject.orig_cl_ord_id is not None else ""),
            (TAG_ORD_STATUS, reject.ord_status),
            (TAG_CXL_REJ_RESPONSE_TO, RESPONSE_TO_VALUES[reject.response_to]),
            (TAG_CXL_REJ_REASON, self.cancel_reject_code_for(reject.reason)),
            (TAG_TEXT, reject.text),
        ]


# --------------------------------------------------------------- profiles

_FIX42_REQUIRED = {
    "D": (11, 21, 55, 54, 60, 38, 40),
    "F": (11, 41, 55, 54, 60),
    "G": (11, 41, 21, 55, 54, 60, 38, 40),
    "H": (11, 55, 54),
}

# HandlInst (21) became optional in 4.4.
_FIX44_REQUIRED = {
    "D": (11, 55, 54, 60, 38, 40),
    "F": (11, 41, 55, 54, 60),
    "G": (11, 41, 55, 54, 60, 38, 40),
    "H": (11, 55, 54),
}

_FIX42_EXEC_TYPES = {
    ReportKind.ACK: "0",
    ReportKind.PARTIAL_FILL: "1",
    ReportKind.FILL: "2",
    ReportKind.CANCELED: "4",
    ReportKind.REPLACED: "5",
    ReportKind.PENDING_CANCEL: "6",
    ReportKind.PENDING_REPLACE: "E",
    ReportKind.REJECTED: "8",
    ReportKind.PENDING_NEW: "A",
    ReportKind.DONE_FOR_DAY: "3",
    ReportKind.EXPIRED: "C",
}

# 4.4 folded both fill kinds into Trade; OrdStatus tells them apart.
_FIX44_EXEC_TYPES = dict(_FIX42_EXEC_TYPES)
_FIX44_EXEC_TYPES[ReportKind.PARTIAL_FILL] = "F"
_FIX44_EXEC_TYPES[ReportKind.FILL] = "F"

_FIX42_REJECT_REASONS = {
    Reason.UNSUPPORTED_CHARACTERISTIC: "0",
    Reason.BAD_QUANTITY: "0",
    Reason.BAD_PRICE: "0",
    Reason.DUPLICATE_CL_ORD_ID: "6",
    Reason.BAND_BREACH: "3",
    Reason.NO_REFERENCE: "0",
    Reason.RULE_REJECT: "0",
    Reason.UNKNOWN_ACCOUNT: "0",               # Broker option: 4.2 has no code
}

_FIX44_REJECT_REASONS = {
    Reason.UNSUPPORTED_CHARACTERISTIC: "11",   # Unsupported order characteristic
    Reason.BAD_QUANTITY: "13",                 # Incorrect quantity
    Reason.BAD_PRICE: "0",
    Reason.DUPLICATE_CL_ORD_ID: "6",
    Reason.BAND_BREACH: "3",
    Reason.NO_REFERENCE: "99",                 # Other
    Reason.RULE_REJECT: "0",
    Reason.UNKNOWN_ACCOUNT: "15",              # Unknown account(s)
}

_FIX42_CANCEL_REASONS = {
    Reason.CXL_TOO_LATE: "0",
    Reason.CXL_UNKNOWN_ORDER: "1",
    Reason.CXL_BROKER_OPTION: "2",
    Reason.DUPLICATE_CL_ORD_ID: "2",
}

_FIX44_CANCEL_REASONS = dict(_FIX42_CANCEL_REASONS)
_FIX44_CANCEL_REASONS[Reason.DUPLICATE_CL_ORD_ID] = "6"   # Duplicate ClOrdID

# FIX 4.2 Side is 1-9; 4.4 added A-G.
_FIX42_SIDES = frozenset("123456789")
_FIX44_SIDES = frozenset("123456789ABCDEFG")

# FIX 4.2 OrdType is 1-9, A-I and P; 4.4 added J-M.
_FIX42_ORD_TYPES = frozenset("123456789ABCDEFGHIP")
_FIX44_ORD_TYPES = frozenset("123456789ABCDEFGHIJKLMP")

# TimeInForce: 4.2 has 0-6, 4.4 added 7 (At the Close).
_FIX42_TIF = frozenset("0123456")
_FIX44_TIF = frozenset("01234567")


FIX42 = FixVersionProfile(
    name=FIX_4_2,
    label="FIX 4.2",
    begin_string=FIX_4_2,
    required_tags=_FIX42_REQUIRED,
    sides=_FIX42_SIDES,
    ord_types=_FIX42_ORD_TYPES,
    times_in_force=_FIX42_TIF,
    exec_types=_FIX42_EXEC_TYPES,
    reject_reasons=_FIX42_REJECT_REASONS,
    cancel_reject_reasons=_FIX42_CANCEL_REASONS,
    include_exec_trans_type=True,
)

FIX44 = FixVersionProfile(
    name=FIX_4_4,
    label="FIX 4.4",
    begin_string=FIX_4_4,
    required_tags=_FIX44_REQUIRED,
    sides=_FIX44_SIDES,
    ord_types=_FIX44_ORD_TYPES,
    times_in_force=_FIX44_TIF,
    exec_types=_FIX44_EXEC_TYPES,
    reject_reasons=_FIX44_REJECT_REASONS,
    cancel_reject_reasons=_FIX44_CANCEL_REASONS,
    include_exec_trans_type=False,
    status_exec_type="I",                      # Order Status
)

PROFILES = {FIX_4_2: FIX42, FIX_4_4: FIX44}
SUPPORTED_VERSIONS = tuple(PROFILES)


class UnknownFixVersion(Exception):
    """Raised for a fix_version we have no profile for."""


def profile_for(fix_version: str) -> FixVersionProfile:
    try:
        return PROFILES[fix_version]
    except KeyError:
        raise UnknownFixVersion(
            f"unsupported FIX version {fix_version!r}; "
            f"supported: {', '.join(SUPPORTED_VERSIONS)}"
        ) from None
