"""The one place that knows what this build is called."""

ORDERECHO_VERSION = "0.9.0"
ORDERECHO_BUILD = "cook9"


def version_string() -> str:
    return f"{ORDERECHO_VERSION} ({ORDERECHO_BUILD})"
