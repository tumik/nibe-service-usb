"""Errors shared by core services and the API layer."""

from __future__ import annotations


class CommandError(Exception):
    """A command failed; ``code`` is one of the error codes in ``protocol.md``."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
