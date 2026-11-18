"""Shared analyzer types."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import SymbolNode


@dataclass(slots=True)
class ParsedFile:
    """Intermediate representation produced by a language analyzer."""

    symbols: list[SymbolNode] = field(default_factory=list)
    # Maps caller symbol ID -> list of raw callee names referenced inside it.
    raw_calls: dict[str, list[str]] = field(default_factory=dict)
    # Imported module/file hints to help disambiguate cross-file calls.
    imports: list[str] = field(default_factory=list)
    # Maps local alias -> original imported symbol name (e.g. "vt" -> "verify_token").
    aliases: dict[str, str] = field(default_factory=dict)
