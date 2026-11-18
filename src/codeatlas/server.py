"""Local HTTP server for the CodeAtlas interactive Svelte UI and JSON API."""

from __future__ import annotations

import json
import mimetypes
import socket
import threading
import webbrowser
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .analysis import compute_impact, evaluate_check, load_config
from .models import CodeGraph
from .scanner import scan_directory

WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_PREVIEW_BYTES = 2_000_000


class _AppState:
    def __init__(self, root: Path, graph: CodeGraph) -> None:
        self.root = root.resolve()
        self.graph = graph
        self.lock = threading.Lock()

    def rescan(self) -> CodeGraph:
        fresh = scan_directory(self.root)
        with self.lock:
            self.graph = fresh
        return fresh


class _AtlasHandler(BaseHTTPRequestHandler):
    def __init__(self, *args: Any, state: _AppState, **kwargs: Any) -> None:
        self.state = state
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Keep terminal output clean; CLI prints startup info.
        return

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_file_bytes(self, content: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path

        if route == "/api/health":
            self._send_json({"status": "ok"})
            return

        if route == "/api/graph":
            qs = parse_qs(parsed.query)
            if qs.get("rescan") == ["1"]:
                graph = self.state.rescan()
            else:
                with self.state.lock:
                    graph = self.state.graph
            self._send_json(graph.to_dict())
            return

        if route == "/api/file":
            self._handle_file_snippet(parse_qs(parsed.query))
            return

        if route == "/api/impact":
            self._handle_impact(parse_qs(parsed.query))
            return

        if route == "/api/check":
            self._handle_check(parse_qs(parsed.query))
            return

        self._serve_static(route)

    def _handle_impact(self, qs: dict[str, list[str]]) -> None:
        files = [f for f in qs.get("file", []) if f]
        symbols = [s for s in qs.get("symbol", []) if s]
        diff_ref = (qs.get("diff") or [""])[0].strip() or None
        use_git = (qs.get("git") or ["1"])[0] != "0"
        try:
            max_depth = max(1, min(20, int((qs.get("depth") or ["8"])[0])))
        except ValueError:
            self._send_json({"error": "Invalid depth parameter"}, HTTPStatus.BAD_REQUEST)
            return

        with self.state.lock:
            graph = self.state.graph

        payload = compute_impact(
            graph,
            changed_files=files or None,
            symbol_queries=symbols or None,
            git_ref=diff_ref,
            use_git=use_git and not (files or symbols),
            max_depth=max_depth,
        )
        self._send_json(payload)

    def _handle_check(self, qs: dict[str, list[str]]) -> None:
        with self.state.lock:
            graph = self.state.graph
        cfg = load_config(self.state.root)
        if qs.get("max_cycles"):
            try:
                cfg.max_cycles = int(qs["max_cycles"][0])
            except ValueError:
                self._send_json({"error": "Invalid max_cycles"}, HTTPStatus.BAD_REQUEST)
                return
        if qs.get("min_health"):
            try:
                cfg.min_health = int(qs["min_health"][0])
            except ValueError:
                self._send_json({"error": "Invalid min_health"}, HTTPStatus.BAD_REQUEST)
                return
        self._send_json(evaluate_check(graph, cfg))

    def _handle_file_snippet(self, qs: dict[str, list[str]]) -> None:
        rel = (qs.get("path") or [""])[0]
        if not rel:
            self._send_json({"error": "Missing 'path' query parameter"}, HTTPStatus.BAD_REQUEST)
            return

        target = (self.state.root / rel).resolve()
        try:
            target.relative_to(self.state.root)
        except ValueError:
            self._send_json({"error": "Access outside project root denied"}, HTTPStatus.FORBIDDEN)
            return

        if not target.is_file():
            self._send_json({"error": "File not found"}, HTTPStatus.NOT_FOUND)
            return

        if target.stat().st_size > MAX_PREVIEW_BYTES:
            self._send_json({"error": "File exceeds 2 MB preview limit"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return

        try:
            all_lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        total = len(all_lines)
        try:
            start = max(1, int((qs.get("start") or ["1"])[0]))
            end = min(total, int((qs.get("end") or [str(total)])[0]))
        except ValueError:
            self._send_json({"error": "Invalid line range"}, HTTPStatus.BAD_REQUEST)
            return

        snippet = "\n".join(all_lines[start - 1 : end]) if total > 0 and start <= end else ""
        self._send_json({
            "path": rel,
            "start_line": start,
            "end_line": end,
            "total_lines": total,
            "content": snippet,
        })

    def _serve_static(self, route: str) -> None:
        if not WEB_DIR.is_dir():
            msg = b"<h1>CodeAtlas Web UI not built</h1><p>Run npm run build in frontend/.</p>"
            self._send_file_bytes(msg, "text/html; charset=utf-8", HTTPStatus.SERVICE_UNAVAILABLE)
            return

        rel = route.lstrip("/") or "index.html"
        candidate = (WEB_DIR / rel).resolve()
        try:
            candidate.relative_to(WEB_DIR.resolve())
        except ValueError:
            self._send_json({"error": "Forbidden"}, HTTPStatus.FORBIDDEN)
            return

        if not candidate.is_file():
            # Return 404 for missing asset files; only use SPA fallback for extensionless routes.
            if rel.startswith("assets/") or Path(rel).suffix:
                self._send_json({"error": "Asset not found"}, HTTPStatus.NOT_FOUND)
                return
            candidate = WEB_DIR / "index.html"

        mime, _ = mimetypes.guess_type(str(candidate))
        content_type = mime or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"

        self._send_file_bytes(candidate.read_bytes(), content_type)


class _AtlasHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows, SO_REUSEADDR allows port hijacking and conflicts with SO_EXCLUSIVEADDRUSE.
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def create_server(root: Path, graph: CodeGraph, host: str = "127.0.0.1", port: int = 4173) -> ThreadingHTTPServer:
    """Create a configured ThreadingHTTPServer without starting its loop."""
    state = _AppState(root=root, graph=graph)
    handler = partial(_AtlasHandler, state=state)
    return _AtlasHTTPServer((host, port), handler)


def serve_graph(
    root: Path,
    graph: CodeGraph,
    host: str = "127.0.0.1",
    port: int = 4173,
    open_browser: bool = True,
) -> None:
    """Start the local CodeAtlas server and block until Ctrl+C."""
    server: ThreadingHTTPServer | None = None
    last_err: OSError | None = None
    for candidate_port in range(port, port + 11):
        try:
            server = create_server(root, graph, host=host, port=candidate_port)
            if candidate_port != port:
                print(f"Port {port} busy; switched to port {candidate_port}.", flush=True)
            break
        except OSError as exc:
            last_err = exc
    if server is None:
        raise RuntimeError(f"Could not bind HTTP server on ports {port}-{port + 10}: {last_err}")

    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}"
    print(f"CodeAtlas UI running at {url}  (Press Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down CodeAtlas server.")
    finally:
        server.server_close()
