"""Tests for v0.4.0 upgrades: type-aware call resolution, git baseline diff, and GitHub Action."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from codeatlas.analysis import compare_graphs, evaluate_baseline_check, load_graph_from_json, scan_git_ref
from codeatlas.cli import main
from codeatlas.scanner import scan_directory


def test_python_local_type_and_constructor_disambiguation(tmp_path: Path) -> None:
    (tmp_path / "users.py").write_text(
        "class UserService:\n"
        "    def get(self):\n"
        "        return self.fetch_db()\n"
        "    def fetch_db(self):\n"
        "        return 'user'\n",
        encoding="utf-8",
    )
    (tmp_path / "orders.py").write_text(
        "class OrderService:\n"
        "    def get(self):\n"
        "        return 'order'\n",
        encoding="utf-8",
    )
    (tmp_path / "api.py").write_text(
        "from users import UserService\n"
        "from orders import OrderService\n\n"
        "def handle_user(svc: UserService | None):\n"
        "    return svc.get()\n\n"
        "def handle_order():\n"
        "    svc = OrderService()\n"
        "    return svc.get()\n",
        encoding="utf-8",
    )
    graph = scan_directory(tmp_path)
    edges = {(e.source, e.target) for e in graph.edges}

    # Even though `get` is an ambiguous global name, typed receiver resolves precisely!
    assert ("api.py::handle_user", "users.py::UserService.get") in edges
    assert ("api.py::handle_user", "orders.py::OrderService.get") not in edges
    assert ("api.py::handle_order", "orders.py::OrderService.get") in edges
    assert ("api.py::handle_order", "users.py::UserService.get") not in edges
    assert ("users.py::UserService.get", "users.py::UserService.fetch_db") in edges


def test_ts_and_js_class_instance_disambiguation(tmp_path: Path) -> None:
    (tmp_path / "auth.ts").write_text(
        "export class AuthClient {\n"
        "  get() { return 1; }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "billing.ts").write_text(
        "export class BillingClient {\n"
        "  get() { return 2; }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "app.ts").write_text(
        "import { AuthClient } from './auth';\n"
        "import { BillingClient } from './billing';\n"
        "function runAuth(client: AuthClient) {\n"
        "  return client.get();\n"
        "}\n"
        "function runBilling() {\n"
        "  const b = new BillingClient();\n"
        "  return b.get();\n"
        "}\n",
        encoding="utf-8",
    )
    graph = scan_directory(tmp_path)
    edges = {(e.source, e.target) for e in graph.edges}
    assert ("app.ts::runAuth", "auth.ts::AuthClient.get") in edges
    assert ("app.ts::runAuth", "billing.ts::BillingClient.get") not in edges
    assert ("app.ts::runBilling", "billing.ts::BillingClient.get") in edges
    assert ("app.ts::runBilling", "auth.ts::AuthClient.get") not in edges


def test_baseline_diff_ignores_preexisting_debt_and_catches_new_cycles(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    # Commit 1: already has 1 pre-existing cycle (legacy_a <-> legacy_b)
    (repo / "legacy.py").write_text(
        "def legacy_a():\n    return legacy_b()\n\ndef legacy_b():\n    return legacy_a()\n",
        encoding="utf-8",
    )
    (repo / "feature.py").write_text("def clean_fn():\n    return 1\n", encoding="utf-8")

    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)

    # Save JSON baseline snapshot too
    snap = tmp_path / "base.json"
    assert main(["export", str(repo), "--format", "json", "-o", str(snap)]) == 0
    capsys.readouterr()

    # Commit 2: clean change (no NEW cycle) -> baseline check must PASS even though legacy cycle exists!
    (repo / "feature.py").write_text(
        "def clean_fn():\n    return helper_fn()\n\ndef helper_fn():\n    return 2\n",
        encoding="utf-8",
    )
    assert main(["check", str(repo), "--baseline", "HEAD", "--max-cycles", "0"]) == 0
    assert main(["check", str(repo), "--baseline-json", str(snap), "--max-cycles", "0"]) == 0
    capsys.readouterr()

    # Now introduce a NEW cycle (new_x <-> new_y) in working tree -> baseline check must FAIL (exit 2)
    (repo / "feature.py").write_text(
        "def new_x():\n    return new_y()\n\ndef new_y():\n    return new_x()\n",
        encoding="utf-8",
    )
    assert main(["check", str(repo), "--baseline", "HEAD", "--max-cycles", "0"]) == 2
    capsys.readouterr()

    # `diff` command with --with-impact --format markdown emits PR comment marker and new cycle
    rc = main(["diff", str(repo), "--base", "HEAD", "--with-impact", "--format", "markdown", "--fail-on-new-cycles"])
    assert rc == 2
    md_out = capsys.readouterr().out
    assert "<!-- codeatlas-pr-comment -->" in md_out
    assert "New Circular Dependencies" in md_out
    assert "CodeAtlas Change Impact" in md_out

    # Direct API sanity check
    base_g = scan_git_ref(repo, "HEAD")
    head_g = scan_directory(repo)
    loaded_g = load_graph_from_json(snap)
    assert len(loaded_g.cycles) == 1
    diff = compare_graphs(base_g, head_g)
    assert len(diff["new_cycles"]) == 1
    assert len(diff["resolved_cycles"]) == 0
    assert evaluate_baseline_check(base_g, head_g, config=load_config_empty())["passed"] is False


def load_config_empty():
    from codeatlas.analysis import AtlasConfig

    return AtlasConfig(max_cycles=0, min_health=0)


def test_github_action_manifest_present() -> None:
    action_file = Path(__file__).resolve().parents[1] / "action.yml"
    assert action_file.is_file()
    content = action_file.read_text(encoding="utf-8")
    assert 'using: "composite"' in content
    assert "codeatlas diff" in content
    assert "codeatlas check" in content
    assert "<!-- codeatlas-pr-comment -->" in content
