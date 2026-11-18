"""Directory scanner, call-edge resolver, and graph analytics."""

from __future__ import annotations

import fnmatch
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

from .analyzers import EXTENSION_LANGUAGE, analyze_file, has_treesitter
from .analyzers.base import ParsedFile
from .models import CallEdge, CodeGraph, FileSummary, SymbolNode

DEFAULT_IGNORE_DIRS = frozenset({
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".svelte-kit",
    ".next",
    "coverage",
    "target",
})

# Very generic method/function names that cause false cross-file edges when unresolved.
_AMBIGUOUS_GLOBAL_NAMES = frozenset({
    "get", "set", "add", "remove", "delete", "update", "create", "read",
    "write", "open", "close", "run", "start", "stop", "init", "main",
    "append", "extend", "pop", "keys", "values", "items", "format",
    "join", "split", "strip", "replace", "find", "filter", "map", "reduce",
    "sort", "copy", "clear", "len", "str", "int", "float", "list", "dict",
    "print", "log", "info", "debug", "warn", "error", "parse", "render",
    "then", "catch", "finally", "resolve", "reject", "push", "slice", "splice",
    "shift", "unshift", "includes", "indexOf", "toString", "valueOf",
    "unwrap", "expect", "clone", "default", "new", "from", "into", "ok",
    "err", "some", "none", "collect", "iter", "next", "is_empty", "String", "Error",
})


def _compile_ignore_patterns(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(fnmatch.translate(pat)) for pat in patterns]


def _load_gitignore(root: Path) -> list[str]:
    """Load simple non-comment patterns from .gitignore if present."""
    gitignore = root / ".gitignore"
    if not gitignore.is_file():
        return []
    patterns: list[str] = []
    try:
        for raw in gitignore.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("!"):
                continue
            cleaned = line.strip("/")
            if cleaned:
                patterns.append(cleaned)
    except OSError:
        return []
    return patterns


def _is_ignored(rel_posix: str, name: str, compiled_patterns: list[re.Pattern[str]]) -> bool:
    if name in DEFAULT_IGNORE_DIRS:
        return True
    if name.endswith((".min.js", ".min.mjs", ".bundle.js")):
        return True
    for pat in compiled_patterns:
        if pat.match(name) or pat.match(rel_posix):
            return True
    return False


def _find_cycles(nodes: list[SymbolNode], edges: list[CallEdge], limit: int = 25) -> list[list[str]]:
    """Find strongly connected components of size >= 2 using iterative Tarjan's algorithm."""
    adj: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        if e.source != e.target:
            adj[e.source].append(e.target)

    index_counter = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    tarjan_stack: list[str] = []
    on_stack: set[str] = set()
    sccs: list[list[str]] = []

    for root_node in nodes:
        if root_node.id in indices:
            continue
        if len(sccs) >= limit:
            break

        # Call stack entries: [node_id, neighbor_iterator_index]
        call_stack: list[list] = [[root_node.id, 0]]
        indices[root_node.id] = index_counter
        lowlink[root_node.id] = index_counter
        index_counter += 1
        tarjan_stack.append(root_node.id)
        on_stack.add(root_node.id)

        while call_stack:
            v, nb_idx = call_stack[-1]
            neighbors = adj.get(v, ())
            if nb_idx < len(neighbors):
                call_stack[-1][1] = nb_idx + 1
                w = neighbors[nb_idx]
                if w not in indices:
                    indices[w] = index_counter
                    lowlink[w] = index_counter
                    index_counter += 1
                    tarjan_stack.append(w)
                    on_stack.add(w)
                    call_stack.append([w, 0])
                elif w in on_stack:
                    if indices[w] < lowlink[v]:
                        lowlink[v] = indices[w]
            else:
                call_stack.pop()
                if call_stack:
                    parent = call_stack[-1][0]
                    if lowlink[v] < lowlink[parent]:
                        lowlink[parent] = lowlink[v]

                if lowlink[v] == indices[v]:
                    component: list[str] = []
                    while True:
                        w = tarjan_stack.pop()
                        on_stack.remove(w)
                        component.append(w)
                        if w == v:
                            break
                    if len(component) >= 2:
                        sccs.append(list(reversed(component)))
                        if len(sccs) >= limit:
                            return sccs

    return sccs[:limit]


def _build_import_paths(imports: list[str]) -> list[list[str]]:
    return [
        [p for p in imp.replace("\\", "/").replace(".", "/").split("/") if p]
        for imp in imports
    ]


def _resolve_target(
    callee_name: str,
    caller_node: SymbolNode | None,
    caller_file: str,
    caller_import_paths: list[list[str]],
    by_file_and_name: dict[tuple[str, str], list[SymbolNode]],
    by_name: dict[str, list[SymbolNode]],
    file_meta: dict[str, tuple[str, str, set[str]]],
) -> SymbolNode | None:
    """Resolve a raw callee name to the most likely SymbolNode."""
    # 1. Same-file symbol (prefer same-class method if caller is inside a class)
    local_matches = by_file_and_name.get((caller_file, callee_name))
    if local_matches:
        if caller_node and caller_node.parent:
            for m in local_matches:
                if m.parent == caller_node.parent:
                    return m
        return local_matches[0]

    candidates = by_name.get(callee_name)
    if not candidates:
        return None

    # 2. Imported module/file/package hint match (scored by matching path segments)
    if caller_import_paths:
        best_score = 0
        best_candidate: SymbolNode | None = None
        for c in candidates:
            stem, parent_dir, c_parts = file_meta[c.file]
            for parts in caller_import_paths:
                if not parts:
                    continue
                if stem in parts:
                    score = sum(1 for p in parts if p in c_parts) + 1
                    if score > best_score:
                        best_score = score
                        best_candidate = c
                elif parent_dir and parent_dir == parts[-1]:
                    score = sum(1 for p in parts if p in c_parts)
                    if score > best_score:
                        best_score = score
                        best_candidate = c
        if best_candidate is not None:
            return best_candidate

    # 3. Unique symbol in codebase (skip overly generic names)
    if len(candidates) == 1 and callee_name not in _AMBIGUOUS_GLOBAL_NAMES:
        return candidates[0]

    return None


def scan_directory(root_path: str | Path, extra_ignores: list[str] | None = None) -> CodeGraph:
    """Scan a codebase directory (or single source file) and return its resolved CodeGraph."""
    root = Path(root_path).resolve()
    files: list[FileSummary] = []
    parsed_files: list[tuple[str, ParsedFile]] = []
    all_nodes: list[SymbolNode] = []

    if root.is_file():
        analyzed = analyze_file(root, root.name)
        if analyzed is not None:
            lang, line_count, parsed = analyzed
            files.append(
                FileSummary(
                    path=root.name,
                    language=lang,
                    lines=line_count,
                    symbol_count=len(parsed.symbols),
                )
            )
            parsed_files.append((root.name, parsed))
            all_nodes.extend(parsed.symbols)
        root = root.parent
    else:
        patterns = _load_gitignore(root)
        if extra_ignores:
            patterns.extend(extra_ignores)
        compiled_patterns = _compile_ignore_patterns(patterns)

        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = Path(dirpath).relative_to(root).as_posix()
            dirnames[:] = sorted(
                d
                for d in dirnames
                if not _is_ignored(
                    f"{rel_dir}/{d}" if rel_dir != "." else d,
                    d,
                    compiled_patterns,
                )
            )

            for fname in sorted(filenames):
                dot = fname.rfind(".")
                if dot == -1 or fname[dot:].lower() not in EXTENSION_LANGUAGE:
                    continue

                rel_posix = f"{rel_dir}/{fname}" if rel_dir != "." else fname
                if _is_ignored(rel_posix, fname, compiled_patterns):
                    continue

                fpath = Path(dirpath) / fname
                analyzed = analyze_file(fpath, rel_posix)
                if analyzed is None:
                    continue

                lang, line_count, parsed = analyzed
                files.append(
                    FileSummary(
                        path=rel_posix,
                        language=lang,
                        lines=line_count,
                        symbol_count=len(parsed.symbols),
                    )
                )
                parsed_files.append((rel_posix, parsed))
                all_nodes.extend(parsed.symbols)

    by_file_and_name: dict[tuple[str, str], list[SymbolNode]] = defaultdict(list)
    by_name: dict[str, list[SymbolNode]] = defaultdict(list)
    node_map: dict[str, SymbolNode] = {}
    file_meta: dict[str, tuple[str, str, set[str]]] = {}

    for node in all_nodes:
        node_map[node.id] = node
        by_file_and_name[(node.file, node.name)].append(node)
        by_name[node.name].append(node)
        if node.file not in file_meta:
            c_path = Path(node.file)
            stem = c_path.parent.name if c_path.stem in {"__init__", "index"} else c_path.stem
            parent_dir = c_path.parent.name
            file_meta[node.file] = (stem, parent_dir, set(c_path.with_suffix("").parts))

    edge_counts: Counter[tuple[str, str]] = Counter()
    resolve_cache: dict[tuple[str, str | None, str], SymbolNode | None] = {}
    for rel_posix, parsed in parsed_files:
        import_paths = _build_import_paths(parsed.imports)
        for caller_id, callees in parsed.raw_calls.items():
            caller_node = node_map.get(caller_id)
            caller_parent = caller_node.parent if caller_node else None
            for raw_callee in callees:
                if (rel_posix, raw_callee) in by_file_and_name:
                    callee_name = raw_callee
                else:
                    callee_name = parsed.aliases.get(raw_callee, raw_callee)
                cache_key = (rel_posix, caller_parent, callee_name)
                if cache_key in resolve_cache:
                    target = resolve_cache[cache_key]
                else:
                    target = _resolve_target(
                        callee_name,
                        caller_node,
                        rel_posix,
                        import_paths,
                        by_file_and_name,
                        by_name,
                        file_meta,
                    )
                    resolve_cache[cache_key] = target
                if target and target.id != caller_id:
                    edge_counts[(caller_id, target.id)] += 1

    edges: list[CallEdge] = []
    file_edge_counts: Counter[tuple[str, str]] = Counter()
    for (src, tgt), count in sorted(edge_counts.items()):
        edges.append(CallEdge(source=src, target=tgt, call_count=count))
        src_node = node_map.get(src)
        tgt_node = node_map.get(tgt)
        if src_node:
            src_node.fan_out += 1
        if tgt_node:
            tgt_node.fan_in += 1
        if src_node and tgt_node and src_node.file != tgt_node.file:
            file_edge_counts[(src_node.file, tgt_node.file)] += count

    file_edges = [
        CallEdge(source=src_f, target=tgt_f, call_count=cnt)
        for (src_f, tgt_f), cnt in sorted(file_edge_counts.items())
    ]

    cycles = _find_cycles(all_nodes, edges)
    return CodeGraph(
        root=str(root),
        nodes=all_nodes,
        edges=edges,
        file_edges=file_edges,
        files=files,
        cycles=cycles,
        analyzer_engine="ast+treesitter" if has_treesitter() else "ast+structural",
    )
