"""Command-line interface for CodeAtlas."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
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
        f"Cycles: {stats['cycle_count']}"
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
    graph = scan_directory(root)
    if fmt == "mermaid":
        rendered = graph.to_mermaid()
    else:
        rendered = json.dumps(graph.to_dict(), indent=2)

    if output:
        Path(output).write_text(rendered, encoding="utf-8")
        print(f"Exported {fmt} graph to {output}")
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

    p_export = sub.add_parser("export", help="Export the call graph as JSON or Mermaid.")
    p_export.add_argument("path", nargs="?", default=".", help="Target repository path (default: .)")
    p_export.add_argument("--format", choices=("json", "mermaid"), default="json", help="Output format")
    p_export.add_argument("-o", "--output", default=None, help="Output file path (prints to stdout if omitted)")

    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    known_cmds = {"scan", "serve", "export"}
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
    if args.command == "serve":
        graph = scan_directory(target)
        serve_graph(
            target,
            graph,
            host=args.host,
            port=args.port,
            open_browser=not args.no_open,
        )
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
