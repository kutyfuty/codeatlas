"""Python AST analyzer for extracting symbols, local type hints, and call relationships."""

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


def _annotation_type_name(ann: ast.expr | None) -> str | None:
    """Extract a simple class/type name from a Python type annotation expression."""
    if ann is None:
        return None
    if isinstance(ann, ast.Name):
        return None if ann.id == "None" else ann.id
    if isinstance(ann, ast.Attribute):
        return ann.attr
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        cleaned = ann.value.strip().split("[", 1)[0].split(".")[-1]
        return cleaned if cleaned.isidentifier() and cleaned != "None" else None
    if isinstance(ann, ast.Subscript):
        base = _annotation_type_name(ann.value)
        if base in {"Optional", "Annotated", "ClassVar", "Final"}:
            if isinstance(ann.slice, ast.Tuple) and ann.slice.elts:
                return _annotation_type_name(ann.slice.elts[0])
            return _annotation_type_name(ann.slice)
        return base
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        return _annotation_type_name(ann.left) or _annotation_type_name(ann.right)
    return None


def analyze_python(source: str, rel_path: str) -> ParsedFile:
    """Parse Python source code iteratively into symbols and call references."""
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError:
        return ParsedFile()

    symbols: list[SymbolNode] = []
    raw_calls: dict[str, list[str]] = {}
    qualified_calls: dict[str, list[tuple[str, str]]] = {}
    imports: list[str] = []
    aliases: dict[str, str] = {}
    class_stack: list[str] = []
    scope_names: list[str] = []
    func_stack: list[str] = []
    var_types_stack: list[dict[str, str]] = []

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
            var_types_stack.pop()
            continue

        assert node is not None
        t = type(node)
        if t is ast.Call:
            if func_stack:
                func_expr = node.func  # type: ignore[attr-defined]
                if isinstance(func_expr, ast.Attribute) and isinstance(func_expr.value, ast.Name):
                    obj = func_expr.value.id
                    meth = func_expr.attr
                    if obj in {"self", "cls"}:
                        raw_calls[func_stack[-1]].append(meth)
                    else:
                        owner = var_types_stack[-1].get(obj) if var_types_stack else None
                        if not owner and obj and obj[0].isupper():
                            owner = obj
                        if owner:
                            qualified_calls.setdefault(func_stack[-1], []).append((owner, meth))
                        raw_calls[func_stack[-1]].append(meth)
                else:
                    callee = _call_name(func_expr)
                    if callee:
                        raw_calls[func_stack[-1]].append(callee)
        elif t is ast.Assign:
            if var_types_stack:
                val = node.value  # type: ignore[attr-defined]
                if isinstance(val, ast.Call):
                    ctor = _call_name(val.func)
                    if ctor and ctor[0].isupper():
                        for tgt in node.targets:  # type: ignore[attr-defined]
                            if isinstance(tgt, ast.Name):
                                var_types_stack[-1][tgt.id] = ctor
        elif t is ast.AnnAssign:
            if var_types_stack:
                tgt = node.target  # type: ignore[attr-defined]
                if isinstance(tgt, ast.Name):
                    tname = _annotation_type_name(node.annotation)  # type: ignore[attr-defined]
                    if tname:
                        var_types_stack[-1][tgt.id] = tname
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
            vt: dict[str, str] = {}
            if class_stack:
                vt["self"] = class_stack[-1]
                vt["cls"] = class_stack[-1]
            args_obj = node.args  # type: ignore[attr-defined]
            for arg in (*args_obj.posonlyargs, *args_obj.args, *args_obj.kwonlyargs):
                tname = _annotation_type_name(arg.annotation)
                if tname:
                    vt[arg.arg] = tname
            var_types_stack.append(vt)
            stack.append((2, None))

        children = list(ast.iter_child_nodes(node))
        for child in reversed(children):
            stack.append((0, child))

    return ParsedFile(
        symbols=symbols,
        raw_calls=raw_calls,
        qualified_calls=qualified_calls,
        imports=imports,
        aliases=aliases,
    )
