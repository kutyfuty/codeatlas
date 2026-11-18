"""Python AST analyzer for extracting symbols and call relationships."""

from __future__ import annotations

import ast

from ..models import SymbolNode
from .base import ParsedFile


def _call_name(node: ast.expr) -> str | None:
    """Extract a readable callee name from an ast.Call func expression."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


class _PythonVisitor(ast.NodeVisitor):
    """Walks a Python AST tracking the active class and function scope."""

    def __init__(self, rel_path: str) -> None:
        self.rel_path = rel_path
        self.symbols: list[SymbolNode] = []
        self.raw_calls: dict[str, list[str]] = {}
        self.imports: list[str] = []
        self.aliases: dict[str, str] = {}
        self._class_stack: list[str] = []
        self._scope_names: list[str] = []
        self._func_stack: list[str] = []

    def _make_id(self, qualified_name: str) -> str:
        return f"{self.rel_path}::{qualified_name}"

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(alias.name)
            if alias.asname:
                self.aliases[alias.asname] = alias.name.split(".")[-1]

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self.imports.append(node.module)
        for alias in node.names:
            if not node.module and alias.name != "*":
                self.imports.append(alias.name)
            if alias.asname and alias.name != "*":
                self.aliases[alias.asname] = alias.name

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope_names.append(node.name)
        qname = ".".join(self._scope_names)
        sym_id = self._make_id(qname)
        doc = ast.get_docstring(node) or ""
        self.symbols.append(
            SymbolNode(
                id=sym_id,
                name=node.name,
                kind="class",
                file=self.rel_path,
                language="python",
                start_line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                docstring=doc.splitlines()[0] if doc else "",
                parent=self._class_stack[-1] if self._class_stack else None,
            )
        )
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()
        self._scope_names.pop()

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        in_class_directly = bool(self._class_stack) and not self._func_stack
        kind = "method" if in_class_directly else "function"
        parent = self._class_stack[-1] if self._class_stack else None

        self._scope_names.append(node.name)
        qname = ".".join(self._scope_names)
        sym_id = self._make_id(qname)
        doc = ast.get_docstring(node) or ""
        self.symbols.append(
            SymbolNode(
                id=sym_id,
                name=node.name,
                kind=kind,
                file=self.rel_path,
                language="python",
                start_line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                docstring=doc.splitlines()[0] if doc else "",
                parent=parent,
            )
        )
        self.raw_calls.setdefault(sym_id, [])
        self._func_stack.append(sym_id)
        self.generic_visit(node)
        self._func_stack.pop()
        self._scope_names.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_func(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self._func_stack:
            callee = _call_name(node.func)
            if callee:
                self.raw_calls[self._func_stack[-1]].append(callee)
        self.generic_visit(node)


def analyze_python(source: str, rel_path: str) -> ParsedFile:
    """Parse Python source code into symbols and call references."""
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError:
        return ParsedFile()

    visitor = _PythonVisitor(rel_path)
    visitor.visit(tree)
    return ParsedFile(
        symbols=visitor.symbols,
        raw_calls=visitor.raw_calls,
        imports=visitor.imports,
        aliases=visitor.aliases,
    )
