"""Unit tests for Python, JavaScript, TypeScript, Svelte, Go, Rust, Java, and C++ analyzers."""

from codeatlas.analyzers.c_family_analyzer import analyze_c_family
from codeatlas.analyzers.js_analyzer import analyze_js
from codeatlas.analyzers.python_analyzer import analyze_python


def test_python_analyzer_extracts_classes_methods_and_calls() -> None:
    source = '''
import os
from helpers import compute

class Service:
    """Service docstring."""

    def run(self, x):
        val = compute(x)
        return self.helper(val)

    def helper(self, y):
        return y + 1

def standalone():
    svc = Service()
    return svc.run(42)
'''
    parsed = analyze_python(source, "app.py")
    names = {s.name: s for s in parsed.symbols}
    assert "Service" in names
    assert names["Service"].kind == "class"
    assert names["Service"].docstring == "Service docstring."
    assert "run" in names
    assert names["run"].kind == "method"
    assert "standalone" in names
    assert names["standalone"].kind == "function"

    assert "compute" in parsed.raw_calls["app.py::Service.run"]
    assert "helper" in parsed.raw_calls["app.py::Service.run"]
    assert "Service" in parsed.raw_calls["app.py::standalone"]
    assert "run" in parsed.raw_calls["app.py::standalone"]
    assert "helpers" in parsed.imports


def test_python_analyzer_handles_syntax_error_gracefully() -> None:
    parsed = analyze_python("def broken(:\n    pass", "broken.py")
    assert parsed.symbols == []
    assert parsed.raw_calls == {}


def test_python_nested_functions_and_aliases() -> None:
    source = '''
from . import utils
from auth import verify_token as vt

def outer():
    def helper():
        return vt()
    return helper()

def helper():
    return 0
'''
    parsed = analyze_python(source, "mod.py")
    ids = {s.id for s in parsed.symbols}
    assert "mod.py::outer" in ids
    assert "mod.py::outer.helper" in ids
    assert "mod.py::helper" in ids
    assert parsed.aliases["vt"] == "verify_token"
    assert "utils" in parsed.imports


def test_js_analyzer_extracts_class_methods() -> None:
    source = '''
export class UserService {
  async login(user) {
    return this.validate(user);
  }

  validate(user) {
    return Boolean(user);
  }
}
'''
    parsed = analyze_js(source, "service.ts", language="typescript")
    by_id = {s.id: s for s in parsed.symbols}
    assert "service.ts::UserService" in by_id
    assert "service.ts::UserService.login" in by_id
    assert by_id["service.ts::UserService.login"].kind == "method"
    assert by_id["service.ts::UserService.login"].parent == "UserService"
    assert "validate" in parsed.raw_calls["service.ts::UserService.login"]


def test_js_analyzer_nested_functions_and_class_arrow_properties() -> None:
    source = '''
class Controller {
  handle = async (req) => {
    return this.ok();
  }
  get status() {
    return computeStatus();
  }
}

function alpha() {
  function helper() {
    return 1;
  }
  return helper();
}

function beta() {
  function helper() {
    return 2;
  }
  return helper();
}
'''
    parsed = analyze_js(source, "ctrl.ts", language="typescript")
    by_id = {s.id: s for s in parsed.symbols}
    assert "ctrl.ts::Controller.handle" in by_id
    assert "ctrl.ts::Controller.status" in by_id
    assert "ctrl.ts::alpha.helper" in by_id
    assert "ctrl.ts::beta.helper" in by_id
    assert "ok" in parsed.raw_calls["ctrl.ts::Controller.handle"]
    assert "computeStatus" in parsed.raw_calls["ctrl.ts::Controller.status"]


def test_js_analyzer_inline_method_and_exact_start_line() -> None:
    source = """class Inline { ping() { return pong(); } }
class Multi {
  public async fetchItems() {
    return 1;
  }
}
"""
    parsed = analyze_js(source, "inline.ts", language="typescript")
    by_id = {s.id: s for s in parsed.symbols}
    assert "inline.ts::Inline.ping" in by_id
    assert by_id["inline.ts::Inline.ping"].start_line == 1
    assert "pong" in parsed.raw_calls["inline.ts::Inline.ping"]
    assert "inline.ts::Multi.fetchItems" in by_id
    assert by_id["inline.ts::Multi.fetchItems"].start_line == 3


def test_js_analyzer_ignores_comments_and_strings() -> None:
    source = '''
import { fetchData } from './api';

// function fakeInComment() { secretCall(); }
const msg = "notACall()";

export async function loadUser(id) {
  const raw = fetchData(id);
  return formatUser(raw);
}

const formatUser = (u) => {
  return u.name;
};
'''
    parsed = analyze_js(source, "user.ts", language="typescript")
    names = {s.name for s in parsed.symbols}
    assert names == {"loadUser", "formatUser"}
    assert "fakeInComment" not in names
    assert "fetchData" in parsed.raw_calls["user.ts::loadUser"]
    assert "formatUser" in parsed.raw_calls["user.ts::loadUser"]
    assert "./api" in parsed.imports


def test_svelte_analyzer_preserves_line_numbers() -> None:
    source = """<script>
  import { loadGraph } from './api';

  function handleClick() {
    loadGraph();
  }
</script>

<button on:click={handleClick}>Go</button>
"""
    parsed = analyze_js(source, "App.svelte", language="svelte")
    assert len(parsed.symbols) == 1
    sym = parsed.symbols[0]
    assert sym.name == "handleClick"
    assert sym.language == "svelte"
    assert sym.start_line == 4
    assert "loadGraph" in parsed.raw_calls["App.svelte::handleClick"]


def test_go_and_rust_analyzers() -> None:
    go_src = '''
package main
import "fmt"

func (s *Server) Start() error {
    return s.listen()
}

func (s *Server) listen() error {
    return nil
}
'''
    go_parsed = analyze_c_family(go_src, "server.go", "go")
    go_ids = {s.id: s for s in go_parsed.symbols}
    assert "server.go::Server.Start" in go_ids
    assert go_ids["server.go::Server.Start"].kind == "method"
    assert "listen" in go_parsed.raw_calls["server.go::Server.Start"]

    rust_src = '''
use crate::engine::Runner;

pub struct Engine {}

impl Engine {
    pub fn execute(&self) {
        self.prepare();
    }
    fn prepare(&self) {}
}
'''
    rs_parsed = analyze_c_family(rust_src, "lib.rs", "rust")
    rs_ids = {s.id: s for s in rs_parsed.symbols}
    assert "lib.rs::Engine" in rs_ids
    assert "lib.rs::Engine.execute" in rs_ids
    assert "prepare" in rs_parsed.raw_calls["lib.rs::Engine.execute"]


def test_java_and_cpp_analyzers() -> None:
    java_src = '''
import com.example.Auth;

public class OrderService {
    public boolean processOrder(int id) {
        return validateOrder(id);
    }
    private boolean validateOrder(int id) {
        return true;
    }
}
'''
    j_parsed = analyze_c_family(java_src, "OrderService.java", "java")
    j_ids = {s.id: s for s in j_parsed.symbols}
    assert "OrderService.java::OrderService" in j_ids
    assert "OrderService.java::OrderService.processOrder" in j_ids
    assert "validateOrder" in j_parsed.raw_calls["OrderService.java::OrderService.processOrder"]
