"""Command-line interface for CodeAtlas."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .analysis import (
    compute_impact,
    dump_json,
    evaluate_check,
    format_check_text,
    format_impact_markdown,
    format_impact_text,
    load_config,
    parse_rule_spec,
)
from .mcp_server import serve_mcp_stdio
from .scanner import scan_directory
from .server import serve_graph


def _print_summary(root: Path) -> int:
    graph = scan_directory(root)
    stats = graph.stats()

    print(f"CodeAtlas v{__version__} — {graph.root}")
    print("-" * 60)
    print(
        f"Files: {stats['total_files']}  |  "
        f"Symbols: {stats['total_symbols']}  |  "
        f"Call Edges: {stats['total_edges']}  |  "
        f"Cycles: {stats['cycle_count']}  |  "
        f"Health: {stats['health_score']}/100"
    )
    if stats["languages"]:
        langs = ", ".join(f"{k}: {v}" for k, v in sorted(stats["languages"].items()))
        print(f"Languages: {langs}")

    if stats["hotspots"]:
        print("\nTop Call-Graph Hubs:")
        for hub in stats["hotspots"][:8]:
            print(
                f"  - {hub['name']} ({hub['file']})  "
                f"[in: {hub['fan_in']}, out: {hub['fan_out']}]"
            )

    if graph.cycles:
        print("\nCircular Call Groups:")
        for idx, comp in enumerate(graph.cycles[:5], start=1):
            print(f"  {idx}. " + " <-> ".join(comp[:5]))

    return 0


def _handle_export(root: Path, fmt: str, output: str | None) -> int:
    normalized = {"md": "markdown", "mmd": "mermaid"}.get(fmt, fmt)
    graph = scan_directory(root)
    if normalized == "mermaid":
        rendered = graph.to_mermaid()
    elif normalized == "html":
        rendered = graph.to_html()
    elif normalized == "markdown":
        rendered = graph.to_markdown()
    else:
        rendered = json.dumps(graph.to_dict(), indent=2)

    if output:
        Path(output).write_text(rendered, encoding="utf-8")
        print(f"Exported {normalized} graph to {output}")
    else:
        print(rendered)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codeatlas",
        description="Zero-config local codebase architecture & call-graph visualizer.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command")

    p_scan = sub.add_parser("scan", help="Print an architectural call-graph summary in the terminal.")
    p_scan.add_argument("path", nargs="?", default=".", help="Target repository path (default: .)")

    p_serve = sub.add_parser("serve", help="Launch the interactive Svelte call-graph UI.")
    p_serve.add_argument("path", nargs="?", default=".", help="Target repository path (default: .)")
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=4173, help="Bind port (default: 4173)")
    p_serve.add_argument("--no-open", action="store_true", help="Do not automatically open the browser.")

    p_export = sub.add_parser("export", help="Export the call graph as JSON, Mermaid, HTML, or Markdown.")
    p_export.add_argument("path", nargs="?", default=".", help="Target repository path (default: .)")
    p_export.add_argument(
        "--format",
        choices=("json", "mermaid", "mmd", "html", "markdown", "md"),
        default="json",
        help="Output format",
    )
    p_export.add_argument("-o", "--output", default=None, help="Output file path (prints to stdout if omitted)")

    p_impact = sub.add_parser("impact", help="Compute reverse-caller blast radius from git diff, files, or symbols.")
    p_impact.add_argument("path", nargs="?", default=".", help="Target repository path (default: .)")
    p_impact.add_argument("--diff", dest="git_ref", default=None, help="Git diff target/range (e.g. HEAD~1 or main...HEAD)")
    p_impact.add_argument("--file", dest="files", action="append", default=[], help="Changed file path (repeatable)")
    p_impact.add_argument("--symbol", dest="symbols", action="append", default=[], help="Changed symbol name/id (repeatable)")
    p_impact.add_argument("--depth", type=int, default=8, help="Max upstream traversal depth (default: 8)")
    p_impact.add_argument("--format", choices=("text", "json", "markdown", "md"), default="text", help="Output format")

    p_check = sub.add_parser("check", help="Run architectural CI checks (cycles, health score, boundary rules).")
    p_check.add_argument("path", nargs="?", default=".", help="Target repository path (default: .)")
    p_check.add_argument("--config", default=None, help="Path to .codeatlas.toml config file")
    p_check.add_argument("--max-cycles", type=int, default=None, help="Maximum allowed dependency cycles")
    p_check.add_argument("--min-health", type=int, default=None, help="Minimum required health score (0-100)")
    p_check.add_argument("--max-dead-ratio", type=float, default=None, help="Maximum allowed unreferenced callable ratio (0-1)")
    p_check.add_argument("--rule", dest="rules", action="append", default=[], help="Forbid edge FROM_GLOB:FORBID_GLOB (repeatable)")
    p_check.add_argument("--format", choices=("text", "json"), default="text", help="Output format")

    p_mcp = sub.add_parser("mcp", help="Start a zero-dependency stdio MCP server for AI coding agents.")
    p_mcp.add_argument("path", nargs="?", default=".", help="Target repository path (default: .)")

    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    known_cmds = {"scan", "serve", "export", "impact", "check", "mcp"}
    if raw_argv and not raw_argv[0].startswith("-") and raw_argv[0] not in known_cmds:
        raw_argv = ["scan", *raw_argv]

    parser = build_parser()
    args = parser.parse_args(raw_argv)

    if args.command is None:
        # Default convenience: `codeatlas [path]` behaves like `scan`
        return _print_summary(Path("."))

    target = Path(args.path)
    if not target.exists():
        print(f"Error: path does not exist: {target}", file=sys.stderr)
        return 1

    if args.command == "scan":
        return _print_summary(target)
    if args.command == "export":
        return _handle_export(target, args.format, args.output)
    if args.command == "impact":
        graph = scan_directory(target)
        impact = compute_impact(
            graph,
            changed_files=args.files or None,
            symbol_queries=args.symbols or None,
            git_ref=args.git_ref,
            use_git=not bool(args.files or args.symbols) or bool(args.git_ref),
            max_depth=max(1, min(20, args.depth)),
        )
        fmt = {"md": "markdown"}.get(args.format, args.format)
        if fmt == "json":
            print(dump_json(impact))
        elif fmt == "markdown":
            print(format_impact_markdown(impact))
        else:
            print(format_impact_text(impact))
        return 0
    if args.command == "check":
        try:
            cfg = load_config(target, Path(args.config) if args.config else None)
            for raw_rule in args.rules:
                cfg.rules.append(parse_rule_spec(raw_rule))
        except (FileNotFoundError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        if args.max_cycles is not None:
            cfg.max_cycles = args.max_cycles
        if args.min_health is not None:
            cfg.min_health = args.min_health
        if args.max_dead_ratio is not None:
            cfg.max_dead_ratio = args.max_dead_ratio

        graph = scan_directory(target)
        res = evaluate_check(graph, cfg)
        if args.format == "json":
            print(dump_json(res))
        else:
            print(format_check_text(res))
        return 0 if res["passed"] else 2
    if args.command == "mcp":
        return serve_mcp_stdio(target)
    if args.command == "serve":
        graph = scan_directory(target)
        try:
            serve_graph(
                target,
                graph,
                host=args.host,
                port=args.port,
                open_browser=not args.no_open,
            )
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
