"""One way to say what a FIX message is, shared by the CLI and the web.

The CLI prints these strings and the web viewer is sent the same strings, so
a message reads the same wherever you meet it.  Nothing here does I/O and
nothing here knows about colour or HTML: a summary is text, and whoever
displays it decides how it looks.
"""

from __future__ import annotations

TAG_AVG_PX = 6
TAG_BEGIN_SEQ_NO = 7
TAG_CL_ORD_ID = 11
TAG_CUM_QTY = 14
TAG_END_SEQ_NO = 16
TAG_EXEC_ID = 17
TAG_LAST_PX = 31
TAG_LAST_QTY = 32
TAG_NEW_SEQ_NO = 36
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
TAG_TIME_IN_FORCE = 59
TAG_HEART_BT_INT = 108
TAG_TEST_REQ_ID = 112
TAG_CXL_REJ_REASON = 102
TAG_ORD_REJ_REASON = 103
TAG_GAP_FILL_FLAG = 123
TAG_RESET_SEQ_NUM_FLAG = 141
TAG_EXEC_TYPE = 150
TAG_LEAVES_QTY = 151
TAG_REF_TAG_ID = 371
TAG_SESSION_REJECT_REASON = 373
TAG_BUSINESS_REJECT_REASON = 380
TAG_CXL_REJ_RESPONSE_TO = 434

#: Message types that are somebody saying no.
REJECT_TYPES = frozenset({"3", "j", "9"})

#: ExecTypes that carry a fill: 4.2 spells it 1/2, 4.4 spells it F.
FILL_EXEC_TYPES = frozenset({"1", "2", "F"})

#: Admin tags worth showing on a session message, in the order they read best.
ADMIN_TAGS = (TAG_TEST_REQ_ID, TAG_BEGIN_SEQ_NO, TAG_END_SEQ_NO,
              TAG_NEW_SEQ_NO, TAG_GAP_FILL_FLAG, TAG_RESET_SEQ_NUM_FLAG,
              TAG_HEART_BT_INT, TAG_TEXT)

#: Flag labels, one vocabulary for the CLI's `[...]` and the web's badges.
FLAG_INJECTED = "INJECTED"
FLAG_POSS_DUP = "POSSDUP"
FLAG_BAD_CHECKSUM = "BAD-CHECKSUM"
FLAG_BAD_LENGTH = "BAD-LENGTH"


def flags_for(message) -> list:
    """The labels that belong on this message, in a fixed order."""
    flags = []
    if message.injected:
        flags.append(FLAG_INJECTED)
    if message.bad_checksum:
        flags.append(FLAG_BAD_CHECKSUM)
    if message.bad_length:
        flags.append(FLAG_BAD_LENGTH)
    if message.poss_dup:
        flags.append(FLAG_POSS_DUP)
    return flags


#: Width of a rendered stamp, so a column lines up whether or not there is one.
STAMP_WIDTH = 21


def stamp_of(message) -> str:
    """`20260927-09:37:08.817`, or dashes -- the CLI's column and the web's."""
    if message.ts is None:
        return "-" * STAMP_WIDTH
    return (message.ts.strftime("%Y%m%d-%H:%M:%S.") +
            f"{message.ts.microsecond // 1000:03d}")


def msg_type_name(message, dictionary) -> str:
    """`New Order`, not `D` -- falling back to the raw type."""
    name = dictionary.enum_name(35, message.msg_type, message.begin_string)
    return name or message.msg_type or "?"


def is_reject(message) -> bool:
    if message.msg_type in REJECT_TYPES:
        return True
    return message.msg_type == "8" and message.get(TAG_ORD_STATUS) == "8"


class SummaryContext:
    """What we have seen of an order, so a replace can say what changed.

    Fed messages in the order they happened.  A cancel/replace names the
    order it is replacing in tag 41, and the quantity and price it wants in
    38 and 44; the old values are only in the earlier message, so we keep
    them.  Nothing is required: without a context a replace simply states
    what it is asking for.
    """

    def __init__(self) -> None:
        self.qty: dict = {}
        self.price: dict = {}

    def note(self, message) -> None:
        cl_ord_id = message.get(TAG_CL_ORD_ID)
        if not cl_ord_id:
            return
        qty = message.get(TAG_ORDER_QTY)
        price = message.get(TAG_PRICE)
        # A rejected request never took effect, so it is not what the order
        # looked like; everything else is the latest word on it.
        if message.msg_type == "8" and message.get(TAG_ORD_STATUS) == "8":
            return
        if qty:
            self.qty[cl_ord_id] = qty
        if price:
            self.price[cl_ord_id] = price

    def previous(self, cl_ord_id) -> tuple:
        return self.qty.get(cl_ord_id), self.price.get(cl_ord_id)


def _change(label, old, new) -> str | None:
    """`qty 500->800`, or `qty 800` when the old value is not in the log."""
    if new is None:
        return None
    if old is not None and old != new:
        return f"{label} {old}->{new}"
    return f"{label} {new}"


def summarize(message, dictionary, context: SummaryContext | None = None):
    """(text, style) for one message, keyed on what it is.

    `style` is a hint -- `reject`, `fill`, `dim` or None -- that the CLI turns
    into colour and the web turns into a class.
    """
    version = message.begin_string
    msg_type = message.msg_type

    def enum(tag):
        value = message.get(tag)
        if value is None:
            return None
        return dictionary.enum_name(tag, value, version) or value

    def joined(bits, style=None):
        return " ".join(str(bit) for bit in bits if bit), style

    if msg_type == "D":
        bits = [enum(TAG_SIDE), message.get(TAG_ORDER_QTY),
                message.get(TAG_SYMBOL), enum(TAG_ORD_TYPE)]
        if message.get(TAG_PRICE):
            bits.append(f"@{message.get(TAG_PRICE)}")
        bits.append(f"11={message.get(TAG_CL_ORD_ID)}")
        return joined(bits)

    if msg_type == "8":
        exec_type = message.get(TAG_EXEC_TYPE)
        bits = [f"{enum(TAG_EXEC_TYPE)}/{enum(TAG_ORD_STATUS)}"]
        last_qty = message.get(TAG_LAST_QTY)
        if last_qty and last_qty not in ("0", "0.00"):
            bits.append(f"{last_qty}@{message.get(TAG_LAST_PX)}")
        bits.append(f"cum={message.get(TAG_CUM_QTY)}")
        bits.append(f"lv={message.get(TAG_LEAVES_QTY)}")
        if message.get(TAG_AVG_PX):
            bits.append(f"avg={message.get(TAG_AVG_PX)}")
        bits.append(f"11={message.get(TAG_CL_ORD_ID)}")
        if message.get(TAG_ORD_REJ_REASON):
            bits.append(f"103={enum(TAG_ORD_REJ_REASON)}")
        if message.get(TAG_TEXT):
            bits.append(f'"{message.get(TAG_TEXT)}"')
        style = "reject" if message.get(TAG_ORD_STATUS) == "8" else (
            "fill" if exec_type in FILL_EXEC_TYPES else None)
        return joined(bits, style)

    if msg_type == "G":
        orig = message.get(TAG_ORIG_CL_ORD_ID)
        old_qty, old_price = (context.previous(orig) if context and orig
                              else (None, None))
        bits = [f"Replace {orig}->{message.get(TAG_CL_ORD_ID)}",
                _change("qty", old_qty, message.get(TAG_ORDER_QTY)),
                _change("px", old_price, message.get(TAG_PRICE))]
        return joined(bits)

    if msg_type == "F":
        return joined([f"Cancel {message.get(TAG_ORIG_CL_ORD_ID)}->"
                       f"{message.get(TAG_CL_ORD_ID)}"])

    if msg_type == "9":
        bits = [f"to={enum(TAG_CXL_REJ_RESPONSE_TO)}",
                f"reason={enum(TAG_CXL_REJ_REASON)}",
                f"11={message.get(TAG_CL_ORD_ID)}",
                f"41={message.get(TAG_ORIG_CL_ORD_ID)}"]
        if message.get(TAG_TEXT):
            bits.append(f'"{message.get(TAG_TEXT)}"')
        return joined(bits, "reject")

    if msg_type in ("3", "j"):
        reason_tag = (TAG_SESSION_REJECT_REASON if msg_type == "3"
                      else TAG_BUSINESS_REJECT_REASON)
        bits = [f"ref={message.get(TAG_REF_SEQ_NUM)}"]
        if message.get(TAG_REF_TAG_ID):
            bits.append(f"tag={message.get(TAG_REF_TAG_ID)}")
        bits.append(f"reason={enum(reason_tag)}")
        if message.get(TAG_TEXT):
            bits.append(f'"{message.get(TAG_TEXT)}"')
        return joined(bits, "reject")

    # Session admin: say the little that matters.
    bits = [f"{tag}={message.get(tag)}" for tag in ADMIN_TAGS
            if message.get(tag)]
    return joined(bits, "dim")


def summarize_all(messages, dictionary) -> dict:
    """Summaries for a whole list, in order, so replaces see their history.

    Keyed by message index, because a caller that filters or re-sorts the
    list still wants the summary that was worked out in time order.
    """
    context = SummaryContext()
    summaries = {}
    for message in messages:
        summaries[message.index] = summarize(message, dictionary, context)
        context.note(message)
    return summaries
