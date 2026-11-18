"""Optional Tree-sitter AST analyzer when `codeatlas-viz[treesitter]` is installed.

Falls back cleanly when `tree-sitter` or language grammars are not present so core
CodeAtlas keeps zero mandatory third-party runtime dependencies.
"""

from __future__ import annotations

import importlib
from functools import lru_cache
from typing import Any

from ..models import SymbolNode
from .base import ParsedFile
from .js_analyzer import _extract_svelte_scripts

_TS_LANG_MAP: dict[str, str] = {
    "javascript": "javascript",
    "typescript": "typescript",
    "svelte": "javascript",
    "go": "go",
    "rust": "rust",
    "java": "java",
    "c": "c",
    "cpp": "cpp",
}

_FUNC_NODE_TYPES = frozenset({
    "function_declaration",
    "function_definition",
    "function_item",
    "method_declaration",
    "method_definition",
    "generator_function_declaration",
    "arrow_function",
    "function_expression",
})

_CLASS_NODE_TYPES = frozenset({
    "class_declaration",
    "class_definition",
    "class_specifier",
    "struct_specifier",
    "struct_item",
    "enum_item",
    "trait_item",
    "impl_item",
    "type_declaration",
    "interface_declaration",
})

_CALL_NODE_TYPES = frozenset({
    "call_expression",
    "method_invocation",
    "invocation_expression",
})

_IMPORT_NODE_TYPES = frozenset({
    "import_statement",
    "import_declaration",
    "import_spec",
    "use_declaration",
    "preproc_include",
})

_IGNORED_CALL_NAMES = frozenset({
    "if", "else", "for", "while", "switch", "return", "sizeof", "typeof", "await",
})


@lru_cache(maxsize=1)
def _load_ts_backend() -> tuple[Any, Any] | None:
    """Return `(tree_sitter, get_parser)` if optional Tree-sitter packages exist."""
    try:
        ts = importlib.import_module("tree_sitter")
    except ImportError:
        return None
    for pkg_name in ("tree_sitter_language_pack", "tree_sitter_languages"):
        try:
            pack = importlib.import_module(pkg_name)
            if hasattr(pack, "get_parser"):
                return ts, pack.get_parser
        except ImportError:
            continue
    return None


def has_treesitter() -> bool:
    """Return True if optional Tree-sitter AST backend is available."""
    return _load_ts_backend() is not None


def _node_text(node: Any, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _find_first_identifier(node: Any, source_bytes: bytes, depth: int = 0) -> str | None:
    """Extract primary identifier name from declaration headers without walking deep bodies."""
    if depth > 6:
        return None
    if node.type in {"identifier", "field_identifier", "type_identifier", "property_identifier"}:
        return _node_text(node, source_bytes)
    for child in node.children:
        if child.type in {"block", "statement_block", "compound_statement", "class_body", "declaration_list"}:
            continue
        if child.type == "qualified_identifier":
            txt = _node_text(child, source_bytes).strip()
            if txt:
                return txt.split("::")[-1]
        if child.type in {"identifier", "field_identifier", "type_identifier", "property_identifier"}:
            return _node_text(child, source_bytes)
        nested = _find_first_identifier(child, source_bytes, depth + 1)
        if nested:
            return nested
    return None


def _extract_call_target(node: Any, source_bytes: bytes) -> str | None:
    fn = node.child_by_field_name("function") or node.child_by_field_name("name")
    if fn is None and node.children:
        fn = node.children[0]
    if fn is None:
        return None
    if fn.type in {"identifier", "field_identifier", "property_identifier"}:
        return _node_text(fn, source_bytes)
    if fn.type in {"member_expression", "field_expression", "selector_expression", "scoped_identifier"}:
        prop = fn.child_by_field_name("property") or fn.child_by_field_name("field") or fn.child_by_field_name("name")
        if prop is not None:
            return _node_text(prop, source_bytes)
        if fn.children:
            return _node_text(fn.children[-1], source_bytes)
    return None


def analyze_with_treesitter(source: str, rel_path: str, language: str) -> ParsedFile | None:
    """Analyze JS/TS/Svelte/Go/Rust/Java/C/C++ via Tree-sitter when installed.

    Returns `None` if Tree-sitter or the target language grammar is unavailable.
    """
    backend = _load_ts_backend()
    if backend is None:
        return None

    _, get_parser = backend
    ts_lang = "tsx" if rel_path.endswith(".tsx") else _TS_LANG_MAP.get(language)
    if not ts_lang:
        return None

    try:
        parser = get_parser(ts_lang)
    except Exception:
        return None

    clean_src = _extract_svelte_scripts(source) if language == "svelte" else source
    source_bytes = clean_src.encode("utf-8", errors="replace")
    try:
        tree = parser.parse(source_bytes)
    except Exception:
        return None

    symbols: list[SymbolNode] = []
    raw_calls: dict[str, list[str]] = {}
    imports: list[str] = []
    seen_ids: set[str] = set()

    def unique_id(qname: str, line: int) -> str:
        base = f"{rel_path}::{qname}"
        if base not in seen_ids:
            seen_ids.add(base)
            return base
        alt = f"{base}@{line}"
        seen_ids.add(alt)
        return alt

    class_stack: list[str] = []
    func_stack: list[str] = []
    # Stack items: (action, node) where 0 = enter, 1 = exit class, 2 = exit func
    stack: list[tuple[int, Any]] = [(0, tree.root_node)]

    while stack:
        action, node = stack.pop()
        if action == 1:
            class_stack.pop()
            continue
        if action == 2:
            func_stack.pop()
            continue

        ntype = node.type
        if ntype in _IMPORT_NODE_TYPES:
            txt = _node_text(node, source_bytes).strip()
            extracted = ""
            for quote in ('"', "'", "<"):
                if quote in txt:
                    end_q = ">" if quote == "<" else quote
                    extracted = txt.split(quote, 1)[1].split(end_q, 1)[0].strip()
                    break
            if not extracted and txt.startswith(("use ", "import ")):
                extracted = (
                    txt.removeprefix("use ")
                    .removeprefix("import ")
                    .removeprefix("static ")
                    .rstrip(";")
                    .strip()
                    .replace("::", "/")
                )
            if extracted:
                imports.append(extracted)

        if ntype in _CLASS_NODE_TYPES:
            name = _find_first_identifier(node, source_bytes)
            if name:
                start_line = node.start_point[0] + 1
                end_line = max(start_line, node.end_point[0] + 1)
                sym_id = unique_id(name, start_line)
                symbols.append(
                    SymbolNode(
                        id=sym_id,
                        name=name,
                        kind="class",
                        file=rel_path,
                        language=language,
                        start_line=start_line,
                        end_line=end_line,
                        parent=class_stack[-1] if class_stack else None,
                    )
                )
                class_stack.append(name)
                stack.append((1, None))
                for child in reversed(node.children):
                    stack.append((0, child))
                continue

        if ntype in _FUNC_NODE_TYPES:
            name = None
            if ntype in {"arrow_function", "function_expression"}:
                parent = node.parent
                if parent is not None and parent.type in {"variable_declarator", "pair", "public_field_definition"}:
                    name = _find_first_identifier(parent, source_bytes)
            else:
                name = _find_first_identifier(node, source_bytes)

            if name and name not in _IGNORED_CALL_NAMES:
                start_line = node.start_point[0] + 1
                end_line = max(start_line, node.end_point[0] + 1)
                parent_cls = class_stack[-1] if class_stack else None
                kind = "method" if parent_cls and not func_stack else "function"
                qname = f"{parent_cls}.{name}" if parent_cls else name
                sym_id = unique_id(qname, start_line)
                symbols.append(
                    SymbolNode(
                        id=sym_id,
                        name=name,
                        kind=kind,
                        file=rel_path,
                        language=language,
                        start_line=start_line,
                        end_line=end_line,
                        parent=parent_cls,
                    )
                )
                raw_calls.setdefault(sym_id, [])
                func_stack.append(sym_id)
                stack.append((2, None))
                for child in reversed(node.children):
                    stack.append((0, child))
                continue

        if ntype in _CALL_NODE_TYPES and func_stack:
            target = _extract_call_target(node, source_bytes)
            if target and target not in _IGNORED_CALL_NAMES:
                raw_calls[func_stack[-1]].append(target)

        for child in reversed(node.children):
            stack.append((0, child))

    return ParsedFile(symbols=symbols, raw_calls=raw_calls, imports=imports)
