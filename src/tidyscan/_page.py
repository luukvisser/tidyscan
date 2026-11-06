"""A page as this package takes it: JPEG bytes, binary PBM (P4) bytes, or an object with to_pbm()."""

from __future__ import annotations

import tempfile
from typing import Protocol, TypeAlias, cast


class PbmSource(Protocol):
    """A page that encodes itself, e.g. a black-and-white scan held as rows."""

    def to_pbm(self) -> bytes: ...


Page: TypeAlias = bytes | bytearray | memoryview | PbmSource
"""JPEG bytes, binary PBM (P4) bytes, or an object with to_pbm()."""


def encoded(page: Page) -> tuple[bytes, str]:
    """(data, file extension) of a page."""
    if hasattr(page, "to_pbm"):
        return cast("PbmSource", page).to_pbm(), "pbm"
    data = bytes(page)
    return data, "pbm" if data.startswith(b"P4") else "jpg"


def workdir(path: str | None) -> str:
    return path or tempfile.gettempdir()
