"""The one place that knows what this build is called."""

ORDERECHO_VERSION = "0.8.0"
ORDERECHO_BUILD = "cook8"


def version_string() -> str:
    return f"{ORDERECHO_VERSION} ({ORDERECHO_BUILD})"
