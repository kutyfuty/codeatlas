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
        callable_count = max(sum(1 for n in self.nodes if n.kind != "class"), 1)
        cycle_penalty = min(40, len(self.cycles) * 8)
        dead_ratio = unreferenced / callable_count
        dead_penalty = int(max(0.0, (dead_ratio - 0.25) * 35))
        health_score = max(0, min(100, 100 - cycle_penalty - dead_penalty))

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
            "health_score": health_score,
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

    def to_markdown(self, max_hubs: int = 8) -> str:
        """Render a concise GitHub-flavored Markdown architectural report."""
        st = self.stats()
        lines = [
            "## CodeAtlas Architecture Summary",
            "",
            f"- **Files:** {st['total_files']} | **Symbols:** {st['total_symbols']} | "
            f"**Call Edges:** {st['total_edges']} | **Cycles:** {st['cycle_count']} | "
            f"**Health Score:** `{st['health_score']}/100`",
        ]
        if st["languages"]:
            langs = ", ".join(f"{k} ({v})" for k, v in sorted(st["languages"].items()))
            lines.append(f"- **Languages:** {langs}")

        if st["hotspots"]:
            lines.extend(["", "### Top Call-Graph Hubs", "", "| Symbol | File | Fan-In | Fan-Out |", "|---|---|---|---|"])
            for hub in st["hotspots"][:max_hubs]:
                lines.append(
                    f"| `{hub['name']}` | `{hub['file']}` | {hub['fan_in']} | {hub['fan_out']} |"
                )

        if self.cycles:
            lines.extend(["", "### Circular Call Groups"])
            for idx, cyc in enumerate(self.cycles[:5], start=1):
                chain = " ↔ ".join(f"`{c.split('::')[-1]}`" for c in cyc[:6])
                lines.append(f"{idx}. {chain}")

        lines.extend(["", "<details><summary>Mermaid Call Graph</summary>", "", "```mermaid", self.to_mermaid(), "```", "", "</details>"])
        return "\n".join(lines)

    def to_html(self) -> str:
        """Render a self-contained single-file HTML architecture report."""
        import html
        import json

        st = self.stats()
        data_json = json.dumps(self.to_dict()).replace("</", "<\\/")
        root_esc = html.escape(self.root)
        return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>CodeAtlas Report — {root_esc}</title>
<style>
  body {{ margin:0; font-family:Inter,system-ui,sans-serif; background:#020617; color:#e2e8f0; padding:1.5rem; }}
  header {{ display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:0.75rem; border-bottom:1px solid #1e293b; padding-bottom:1rem; margin-bottom:1.2rem; }}
  .pills span {{ background:#1e293b; padding:0.3rem 0.65rem; border-radius:999px; font-size:0.82rem; margin-left:0.4rem; }}
  .toolbar {{ margin-bottom:1rem; display:flex; gap:0.75rem; align-items:center; }}
  input {{ background:#0f172a; color:#f8fafc; border:1px solid #334155; border-radius:6px; padding:0.45rem 0.7rem; width:300px; font-size:0.86rem; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(360px, 1fr)); gap:1rem; }}
  .card {{ background:#0f172a; border:1px solid #1e293b; border-radius:8px; padding:1rem; max-height:420px; overflow:auto; }}
  h2 {{ margin:0 0 0.75rem; font-size:0.85rem; text-transform:uppercase; letter-spacing:0.04em; color:#94a3b8; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.82rem; }}
  th, td {{ text-align:left; padding:0.4rem; border-bottom:1px solid #1e293b; }}
  .score {{ color:#22c55e; font-weight:700; }}
  .warn {{ color:#fb923c; }}
</style>
</head>
<body>
<header>
  <div><strong>◈ CodeAtlas Architecture Report</strong> — <span>{root_esc}</span></div>
  <div class="pills">
    <span>{st['total_files']} files</span>
    <span>{st['total_symbols']} symbols</span>
    <span>{st['total_edges']} edges</span>
    <span class="warn">{st['cycle_count']} cycles</span>
    <span class="score">Health: {st['health_score']}/100</span>
  </div>
</header>
<div class="toolbar">
  <input id="q" type="search" placeholder="Filter hubs, files, or symbols..." />
  <span id="langs" style="font-size:0.82rem;color:#94a3b8"></span>
</div>
<div class="grid">
  <div class="card">
    <h2>Top Call-Graph Hubs</h2>
    <table>
      <thead><tr><th>Symbol</th><th>File</th><th>In</th><th>Out</th></tr></thead>
      <tbody id="hubs"></tbody>
    </table>
  </div>
  <div class="card">
    <h2>Files Breakdown (<span id="fcount"></span>)</h2>
    <table>
      <thead><tr><th>File</th><th>Language</th><th>Lines</th><th>Symbols</th></tr></thead>
      <tbody id="files"></tbody>
    </table>
  </div>
  <div class="card">
    <h2>Circular Dependencies (<span id="ccount"></span>)</h2>
    <table>
      <thead><tr><th>#</th><th>Cycle Chain</th></tr></thead>
      <tbody id="cycles"></tbody>
    </table>
  </div>
  <div class="card">
    <h2>Unreferenced Symbols (fan-in = 0)</h2>
    <table>
      <thead><tr><th>Symbol</th><th>Kind</th><th>File</th></tr></thead>
      <tbody id="dead"></tbody>
    </table>
  </div>
</div>
<script>
  const DATA = {data_json};
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
  document.getElementById('langs').textContent = Object.entries(DATA.stats.languages || {{}}).map(([k,v]) => `${{k}}: ${{v}}`).join(' · ');
  const render = (q = '') => {{
    const s = q.trim().toLowerCase();
    const hubs = DATA.stats.hotspots.filter(h => !s || h.name.toLowerCase().includes(s) || h.file.toLowerCase().includes(s));
    document.getElementById('hubs').innerHTML = hubs.map(h =>
      `<tr><td>${{esc(h.name)}}</td><td>${{esc(h.file)}}</td><td>${{h.fan_in}}</td><td>${{h.fan_out}}</td></tr>`
    ).join('') || '<tr><td colspan="4">No matching hubs</td></tr>';

    const files = DATA.files.filter(f => !s || f.path.toLowerCase().includes(s) || f.language.toLowerCase().includes(s));
    document.getElementById('fcount').textContent = files.length;
    document.getElementById('files').innerHTML = files.map(f =>
      `<tr><td>${{esc(f.path)}}</td><td>${{esc(f.language)}}</td><td>${{f.lines}}</td><td>${{f.symbol_count}}</td></tr>`
    ).join('') || '<tr><td colspan="4">No matching files</td></tr>';

    document.getElementById('ccount').textContent = DATA.cycles.length;
    document.getElementById('cycles').innerHTML = DATA.cycles.map((c, i) =>
      `<tr><td>${{i + 1}}</td><td>${{esc(c.map(x => x.split('::').pop()).join(' ↔ '))}}</td></tr>`
    ).join('') || '<tr><td colspan="2">No circular dependencies</td></tr>';

    const dead = DATA.nodes.filter(n => n.kind !== 'class' && n.fan_in === 0 && (!s || n.name.toLowerCase().includes(s) || n.file.toLowerCase().includes(s))).slice(0, 100);
    document.getElementById('dead').innerHTML = dead.map(n =>
      `<tr><td>${{esc(n.name)}}</td><td>${{esc(n.kind)}}</td><td>${{esc(n.file)}}</td></tr>`
    ).join('') || '<tr><td colspan="3">None</td></tr>';
  }};
  document.getElementById('q').addEventListener('input', (e) => render(e.target.value));
  render('');
</script>
</body>
</html>"""
