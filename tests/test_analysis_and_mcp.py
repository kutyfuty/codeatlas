"""Tests for architectural rules, impact blast radius, MCP server, and optional Tree-sitter."""

from __future__ import annotations

import io
import json
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

from codeatlas.analysis import (
    compute_impact,
    evaluate_check,
    load_config,
    parse_rule_spec,
    shortest_path,
    _parse_toml_fallback,
)
from codeatlas.analyzers.treesitter_analyzer import analyze_with_treesitter
from codeatlas.cli import main
from codeatlas.mcp_server import CodeAtlasMCPServer, serve_mcp_stdio
from codeatlas.scanner import scan_directory
from codeatlas.server import create_server


def _build_layered_repo(tmp_path: Path) -> Path:
    (tmp_path / "ui").mkdir()
    (tmp_path / "service").mkdir()
    (tmp_path / "db").mkdir()
    (tmp_path / "tests").mkdir()

    (tmp_path / "db" / "store.py").write_text(
        "def query_users():\n    return []\n",
        encoding="utf-8",
    )
    (tmp_path / "service" / "users.py").write_text(
        "from db.store import query_users\n\ndef fetch_users():\n    return query_users()\n",
        encoding="utf-8",
    )
    (tmp_path / "ui" / "dashboard.py").write_text(
        "from service.users import fetch_users\nfrom db.store import query_users\n\n"
        "def render_page():\n    return fetch_users()\n\n"
        "def shortcut_bug():\n    return query_users()\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_users.py").write_text(
        "from service.users import fetch_users\n\ndef test_fetch():\n    assert fetch_users() == []\n",
        encoding="utf-8",
    )
    return tmp_path


def test_toml_fallback_and_boundary_check(tmp_path: Path) -> None:
    repo = _build_layered_repo(tmp_path)
    toml_txt = (
        '[check]\nmax_cycles = 0\nmin_health = 70\nmax_dead_ratio = 0.9\n\n'
        '[[rules]]\nname = "UI must not call DB"\nfrom = "ui/*"\nforbid = "db/*"\n'
    )
    (repo / ".codeatlas.toml").write_text(toml_txt, encoding="utf-8")

    fallback_parsed = _parse_toml_fallback(toml_txt)
    assert fallback_parsed["check"]["min_health"] == 70
    assert fallback_parsed["rules"][0]["from"] == "ui/*"

    cfg = load_config(repo)
    assert cfg.min_health == 70
    assert len(cfg.rules) == 1

    graph = scan_directory(repo)
    res = evaluate_check(graph, cfg)
    assert res["passed"] is False
    assert any(v["kind"] == "rule" and v["source_file"] == "ui/dashboard.py" for v in res["violations"])

    # Fixing the forbidden edge makes `check` pass
    (repo / "ui" / "dashboard.py").write_text(
        "from service.users import fetch_users\n\ndef render_page():\n    return fetch_users()\n",
        encoding="utf-8",
    )
    assert main(["check", str(repo)]) == 0

    # CLI flag rule catches ui -> service boundary
    assert main(["check", str(repo), "--rule", "ui/*:service/*"]) == 2
    assert main(["check", str(repo), "--rule", "badformat"]) == 1


def test_impact_blast_radius_and_git_diff(tmp_path: Path) -> None:
    repo = _build_layered_repo(tmp_path)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    # Modify db/store.py in working tree
    (repo / "db" / "store.py").write_text(
        "def query_users(limit=10):\n    return []\n",
        encoding="utf-8",
    )
    graph = scan_directory(repo)
    impact = compute_impact(graph)

    assert impact["source"] == "git:working-tree"
    assert impact["changed_files"] == ["db/store.py"]
    impacted_names = {s["name"]: s["hop"] for s in impact["impacted_symbols"]}
    assert impacted_names["fetch_users"] == 1
    assert impacted_names["render_page"] == 2
    assert impacted_names["test_fetch"] == 2

    ep_names = {e["name"] for e in impact["affected_entrypoints"]}
    assert "render_page" in ep_names
    assert any(t["name"] == "test_fetch" for t in impact["affected_tests"])

    path_res = shortest_path(graph, "render_page", "query_users")
    assert path_res["found"] is True
    assert path_res["hops"] == 2

    assert main(["impact", str(repo), "--format", "md"]) == 0


def test_mcp_stdio_newline_and_content_length_framing(tmp_path: Path) -> None:
    repo = _build_layered_repo(tmp_path)
    requests_list = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "get_overview", "arguments": {}}},
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "blast_radius", "arguments": {"file": "db/store.py", "max_depth": None}},
        },
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "trace_path", "arguments": {"source": "render_page", "target": "query_users"}},
        },
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "search_symbols", "arguments": {"query": "fetch", "limit": None}},
        },
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "nonexistent_tool", "arguments": {}},
        },
    ]

    # 1. Newline-delimited stdio loop
    stdin_buf = io.StringIO("".join(json.dumps(r) + "\n" for r in requests_list))
    stdout_buf = io.StringIO()
    assert serve_mcp_stdio(repo, stdin=stdin_buf, stdout=stdout_buf) == 0

    lines = [json.loads(line) for line in stdout_buf.getvalue().splitlines() if line.strip()]
    # Notification without id must not emit a response
    assert len(lines) == 8
    assert lines[0]["result"]["serverInfo"]["name"] == "codeatlas"
    tool_names = {t["name"] for t in lines[2]["result"]["tools"]}
    assert {"get_overview", "blast_radius", "trace_path", "find_cycles_and_dead_code"} <= tool_names

    blast_payload = json.loads(lines[4]["result"]["content"][0]["text"])
    assert blast_payload["summary"]["impacted_symbol_count"] >= 3
    path_payload = json.loads(lines[5]["result"]["content"][0]["text"])
    assert path_payload["found"] is True
    assert lines[7]["error"]["code"] == -32601

    # 2. Content-Length framed message check
    frame_msg = json.dumps({"jsonrpc": "2.0", "id": 99, "method": "tools/call", "params": {"name": "get_callers", "arguments": {"symbol": "query_users"}}})
    framed_in = io.StringIO(f"Content-Length: {len(frame_msg)}\r\n\r\n{frame_msg}")
    framed_out = io.StringIO()
    assert serve_mcp_stdio(repo, stdin=framed_in, stdout=framed_out) == 0
    raw_out = framed_out.getvalue()
    assert raw_out.startswith("Content-Length:")
    body = raw_out.split("\r\n\r\n", 1)[1]
    callers_resp = json.loads(json.loads(body)["result"]["content"][0]["text"])
    assert callers_resp["found"] is True


def test_http_api_impact_check_and_export_aliases(tmp_path: Path) -> None:
    repo = _build_layered_repo(tmp_path)
    graph = scan_directory(repo)
    server = create_server(repo, graph, host="127.0.0.1", port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/impact?symbol=query_users") as resp:
            imp = json.loads(resp.read().decode("utf-8"))
            assert imp["summary"]["changed_symbol_count"] == 1
            assert imp["summary"]["impacted_symbol_count"] >= 3

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/check") as resp:
            chk = json.loads(resp.read().decode("utf-8"))
            assert chk["passed"] is True

        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/impact?depth=notanint")
            assert False, "Expected 400"
        except urllib.error.HTTPError as err:
            assert err.code == 400
    finally:
        server.shutdown()
        server.server_close()

    md_out = tmp_path / "short.md"
    mmd_out = tmp_path / "short.mmd"
    assert main(["export", str(repo), "--format", "md", "-o", str(md_out)]) == 0
    assert main(["export", str(repo), "--format", "mmd", "-o", str(mmd_out)]) == 0
    assert "## CodeAtlas Architecture Summary" in md_out.read_text(encoding="utf-8")
    assert "flowchart LR" in mmd_out.read_text(encoding="utf-8")


def test_treesitter_ast_walk_with_fake_backend(monkeypatch) -> None:
    class _FakeNode:
        def __init__(self, ntype: str, start_b: int, end_b: int, line: int, children=None, fields=None):
            self.type = ntype
            self.start_byte = start_b
            self.end_byte = end_b
            self.start_point = (line, 0)
            self.end_point = (line, 10)
            self.children = children or []
            self.parent = None
            self._fields = fields or {}
            for ch in self.children:
                ch.parent = self

        def child_by_field_name(self, name: str):
            return self._fields.get(name)

    src = "import { helper } from './util';\nfunction greet() { return helper(); }\n"
    idx_imp_end = src.index("\n")
    idx_fn_start = idx_imp_end + 1
    idx_fn_id = src.index("greet")
    idx_call = src.index("helper()")
    idx_call_id = src.index("helper", idx_call)

    imp_node = _FakeNode("import_statement", 0, idx_imp_end, 0)
    id_fn = _FakeNode("identifier", idx_fn_id, idx_fn_id + 5, 1)
    id_call = _FakeNode("identifier", idx_call_id, idx_call_id + 6, 1)
    call_node = _FakeNode("call_expression", idx_call, idx_call + 8, 1, children=[id_call], fields={"function": id_call})
    body = _FakeNode("statement_block", idx_fn_id + 8, len(src) - 1, 1, children=[call_node])
    fn_node = _FakeNode("function_declaration", idx_fn_start, len(src) - 1, 1, children=[id_fn, body])
    root_node = _FakeNode("program", 0, len(src), 0, children=[imp_node, fn_node])

    class _FakeTree:
        def __init__(self, root):
            self.root_node = root

    class _FakeParser:
        def parse(self, _b):
            return _FakeTree(root_node)

    monkeypatch.setattr(
        "codeatlas.analyzers.treesitter_analyzer._load_ts_backend",
        lambda: (object(), lambda _lang: _FakeParser()),
    )
    parsed = analyze_with_treesitter(src, "src/app.js", "javascript")
    assert parsed is not None
    assert parsed.imports == ["./util"]
    assert [s.name for s in parsed.symbols] == ["greet"]
    assert parsed.raw_calls["src/app.js::greet"] == ["helper"]


def test_config_errors_trailing_slash_multisource_and_mcp_guards(tmp_path: Path) -> None:
    # 1. Trailing slash rule + file root config loading + bad TOML value error
    (tmp_path / "ui").mkdir()
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "db.py").write_text("def save_row():\n    pass\n", encoding="utf-8")
    (tmp_path / "ui" / "view.py").write_text(
        "from db.db import save_row\ndef show():\n    return save_row()\n",
        encoding="utf-8",
    )
    (tmp_path / ".codeatlas.toml").write_text(
        '[check]\nmin_health = "not-int"\n',
        encoding="utf-8",
    )
    assert main(["check", str(tmp_path)]) == 1

    # Passing a regular file uses parent directory for config lookup and trailing slash rule works
    (tmp_path / "solo.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".codeatlas.toml").write_text(
        '[[rules]]\nfrom = "ui/"\nforbid = "db/"\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path / "solo.py")
    assert len(cfg.rules) == 1
    assert evaluate_check(scan_directory(tmp_path), cfg)["passed"] is False

    # 2. Multi-source BFS finds path even when the first same-named symbol has no path
    (tmp_path / "a.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text(
        "from db.db import save_row\ndef helper():\n    return save_row()\n",
        encoding="utf-8",
    )
    graph = scan_directory(tmp_path)
    p = shortest_path(graph, "helper", "save_row")
    assert p["found"] is True
    assert p["path"][0]["file"] == "b.py"

    # 3. MCP server rejects non-dict JSON and negative Content-Length cleanly
    bad_in = io.StringIO("[1, 2, 3]\nContent-Length: -5\r\n\r\n")
    bad_out = io.StringIO()
    assert serve_mcp_stdio(tmp_path, stdin=bad_in, stdout=bad_out) == 0
    err_objs = [json.loads(ln) for ln in bad_out.getvalue().splitlines() if ln.strip()]
    assert err_objs[0]["error"]["code"] == -32600
    assert err_objs[1]["error"]["code"] == -32700


def test_treesitter_deep_iterative_walk_no_recursion_error(monkeypatch) -> None:
    class _Node:
        def __init__(self, ntype: str, children=None):
            self.type = ntype
            self.start_byte = 0
            self.end_byte = 1
            self.start_point = (0, 0)
            self.end_point = (0, 1)
            self.children = children or []
            self.parent = None

        def child_by_field_name(self, _name: str):
            return None

    cur = _Node("block")
    for _ in range(1800):
        parent = _Node("block", [cur])
        cur.parent = parent
        cur = parent
    root = _Node("program", [cur])

    class _Tree:
        root_node = root

    class _Parser:
        def parse(self, _b):
            return _Tree()

    monkeypatch.setattr(
        "codeatlas.analyzers.treesitter_analyzer._load_ts_backend",
        lambda: (object(), lambda _lang: _Parser()),
    )
    parsed = analyze_with_treesitter("x", "deep.js", "javascript")
    assert parsed is not None

