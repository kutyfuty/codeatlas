"""Zero-dependency Model Context Protocol (MCP) stdio server for CodeAtlas."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .analysis import compute_impact, evaluate_check, load_config, resolve_symbols, shortest_path
from .models import CodeGraph
from .scanner import scan_directory

PROTOCOL_VERSION = "2024-11-05"


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "get_overview",
            "description": "Get repository call-graph summary: file/symbol/edge counts, languages, health score, top hubs, and cycles.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "search_symbols",
            "description": "Search functions, methods, or classes by name or file path.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Substring or symbol name to search"},
                    "language": {"type": "string", "description": "Optional language filter"},
                    "limit": {"type": "integer", "default": 25},
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_callers",
            "description": "Find all functions/methods calling a given symbol id or name.",
            "inputSchema": {
                "type": "object",
                "properties": {"symbol": {"type": "string", "description": "Symbol id or name"}},
                "required": ["symbol"],
            },
        },
        {
            "name": "get_callees",
            "description": "Find all functions/methods called by a given symbol id or name.",
            "inputSchema": {
                "type": "object",
                "properties": {"symbol": {"type": "string", "description": "Symbol id or name"}},
                "required": ["symbol"],
            },
        },
        {
            "name": "trace_path",
            "description": "Find shortest directed call path between two symbols via BFS.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Source symbol id or name"},
                    "target": {"type": "string", "description": "Target symbol id or name"},
                },
                "required": ["source", "target"],
            },
        },
        {
            "name": "blast_radius",
            "description": "Compute upstream blast radius (impacted callers, entrypoints, tests) for a file, symbol, or git diff.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Optional relative file path"},
                    "symbol": {"type": "string", "description": "Optional symbol id or name"},
                    "git_ref": {"type": "string", "description": "Optional git diff ref"},
                    "max_depth": {"type": "integer", "default": 8},
                },
            },
        },
        {
            "name": "find_cycles_and_dead_code",
            "description": "List circular call dependencies, unreferenced symbols, and architectural rule violations.",
            "inputSchema": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 30}},
            },
        },
        {
            "name": "rescan",
            "description": "Rescan the repository from disk to refresh the call graph after edits.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ]


class CodeAtlasMCPServer:
    """Stateful MCP request handler backed by a scanned CodeGraph."""

    def __init__(self, root: Path, graph: CodeGraph | None = None) -> None:
        self.root = root.resolve()
        self.graph = graph if graph is not None else scan_directory(self.root)

    def _neighbors(self, query: str, direction: str) -> dict[str, Any]:
        matches = resolve_symbols(self.graph, query)
        if not matches:
            return {"found": False, "error": f"Symbol not found: {query}", "matches": []}

        node_map = {n.id: n for n in self.graph.nodes}
        results: list[dict[str, Any]] = []
        for sym in matches[:5]:
            related: list[dict[str, Any]] = []
            for edge in self.graph.edges:
                peer_id = edge.source if (direction == "in" and edge.target == sym.id) else (
                    edge.target if (direction == "out" and edge.source == sym.id) else None
                )
                if peer_id and peer_id in node_map:
                    peer = node_map[peer_id]
                    related.append({
                        "id": peer.id,
                        "name": peer.name,
                        "file": peer.file,
                        "line": peer.start_line,
                        "call_count": edge.call_count,
                    })
            key = "callers" if direction == "in" else "callees"
            results.append({
                "symbol": sym.id,
                "file": sym.file,
                "line": sym.start_line,
                key: related,
            })
        return {"found": True, "results": results}

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute an MCP tool and return structured payload."""
        if name == "rescan":
            self.graph = scan_directory(self.root)
            return {"rescanned": True, "stats": self.graph.stats()}

        if name == "get_overview":
            return {"root": self.graph.root, "stats": self.graph.stats(), "cycles": self.graph.cycles}

        if name == "search_symbols":
            q = str(arguments.get("query") or "").strip().lower()
            lang = str(arguments.get("language") or "").strip().lower()
            limit = max(1, min(100, int(arguments.get("limit") or 25)))
            hits = [
                n.to_dict()
                for n in self.graph.nodes
                if (not lang or n.language.lower() == lang)
                and (not q or q in n.name.lower() or q in n.file.lower())
            ]
            hits.sort(key=lambda item: (item["fan_in"] + item["fan_out"]), reverse=True)
            return {"total_matches": len(hits), "symbols": hits[:limit]}

        if name == "get_callers":
            return self._neighbors(str(arguments.get("symbol", "")), "in")

        if name == "get_callees":
            return self._neighbors(str(arguments.get("symbol", "")), "out")

        if name == "trace_path":
            return shortest_path(
                self.graph,
                str(arguments.get("source", "")),
                str(arguments.get("target", "")),
            )

        if name == "blast_radius":
            file_arg = arguments.get("file")
            sym_arg = arguments.get("symbol")
            git_ref = arguments.get("git_ref")
            max_depth = max(1, min(20, int(arguments.get("max_depth") or 8)))
            return compute_impact(
                self.graph,
                changed_files=[str(file_arg)] if file_arg else None,
                symbol_queries=[str(sym_arg)] if sym_arg else None,
                git_ref=str(git_ref) if git_ref else None,
                use_git=not bool(file_arg or sym_arg),
                max_depth=max_depth,
            )

        if name == "find_cycles_and_dead_code":
            limit = max(1, min(100, int(arguments.get("limit") or 30)))
            dead = [
                {
                    "id": n.id,
                    "name": n.name,
                    "kind": n.kind,
                    "file": n.file,
                    "line": n.start_line,
                }
                for n in self.graph.nodes
                if n.kind != "class" and n.fan_in == 0
            ][:limit]
            cfg = load_config(self.root)
            chk = evaluate_check(self.graph, cfg)
            return {
                "cycles": self.graph.cycles,
                "unreferenced": dead,
                "rule_violations": chk["violations"],
            }

        raise KeyError(f"Unknown tool: {name}")

    def handle_message(self, msg: Any) -> dict[str, Any] | None:
        """Process one JSON-RPC 2.0 request or notification."""
        if not isinstance(msg, dict):
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request: expected JSON object"},
            }
        method = msg.get("method", "")
        msg_id = msg.get("id")
        raw_params = msg.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}

        # Notifications do not carry `id` and expect no JSON-RPC response.
        if msg_id is None:
            return None

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "codeatlas", "version": __version__},
                },
            }

        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": _tool_definitions()}}

        if method == "tools/call":
            tool_name = str(params.get("name", ""))
            raw_args = params.get("arguments")
            tool_args = raw_args if isinstance(raw_args, dict) else {}
            try:
                payload = self.call_tool(tool_name, tool_args)
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
                        "isError": False,
                    },
                }
            except KeyError as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": str(exc)},
                }
            except Exception as exc:  # pragma: no cover - defensive error guard
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
                        "isError": True,
                    },
                }

        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }


def _read_message(stream: Any) -> tuple[dict[str, Any] | None, bool]:
    """Read either newline-delimited JSON or Content-Length framed JSON-RPC message."""
    raw_bin = getattr(stream, "buffer", None)
    if raw_bin is not None:
        while True:
            raw_line = raw_bin.readline()
            if raw_line == b"":
                return None, False
            stripped_b = raw_line.strip()
            if not stripped_b:
                continue
            if stripped_b.lower().startswith(b"content-length:"):
                length = int(stripped_b.split(b":", 1)[1].strip())
                if length < 0:
                    raise ValueError("Negative Content-Length")
                while True:
                    hdr = raw_bin.readline()
                    if hdr in (b"", b"\n", b"\r\n"):
                        break
                body_b = raw_bin.read(length)
                if not body_b:
                    return None, True
                return json.loads(body_b.decode("utf-8")), True
            return json.loads(stripped_b.decode("utf-8")), False

    while True:
        line = stream.readline()
        if line == "":
            return None, False
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.lower().startswith("content-length:"):
            length = int(stripped.split(":", 1)[1].strip())
            if length < 0:
                raise ValueError("Negative Content-Length")
            while True:
                hdr = stream.readline()
                if hdr in ("", "\n", "\r\n"):
                    break
            body = stream.read(length)
            if not body:
                return None, True
            return json.loads(body), True

        return json.loads(stripped), False


def serve_mcp_stdio(
    root: Path,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Run blocking stdio MCP loop until EOF."""
    in_stream = stdin if stdin is not None else sys.stdin
    out_stream = stdout if stdout is not None else sys.stdout
    out_bin = getattr(out_stream, "buffer", None)
    server = CodeAtlasMCPServer(root)

    def write_bytes_or_text(payload_str: str) -> None:
        if out_bin is not None:
            out_bin.write(payload_str.encode("utf-8"))
            out_bin.flush()
        else:
            out_stream.write(payload_str)
            out_stream.flush()

    while True:
        try:
            msg, framed = _read_message(in_stream)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            err_resp = json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}})
            write_bytes_or_text(err_resp + "\n")
            continue

        if msg is None:
            break

        resp = server.handle_message(msg)
        if resp is not None:
            encoded = json.dumps(resp)
            if framed:
                byte_len = len(encoded.encode("utf-8"))
                write_bytes_or_text(f"Content-Length: {byte_len}\r\n\r\n{encoded}")
            else:
                write_bytes_or_text(encoded + "\n")

    return 0
