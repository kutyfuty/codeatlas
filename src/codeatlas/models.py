"""Data models and graph analytics for CodeAtlas."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class SymbolNode:
    """Represents a function, method, class, or module symbol in the codebase."""

    id: str
    name: str
    kind: str  # "function", "method", "class"
    file: str
    language: str
    start_line: int
    end_line: int
    docstring: str = ""
    parent: str | None = None
    fan_in: int = 0
    fan_out: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CallEdge:
    """Represents a directed call relationship from one symbol to another."""

    source: str
    target: str
    call_count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FileSummary:
    """Metadata about a scanned source file."""

    path: str
    language: str
    lines: int
    symbol_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CodeGraph:
    """Complete architectural call graph and summary statistics for a repository."""

    root: str
    nodes: list[SymbolNode] = field(default_factory=list)
    edges: list[CallEdge] = field(default_factory=list)
    file_edges: list[CallEdge] = field(default_factory=list)
    files: list[FileSummary] = field(default_factory=list)
    cycles: list[list[str]] = field(default_factory=list)

    def stats(self) -> dict[str, Any]:
        """Compute summary statistics and top hubs for quick inspection."""
        by_lang: dict[str, int] = {}
        for f in self.files:
            by_lang[f.language] = by_lang.get(f.language, 0) + 1

        unreferenced = sum(
            1 for n in self.nodes if n.kind != "class" and n.fan_in == 0
        )

        hotspots = sorted(
            self.nodes,
            key=lambda n: (n.fan_in + n.fan_out, n.fan_in),
            reverse=True,
        )[:10]

        return {
            "total_files": len(self.files),
            "total_symbols": len(self.nodes),
            "total_edges": len(self.edges),
            "cycle_count": len(self.cycles),
            "unreferenced_count": unreferenced,
            "languages": by_lang,
            "hotspots": [
                {
                    "id": n.id,
                    "name": n.name,
                    "file": n.file,
                    "fan_in": n.fan_in,
                    "fan_out": n.fan_out,
                }
                for n in hotspots
                if (n.fan_in + n.fan_out) > 0
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "stats": self.stats(),
            "files": [f.to_dict() for f in self.files],
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "file_edges": [e.to_dict() for e in self.file_edges],
            "cycles": self.cycles,
        }

    def to_mermaid(self, max_edges: int = 80) -> str:
        """Render the call graph as a Mermaid flowchart."""
        lines = ["flowchart LR"]
        node_map = {n.id: n for n in self.nodes}
        safe_id = lambda raw: "n_" + "".join(c if c.isalnum() else "_" for c in raw)

        emitted: set[str] = set()
        for edge in self.edges[:max_edges]:
            src = node_map.get(edge.source)
            tgt = node_map.get(edge.target)
            if not src or not tgt:
                continue
            for node in (src, tgt):
                if node.id not in emitted:
                    emitted.add(node.id)
                    label = f"{node.name} ({node.file})".replace('"', "'")
                    lines.append(f'    {safe_id(node.id)}["{label}"]')
            lines.append(f"    {safe_id(src.id)} --> {safe_id(tgt.id)}")

        if len(lines) == 1:
            lines.append('    empty["No call edges detected"]')
        return "\n".join(lines)
