"""Secret storage.

Secrets live in the OS credential store via `keyring`:
  Windows -> Credential Manager, macOS -> Keychain, Linux -> Secret Service.
They are never written to the repo, the DuckDB file, or exported tabs.

Lookup order for a secret NAME:
  1. environment variable NAME (handy for CI / one-off runs)
  2. OS credential store, service "montney-stack", username NAME

Source specs reference secrets as "{secret:NAME}".
"""

from __future__ import annotations

import os

import keyring
from keyring.errors import PasswordDeleteError

SERVICE = "montney-stack"
_INDEX = "__index__"  # keyring can't enumerate, so we keep a list of names


class MissingSecret(KeyError):
    pass


def _names() -> list[str]:
    raw = keyring.get_password(SERVICE, _INDEX) or ""
    return [n for n in raw.split(",") if n]


def _save_names(names: list[str]) -> None:
    # dict.fromkeys, not set(): this module defines its own set() below
    keyring.set_password(SERVICE, _INDEX, ",".join(sorted(dict.fromkeys(names))))


def get(name: str) -> str:
    val = os.environ.get(name)
    if val:
        return val
    try:
        val = keyring.get_password(SERVICE, name)
    except Exception:  # no usable backend
        val = None
    if not val:
        raise MissingSecret(
            f"secret {name!r} not set. Run:  uv run montney secrets set {name}"
        )
    return val


def has(name: str) -> bool:
    try:
        get(name)
        return True
    except MissingSecret:
        return False


def set(name: str, value: str) -> None:  # noqa: A001 - module-level API
    keyring.set_password(SERVICE, name, value)
    _save_names(_names() + [name])


def delete(name: str) -> bool:
    try:
        keyring.delete_password(SERVICE, name)
    except PasswordDeleteError:
        return False
    _save_names([n for n in _names() if n != name])
    return True


def list_names() -> list[str]:
    return _names()


def clean(value: str) -> str:
    """Strip whitespace and control characters a terminal paste can smuggle in (e.g. ^V)."""
    return "".join(ch for ch in value if ch.isprintable()).strip()


def masked(value: str) -> str:
    return f"{len(value)} chars, ending ...{value[-4:]}" if len(value) >= 8 else f"{len(value)} chars"
