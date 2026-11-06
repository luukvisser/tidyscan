"""A time budget for one page: straighten() starts it, and every Tesseract call within gets at most
what is left of it (and never more than its own limit). Spent, a call ends at once as a timeout,
which every caller already takes as "unsure": the page is kept as scanned rather than taking many
minutes (a dense table turned a quarter took up to 130 s, 05-11-2026; the worst case is far longer)."""

from __future__ import annotations

import contextvars
import logging
import subprocess
import time

log = logging.getLogger(__name__)

_END: contextvars.ContextVar[float | None] = contextvars.ContextVar("tidyscan_page_deadline", default=None)


def start(seconds: float | None) -> contextvars.Token[float | None]:
    return _END.set(None if seconds is None else time.monotonic() + seconds)


def end(token: contextvars.Token[float | None]) -> None:
    _END.reset(token)


def timeout(limit: float) -> float:
    """The timeout for the next call: its own limit, or less when the page's budget is nearly spent.
    Raises subprocess.TimeoutExpired when nothing is left."""
    deadline = _END.get()
    if deadline is None:
        return limit
    left = deadline - time.monotonic()
    if left <= 0:
        log.warning("the page's time budget is spent; what is not measured yet is kept as scanned")
        raise subprocess.TimeoutExpired("tesseract", 0)
    return min(limit, left)
