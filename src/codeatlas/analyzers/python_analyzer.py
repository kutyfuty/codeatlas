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


def analyze_python(source: str, rel_path: str) -> ParsedFile:
    """Parse Python source code iteratively into symbols and call references."""
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError:
        return ParsedFile()

    symbols: list[SymbolNode] = []
    raw_calls: dict[str, list[str]] = {}
    imports: list[str] = []
    aliases: dict[str, str] = {}
    class_stack: list[str] = []
    scope_names: list[str] = []
    func_stack: list[str] = []

    # Stack items: (action, payload) where action 0 = enter node, 1 = exit class, 2 = exit func
    stack: list[tuple[int, ast.AST | None]] = [(0, tree)]
    while stack:
        action, node = stack.pop()
        if action == 1:
            class_stack.pop()
            scope_names.pop()
            continue
        if action == 2:
            func_stack.pop()
            scope_names.pop()
            continue

        assert node is not None
        t = type(node)
        if t is ast.Call:
            if func_stack:
                callee = _call_name(node.func)  # type: ignore[attr-defined]
                if callee:
                    raw_calls[func_stack[-1]].append(callee)
        elif t is ast.Import:
            for alias in node.names:  # type: ignore[attr-defined]
                imports.append(alias.name)
                if alias.asname:
                    aliases[alias.asname] = alias.name.split(".")[-1]
            continue
        elif t is ast.ImportFrom:
            mod = node.module  # type: ignore[attr-defined]
            if mod:
                imports.append(mod)
            for alias in node.names:  # type: ignore[attr-defined]
                if not mod and alias.name != "*":
                    imports.append(alias.name)
                if alias.asname and alias.name != "*":
                    aliases[alias.asname] = alias.name
            continue
        elif t is ast.ClassDef:
            name = node.name  # type: ignore[attr-defined]
            scope_names.append(name)
            sym_id = f"{rel_path}::{'.'.join(scope_names)}"
            doc = ast.get_docstring(node) or ""  # type: ignore[arg-type]
            lineno = node.lineno  # type: ignore[attr-defined]
            end_lineno = node.end_lineno or lineno  # type: ignore[attr-defined]
            symbols.append(
                SymbolNode(
                    id=sym_id,
                    name=name,
                    kind="class",
                    file=rel_path,
                    language="python",
                    start_line=lineno,
                    end_line=end_lineno,
                    docstring=doc.splitlines()[0] if doc else "",
                    parent=class_stack[-1] if class_stack else None,
                )
            )
            class_stack.append(name)
            stack.append((1, None))
        elif t is ast.FunctionDef or t is ast.AsyncFunctionDef:
            in_class_directly = bool(class_stack) and not func_stack
            kind = "method" if in_class_directly else "function"
            parent = class_stack[-1] if class_stack else None
            name = node.name  # type: ignore[attr-defined]
            scope_names.append(name)
            sym_id = f"{rel_path}::{'.'.join(scope_names)}"
            doc = ast.get_docstring(node) or ""  # type: ignore[arg-type]
            lineno = node.lineno  # type: ignore[attr-defined]
            end_lineno = node.end_lineno or lineno  # type: ignore[attr-defined]
            symbols.append(
                SymbolNode(
                    id=sym_id,
                    name=name,
                    kind=kind,
                    file=rel_path,
                    language="python",
                    start_line=lineno,
                    end_line=end_lineno,
                    docstring=doc.splitlines()[0] if doc else "",
                    parent=parent,
                )
            )
            raw_calls.setdefault(sym_id, [])
            func_stack.append(sym_id)
            stack.append((2, None))

        children = list(ast.iter_child_nodes(node))
        for child in reversed(children):
            stack.append((0, child))

    return ParsedFile(
        symbols=symbols,
        raw_calls=raw_calls,
        imports=imports,
        aliases=aliases,
    )
