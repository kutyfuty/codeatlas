"""Integration tests for scanner, CLI, and HTTP server."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from codeatlas.cli import main
from codeatlas.scanner import scan_directory
from codeatlas.server import create_server


def _build_sample_repo(tmp_path: Path) -> Path:
    (tmp_path / ".gitignore").write_text("/ignored_dir/\n", encoding="utf-8")
    ignored = tmp_path / "ignored_dir"
    ignored.mkdir()
    (ignored / "skip.py").write_text("def should_not_appear():\n    pass\n", encoding="utf-8")

    (tmp_path / "alpha.py").write_text(
        "from beta import ping\n\ndef start():\n    return ping()\n\ndef pong():\n    return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "beta.py").write_text(
        "from alpha import pong\n\ndef ping():\n    return pong()\n",
        encoding="utf-8",
    )
    (tmp_path / "bundle.js").write_text(
        "/* License line 1 */\n/* License line 2 */\n/* License line 3 */\n"
        + "function x(){return 1;}" + ("a();" * 300),
        encoding="utf-8",
    )
    return tmp_path


def test_scanner_resolves_cross_file_edges_and_respects_gitignore(tmp_path: Path) -> None:
    repo = _build_sample_repo(tmp_path)
    graph = scan_directory(repo)

    sym_names = {s.name for s in graph.nodes}
    assert {"start", "ping", "pong"} <= sym_names
    assert "should_not_appear" not in sym_names

    edge_pairs = {(e.source, e.target) for e in graph.edges}
    assert ("alpha.py::start", "beta.py::ping") in edge_pairs
    assert ("beta.py::ping", "alpha.py::pong") in edge_pairs

    file_edge_pairs = {(e.source, e.target) for e in graph.file_edges}
    assert ("alpha.py", "beta.py") in file_edge_pairs
    assert ("beta.py", "alpha.py") in file_edge_pairs

    mermaid = graph.to_mermaid()
    assert "flowchart LR" in mermaid
    assert "-->" in mermaid


def test_scanner_disambiguates_same_file_class_methods(tmp_path: Path) -> None:
    (tmp_path / "classes.py").write_text(
        """
class A:
    def validate(self):
        return True

class B:
    def validate(self):
        return False

    def run(self):
        return self.validate()
""",
        encoding="utf-8",
    )
    graph = scan_directory(tmp_path)
    edge_pairs = {(e.source, e.target) for e in graph.edges}
    assert ("classes.py::B.run", "classes.py::B.validate") in edge_pairs
    assert ("classes.py::B.run", "classes.py::A.validate") not in edge_pairs


def test_scanner_disambiguates_imports_by_path_and_local_shadowing(tmp_path: Path) -> None:
    (tmp_path / "other").mkdir()
    (tmp_path / "subpkg").mkdir()
    (tmp_path / "other" / "models.py").write_text("def save_user():\n    return 1\n", encoding="utf-8")
    (tmp_path / "subpkg" / "models.py").write_text("def save_user():\n    return 2\n", encoding="utf-8")
    (tmp_path / "barrel").mkdir()
    (tmp_path / "barrel" / "__init__.py").write_text("def boot():\n    return 3\n", encoding="utf-8")

    (tmp_path / "consumer.py").write_text(
        """
from subpkg.models import save_user as local_shadow
from barrel import boot

def local_shadow():
    return 99

def run():
    boot()
    local_shadow()
""",
        encoding="utf-8",
    )
    (tmp_path / "real_subpkg_caller.py").write_text(
        "from subpkg.models import save_user\n\ndef go():\n    return save_user()\n",
        encoding="utf-8",
    )

    graph = scan_directory(tmp_path)
    edge_pairs = {(e.source, e.target) for e in graph.edges}
    assert ("real_subpkg_caller.py::go", "subpkg/models.py::save_user") in edge_pairs
    assert ("real_subpkg_caller.py::go", "other/models.py::save_user") not in edge_pairs
    assert ("consumer.py::run", "barrel/__init__.py::boot") in edge_pairs
    # Local function shadows imported alias
    assert ("consumer.py::run", "consumer.py::local_shadow") in edge_pairs
    assert ("consumer.py::run", "subpkg/models.py::save_user") not in edge_pairs


def test_scanner_detects_cycles(tmp_path: Path) -> None:
    (tmp_path / "cycle.py").write_text(
        "def a():\n    return b()\n\ndef b():\n    return a()\n",
        encoding="utf-8",
    )
    graph = scan_directory(tmp_path)
    assert len(graph.cycles) == 1
    assert set(graph.cycles[0]) == {"cycle.py::a", "cycle.py::b"}


def test_iterative_tarjan_handles_deep_graph_without_recursion_error() -> None:
    from codeatlas.models import CallEdge, SymbolNode
    from codeatlas.scanner import _find_cycles

    n = 1200
    nodes = [
        SymbolNode(
            id=f"f.py::fn_{i}",
            name=f"fn_{i}",
            kind="function",
            file="f.py",
            language="python",
            start_line=i + 1,
            end_line=i + 1,
        )
        for i in range(n)
    ]
    edges = [CallEdge(source=f"f.py::fn_{i}", target=f"f.py::fn_{(i + 1) % n}") for i in range(n)]
    cycles = _find_cycles(nodes, edges)
    assert len(cycles) == 1
    assert len(cycles[0]) == n


def test_cli_scan_and_export(tmp_path: Path, capsys) -> None:
    repo = _build_sample_repo(tmp_path)
    out_file = tmp_path / "out.json"

    assert main(["scan", str(repo)]) == 0
    captured = capsys.readouterr().out
    assert "CodeAtlas" in captured
    assert "Files: 2" in captured

    # Shorthand `codeatlas <path>` without explicit `scan` subcommand
    assert main([str(repo)]) == 0
    assert "Files: 2" in capsys.readouterr().out

    assert main(["export", str(repo), "--format", "json", "-o", str(out_file)]) == 0
    exported = json.loads(out_file.read_text(encoding="utf-8"))
    assert exported["stats"]["total_files"] == 2
    assert "health_score" in exported["stats"]

    html_file = tmp_path / "report.html"
    assert main(["export", str(repo), "--format", "html", "-o", str(html_file)]) == 0
    html_text = html_file.read_text(encoding="utf-8")
    assert "CodeAtlas Architecture Report" in html_text
    assert "Health:" in html_text
    assert "Circular Dependencies" in html_text
    assert "Unreferenced Symbols" in html_text


def test_scanner_single_file_and_go_package_import(tmp_path: Path) -> None:
    single = tmp_path / "solo.py"
    single.write_text("def a():\n    return b()\n\ndef b():\n    return 1\n", encoding="utf-8")
    single_graph = scan_directory(single)
    assert len(single_graph.files) == 1
    assert ("solo.py::a", "solo.py::b") in {(e.source, e.target) for e in single_graph.edges}

    go_repo = tmp_path / "gorepo"
    (go_repo / "pkg").mkdir(parents=True)
    (go_repo / "other").mkdir()
    (go_repo / "pkg" / "server.go").write_text(
        "package pkg\nfunc StartServer() {\n    return\n}\n",
        encoding="utf-8",
    )
    (go_repo / "other" / "dup.go").write_text(
        "package other\nfunc StartServer() {\n    return\n}\n",
        encoding="utf-8",
    )
    (go_repo / "cmd.go").write_text(
        'package main\nimport "github.com/test/gorepo/pkg"\nfunc main() {\n    pkg.StartServer()\n}\n',
        encoding="utf-8",
    )
    go_graph = scan_directory(go_repo)
    go_edges = {(e.source, e.target) for e in go_graph.edges}
    assert ("cmd.go::main", "pkg/server.go::StartServer") in go_edges
    assert ("cmd.go::main", "other/dup.go::StartServer") not in go_edges


def test_server_api_graph_and_file_security(tmp_path: Path) -> None:
    repo = _build_sample_repo(tmp_path)
    graph = scan_directory(repo)
    server = create_server(repo, graph, host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/graph") as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["stats"]["total_files"] == 2

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/graph?rescan=1") as resp:
            rescanned = json.loads(resp.read().decode("utf-8"))
            assert rescanned["stats"]["total_files"] == 2

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
            html = resp.read().decode("utf-8")
            assert "CodeAtlas" in html

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/file?path=alpha.py&start=3&end=4") as resp:
            snippet = json.loads(resp.read().decode("utf-8"))
            assert "def start" in snippet["content"]

        # Path traversal must be rejected with 403
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/file?path=../secret.txt")
            assert False, "Expected HTTPError 403"
        except urllib.error.HTTPError as err:
            assert err.code == 403

        # Missing static asset must return 404 (not 200 SPA fallback)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/assets/missing-chunk.js")
            assert False, "Expected HTTPError 404"
        except urllib.error.HTTPError as err:
            assert err.code == 404

        # Huge file > 2 MB must return 413 Request Entity Too Large
        big = repo / "huge.py"
        big.write_text("# " + ("x" * 2_100_000), encoding="utf-8")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/file?path=huge.py")
            assert False, "Expected HTTPError 413"
        except urllib.error.HTTPError as err:
            assert err.code == 413

        # Binding a second server to the exact same active port must raise OSError
        try:
            second = create_server(repo, graph, host="127.0.0.1", port=port)
            second.server_close()
            assert False, "Expected OSError when binding an occupied port"
        except OSError:
            pass
    finally:
        server.shutdown()
        server.server_close()
