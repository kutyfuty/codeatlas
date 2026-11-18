"""Architectural rules, git blast-radius impact analysis, and graph query helpers."""

from __future__ import annotations

import fnmatch
import json
import subprocess
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import CodeGraph, SymbolNode

try:  # Python 3.11+
    import tomllib as _tomllib
except ModuleNotFoundError:  # Python 3.10 fallback
    _tomllib = None


@dataclass(slots=True)
class AtlasRule:
    """Architectural boundary rule forbidding calls between file glob patterns."""

    name: str
    from_pattern: str
    forbid_pattern: str


@dataclass(slots=True)
class AtlasConfig:
    """Configuration loaded from `.codeatlas.toml` and/or CLI overrides."""

    max_cycles: int = 0
    min_health: int = 0
    max_dead_ratio: float | None = None
    rules: list[AtlasRule] = field(default_factory=list)


def _strip_toml_value(raw: str) -> Any:
    val = raw.strip()
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        return val[1:-1]
    lower = val.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


def _parse_toml_fallback(text: str) -> dict[str, Any]:
    """Minimal TOML subset parser supporting `[check]` and `[[rules]]` on Python 3.10."""
    data: dict[str, Any] = {}
    section: dict[str, Any] = data
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[[") and line.endswith("]]"):
            key = line[2:-2].strip()
            lst = data.setdefault(key, [])
            if isinstance(lst, list):
                item: dict[str, Any] = {}
                lst.append(item)
                section = item
            continue
        if line.startswith("[") and line.endswith("]"):
            key = line[1:-1].strip()
            sec = data.setdefault(key, {})
            if isinstance(sec, dict):
                section = sec
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            section[k.strip()] = _strip_toml_value(v)
    return data


def parse_rule_spec(spec: str) -> AtlasRule:
    """Parse a CLI boundary rule formatted as `FROM_GLOB:FORBID_GLOB`."""
    if ":" not in spec:
        raise ValueError(f"Invalid rule '{spec}'. Expected FROM_GLOB:FORBID_GLOB")
    src, dst = spec.split(":", 1)
    src = src.strip()
    dst = dst.strip()
    if not src or not dst:
        raise ValueError(f"Invalid rule '{spec}'. Both patterns must be non-empty")
    return AtlasRule(name=f"{src} -> {dst}", from_pattern=src, forbid_pattern=dst)


def load_config(root: Path, config_path: Path | None = None) -> AtlasConfig:
    """Load `.codeatlas.toml` from project root if present."""
    cfg_file = config_path if config_path is not None else (root / ".codeatlas.toml")
    if config_path is not None and not cfg_file.is_file():
        raise FileNotFoundError(f"Config file not found: {cfg_file}")
    if not cfg_file.is_file():
        return AtlasConfig()

    raw_text = cfg_file.read_text(encoding="utf-8")
    parsed: dict[str, Any]
    if _tomllib is not None:
        parsed = _tomllib.loads(raw_text)
    else:
        parsed = _parse_toml_fallback(raw_text)

    chk = parsed.get("check") or {}
    rules: list[AtlasRule] = []
    for idx, r in enumerate(parsed.get("rules") or [], start=1):
        if not isinstance(r, dict):
            continue
        src = str(r.get("from", "")).strip()
        dst = str(r.get("forbid", "")).strip()
        if src and dst:
            rules.append(
                AtlasRule(
                    name=str(r.get("name") or f"Rule #{idx}: {src} -> {dst}"),
                    from_pattern=src,
                    forbid_pattern=dst,
                )
            )

    raw_dead = chk.get("max_dead_ratio")
    return AtlasConfig(
        max_cycles=int(chk.get("max_cycles", 0)),
        min_health=int(chk.get("min_health", 0)),
        max_dead_ratio=float(raw_dead) if raw_dead is not None else None,
        rules=rules,
    )


def _match_glob(path: str, pattern: str) -> bool:
    norm_path = path.replace("\\", "/").lstrip("./")
    norm_pat = pattern.replace("\\", "/").lstrip("./")
    if norm_pat.endswith("/"):
        norm_pat += "*"
    return fnmatch.fnmatch(norm_path, norm_pat) or fnmatch.fnmatch(f"/{norm_path}", f"/{norm_pat}")


def evaluate_check(graph: CodeGraph, config: AtlasConfig) -> dict[str, Any]:
    """Evaluate architectural gates and boundary rules against a scanned CodeGraph."""
    st = graph.stats()
    violations: list[dict[str, Any]] = []

    if len(graph.cycles) > config.max_cycles:
        for idx, cyc in enumerate(graph.cycles, start=1):
            chain = " -> ".join(c.split("::")[-1] for c in cyc[:6])
            violations.append({
                "kind": "cycle",
                "severity": "error",
                "message": f"Circular dependency #{idx} ({len(cyc)} symbols): {chain}",
                "symbols": cyc,
            })

    if st["health_score"] < config.min_health:
        violations.append({
            "kind": "health",
            "severity": "error",
            "message": f"Health score {st['health_score']}/100 is below minimum {config.min_health}/100",
            "actual": st["health_score"],
            "minimum": config.min_health,
        })

    callable_count = sum(1 for n in graph.nodes if n.kind != "class")
    dead_ratio = (st["unreferenced_count"] / callable_count) if callable_count else 0.0
    if config.max_dead_ratio is not None and dead_ratio > config.max_dead_ratio:
        violations.append({
            "kind": "dead_ratio",
            "severity": "error",
            "message": (
                f"Unreferenced symbol ratio {dead_ratio:.2f} exceeds "
                f"max_dead_ratio {config.max_dead_ratio:.2f}"
            ),
            "actual": round(dead_ratio, 4),
            "maximum": config.max_dead_ratio,
        })

    node_map = {n.id: n for n in graph.nodes}
    for rule in config.rules:
        seen_pairs: set[tuple[str, str]] = set()
        for edge in graph.edges:
            src = node_map.get(edge.source)
            tgt = node_map.get(edge.target)
            if not src or not tgt or src.file == tgt.file:
                continue
            if _match_glob(src.file, rule.from_pattern) and _match_glob(tgt.file, rule.forbid_pattern):
                pair = (src.id, tgt.id)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                violations.append({
                    "kind": "rule",
                    "severity": "error",
                    "rule": rule.name,
                    "source": src.id,
                    "target": tgt.id,
                    "source_file": src.file,
                    "target_file": tgt.file,
                    "line": src.start_line,
                    "message": (
                        f"Rule '{rule.name}': {src.file}:{src.start_line} ({src.name}) "
                        f"calls forbidden target {tgt.file} ({tgt.name})"
                    ),
                })

    return {
        "passed": len(violations) == 0,
        "violation_count": len(violations),
        "violations": violations,
        "stats": st,
        "config": {
            "max_cycles": config.max_cycles,
            "min_health": config.min_health,
            "max_dead_ratio": config.max_dead_ratio,
            "rule_count": len(config.rules),
        },
    }


def _run_git(root: Path, args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
        return proc.returncode, proc.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def git_changed_files(root: Path, git_ref: str | None = None) -> tuple[list[str], str]:
    """Detect changed repository files via git diff or working tree status."""
    rc, inside = _run_git(root, ["rev-parse", "--is-inside-work-tree"])
    if rc != 0 or inside.strip() != "true":
        return [], "not-a-git-repo"

    paths: list[str] = []
    seen: set[str] = set()

    def add_lines(raw: str) -> None:
        for line in raw.splitlines():
            p = line.strip().replace("\\", "/")
            if p and p not in seen:
                seen.add(p)
                paths.append(p)

    if git_ref:
        rc, out = _run_git(root, ["diff", "--name-only", "--relative", git_ref])
        if rc == 0:
            add_lines(out)
        return paths, f"git:{git_ref}"

    rc1, out_unstaged = _run_git(root, ["diff", "--name-only", "--relative"])
    rc2, out_staged = _run_git(root, ["diff", "--name-only", "--relative", "--cached"])
    rc3, out_untracked = _run_git(root, ["ls-files", "--others", "--exclude-standard"])
    if rc1 == 0:
        add_lines(out_unstaged)
    if rc2 == 0:
        add_lines(out_staged)
    if rc3 == 0:
        add_lines(out_untracked)

    if paths:
        return paths, "git:working-tree"

    # Fall back to the latest commit when the working tree is clean.
    rc4, out_head = _run_git(root, ["diff", "--name-only", "--relative", "HEAD~1", "HEAD"])
    if rc4 == 0:
        add_lines(out_head)
        return paths, "git:last-commit"

    return paths, "git:clean"


def is_test_path(rel_path: str) -> bool:
    """Heuristic check for test files across Python, JS/TS, Go, Rust, Java, and C++."""
    norm = rel_path.replace("\\", "/").lower()
    parts = norm.split("/")
    fname = parts[-1]
    if any(p in {"tests", "test", "__tests__", "spec", "e2e"} for p in parts[:-1]):
        return True
    return (
        fname.startswith("test_")
        or fname.endswith("_test.py")
        or fname.endswith("_test.go")
        or ".test." in fname
        or ".spec." in fname
    )


def resolve_symbols(graph: CodeGraph, query: str) -> list[SymbolNode]:
    """Resolve a symbol id (`file::name`) or symbol name cleanly."""
    q = query.strip()
    if not q:
        return []
    exact = [n for n in graph.nodes if n.id == q]
    if exact:
        return exact
    by_name = [n for n in graph.nodes if n.name == q]
    if by_name:
        return by_name
    q_lower = q.lower()
    return [n for n in graph.nodes if q_lower in n.id.lower()]


def shortest_path(graph: CodeGraph, source_query: str, target_query: str) -> dict[str, Any]:
    """Find shortest directed call chain between two symbols via multi-source BFS."""
    sources = resolve_symbols(graph, source_query)
    targets = resolve_symbols(graph, target_query)
    if not sources:
        return {"found": False, "error": f"Source symbol not found: {source_query}", "path": []}
    if not targets:
        return {"found": False, "error": f"Target symbol not found: {target_query}", "path": []}

    target_ids = {t.id for t in targets}
    node_map = {n.id: n for n in graph.nodes}

    adj: dict[str, list[str]] = defaultdict(list)
    for e in graph.edges:
        adj[e.source].append(e.target)

    queue: deque[list[str]] = deque([[s.id] for s in sources[:10]])
    visited = {s.id for s in sources[:10]}
    while queue:
        path = queue.popleft()
        cur = path[-1]
        if cur in target_ids:
            return {
                "found": True,
                "hops": len(path) - 1,
                "path": [
                    {
                        "id": nid,
                        "name": node_map[nid].name,
                        "file": node_map[nid].file,
                        "line": node_map[nid].start_line,
                    }
                    for nid in path
                    if nid in node_map
                ],
            }
        for nxt in adj.get(cur, ()):
            if nxt not in visited:
                visited.add(nxt)
                queue.append([*path, nxt])

    return {"found": False, "hops": None, "path": []}


def _match_changed_files(graph: CodeGraph, candidates: list[str]) -> list[str]:
    known = [f.path for f in graph.files]
    known_set = set(known)
    matched: list[str] = []
    seen: set[str] = set()
    root_path = Path(graph.root)

    for raw in candidates:
        norm = raw.strip().replace("\\", "/")
        if not norm:
            continue
        p = Path(raw)
        if p.is_absolute():
            try:
                norm = p.resolve().relative_to(root_path.resolve()).as_posix()
            except ValueError:
                pass
        norm = norm.lstrip("./")
        if norm in known_set:
            if norm not in seen:
                seen.add(norm)
                matched.append(norm)
            continue
        # Allow partial path or directory prefix match when unique/useful.
        for k in known:
            if (k == norm or k.startswith(f"{norm.rstrip('/')}/") or k.endswith(f"/{norm}")) and k not in seen:
                seen.add(k)
                matched.append(k)
    return matched


def compute_impact(
    graph: CodeGraph,
    *,
    changed_files: list[str] | None = None,
    symbol_queries: list[str] | None = None,
    git_ref: str | None = None,
    use_git: bool = True,
    max_depth: int = 8,
) -> dict[str, Any]:
    """Compute reverse-caller blast radius for changed files, symbols, or git diff."""
    source_label = "explicit"
    raw_files = list(changed_files or [])
    if not raw_files and not symbol_queries and use_git:
        raw_files, source_label = git_changed_files(Path(graph.root), git_ref=git_ref)
    elif git_ref and use_git:
        raw_files, source_label = git_changed_files(Path(graph.root), git_ref=git_ref)

    matched_files = _match_changed_files(graph, raw_files)
    matched_file_set = set(matched_files)

    seed_nodes: dict[str, SymbolNode] = {}
    for node in graph.nodes:
        if node.file in matched_file_set:
            seed_nodes[node.id] = node

    if symbol_queries:
        for q in symbol_queries:
            for node in resolve_symbols(graph, q):
                seed_nodes[node.id] = node
                matched_file_set.add(node.file)

    matched_files = sorted(matched_file_set)
    node_map = {n.id: n for n in graph.nodes}

    callers_map: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        if edge.source != edge.target:
            callers_map[edge.target].append(edge.source)

    visited_hop: dict[str, int] = {nid: 0 for nid in seed_nodes}
    via_map: dict[str, str] = {}
    queue: deque[str] = deque(seed_nodes.keys())

    while queue:
        cur = queue.popleft()
        cur_hop = visited_hop[cur]
        if cur_hop >= max_depth:
            continue
        for caller_id in callers_map.get(cur, ()):
            if caller_id not in visited_hop:
                visited_hop[caller_id] = cur_hop + 1
                via_map[caller_id] = cur
                queue.append(caller_id)

    impacted_symbols: list[dict[str, Any]] = []
    affected_entrypoints: list[dict[str, Any]] = []
    affected_tests: list[dict[str, Any]] = []
    impacted_files_set = set(matched_files)

    for nid, hop in visited_hop.items():
        node = node_map.get(nid)
        if not node:
            continue
        impacted_files_set.add(node.file)
        entry = {
            "id": node.id,
            "name": node.name,
            "file": node.file,
            "kind": node.kind,
            "language": node.language,
            "start_line": node.start_line,
            "hop": hop,
            "fan_in": node.fan_in,
            "fan_out": node.fan_out,
            "via": via_map.get(nid),
        }
        if hop > 0:
            impacted_symbols.append(entry)

        is_test = is_test_path(node.file) or node.name.startswith("test_")
        if is_test and node.kind != "class":
            affected_tests.append(entry)
        elif node.kind != "class" and node.fan_in == 0:
            affected_entrypoints.append(entry)

    impacted_symbols.sort(key=lambda item: (item["hop"], -item["fan_in"], item["id"]))
    affected_entrypoints.sort(key=lambda item: (item["hop"], item["id"]))
    affected_tests.sort(key=lambda item: (item["hop"], item["id"]))

    return {
        "source": source_label,
        "changed_files": matched_files,
        "changed_symbols": [
            {
                "id": n.id,
                "name": n.name,
                "file": n.file,
                "kind": n.kind,
                "language": n.language,
                "start_line": n.start_line,
            }
            for n in sorted(seed_nodes.values(), key=lambda x: x.id)
        ],
        "impacted_symbols": impacted_symbols,
        "impacted_files": sorted(impacted_files_set),
        "affected_entrypoints": affected_entrypoints,
        "affected_tests": affected_tests,
        "summary": {
            "changed_file_count": len(matched_files),
            "changed_symbol_count": len(seed_nodes),
            "impacted_symbol_count": len(impacted_symbols),
            "impacted_file_count": len(impacted_files_set),
            "entrypoint_count": len(affected_entrypoints),
            "test_count": len(affected_tests),
            "max_depth": max_depth,
        },
    }


def format_impact_markdown(impact: dict[str, Any]) -> str:
    """Format blast-radius analysis for PR comments or Markdown summaries."""
    s = impact["summary"]
    lines = [
        "### CodeAtlas Change Impact (Blast Radius)",
        "",
        f"- **Source:** `{impact['source']}`",
        f"- **Changed Files:** {s['changed_file_count']} | **Direct Symbols:** {s['changed_symbol_count']} | "
        f"**Upstream Impacted Symbols:** {s['impacted_symbol_count']} | **Entrypoints Affected:** {s['entrypoint_count']} | "
        f"**Tests Reaching Changes:** {s['test_count']}",
    ]
    if impact["affected_entrypoints"]:
        lines.extend(["", "#### Affected Entrypoints"])
        for ep in impact["affected_entrypoints"][:12]:
            lines.append(f"- `{ep['name']}` (`{ep['file']}:{ep['start_line']}`, hop {ep['hop']})")
    if impact["impacted_symbols"]:
        lines.extend(["", "#### Upstream Callers"])
        for sym in impact["impacted_symbols"][:20]:
            lines.append(f"- [hop {sym['hop']}] `{sym['name']}` (`{sym['file']}:{sym['start_line']}`)")
    if impact["affected_tests"]:
        lines.extend(["", "#### Related Tests"])
        for t in impact["affected_tests"][:15]:
            lines.append(f"- `{t['name']}` (`{t['file']}`)")
    return "\n".join(lines)


def format_impact_text(impact: dict[str, Any]) -> str:
    """Format blast-radius analysis cleanly for terminal output."""
    s = impact["summary"]
    lines = [
        f"CodeAtlas Impact ({impact['source']})",
        "-" * 60,
        (
            f"Changed Files: {s['changed_file_count']}  |  Direct Symbols: {s['changed_symbol_count']}  |  "
            f"Upstream Callers: {s['impacted_symbol_count']}  |  Entrypoints: {s['entrypoint_count']}  |  "
            f"Tests: {s['test_count']}"
        ),
    ]
    if impact["changed_files"]:
        lines.append("\nChanged Files:")
        for f in impact["changed_files"][:15]:
            lines.append(f"  * {f}")
    if impact["affected_entrypoints"]:
        lines.append("\nAffected Entrypoints:")
        for ep in impact["affected_entrypoints"][:12]:
            lines.append(f"  - {ep['name']} ({ep['file']}:{ep['start_line']}) [hop {ep['hop']}]")
    if impact["impacted_symbols"]:
        lines.append("\nUpstream Blast Radius:")
        for sym in impact["impacted_symbols"][:20]:
            via = f" via {sym['via'].split('::')[-1]}" if sym.get("via") else ""
            lines.append(f"  - [hop {sym['hop']}] {sym['name']} ({sym['file']}:{sym['start_line']}){via}")
    if impact["affected_tests"]:
        lines.append("\nTests Reaching Changed Code:")
        for t in impact["affected_tests"][:15]:
            lines.append(f"  - {t['name']} ({t['file']})")
    return "\n".join(lines)


def format_check_text(result: dict[str, Any]) -> str:
    """Format architectural gate check report for terminal/CI output."""
    st = result["stats"]
    status = "PASSED" if result["passed"] else "FAILED"
    lines = [
        f"CodeAtlas Check: {status}",
        "-" * 60,
        (
            f"Files: {st['total_files']}  |  Symbols: {st['total_symbols']}  |  "
            f"Cycles: {st['cycle_count']}  |  Health: {st['health_score']}/100  |  "
            f"Violations: {result['violation_count']}"
        ),
    ]
    if result["violations"]:
        lines.append("\nViolations:")
        for v in result["violations"]:
            lines.append(f"  [x] {v['message']}")
    return "\n".join(lines)


def dump_json(payload: Any) -> str:
    return json.dumps(payload, indent=2)
