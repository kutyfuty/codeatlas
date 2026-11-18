"""Structural analyzer for Go, Rust, Java, and C/C++ source files."""

from __future__ import annotations

import re

from ..models import SymbolNode
from .base import ParsedFile
from .js_analyzer import (
    _build_line_index,
    _find_matching_brace,
    _line_at,
    _strip_comments_and_strings,
)

_C_FAMILY_KEYWORDS = frozenset({
    "if", "else", "for", "while", "do", "switch", "case", "catch", "finally",
    "try", "return", "throw", "sizeof", "alignof", "typeof", "new", "delete",
    "class", "struct", "enum", "union", "interface", "impl", "trait", "fn",
    "func", "pub", "private", "protected", "public", "static", "const",
    "virtual", "override", "synchronized", "match", "loop", "where", "defer",
    "go", "select", "chan", "package", "import", "use", "mod", "crate",
    "super", "self", "this", "main", "printf", "println", "fmt", "std",
})

# Go: func (r *Recv) Name(args) [ret] {  OR  func Name(args) [ret] {
_GO_FUNC_RE = re.compile(
    r"""(?:^|\n)\s*func\s+(?:\(\s*[A-Za-z_]\w*\s+\*?([A-Za-z_]\w*)\s*\)\s+)?([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*\([^)]*\)[^{;]*\{""",
)
_GO_IMPORT_RE = re.compile(r'"([^"]+)"')

# Rust: [pub] [async] [unsafe] fn name[<T>](args) [-> Ret] [where ...] {
_RUST_STRUCT_IMPL_RE = re.compile(
    r"""(?:^|\n)\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|impl(?:\s*<[^>]*>)?(?:\s+[A-Za-z_][\w:<>]*\s+for)?)\s+([A-Za-z_]\w*)[^{;]*\{""",
)
_RUST_FN_RE = re.compile(
    r"""(?:^|\n)\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\([^)]*\)[^{;]*\{""",
)
_RUST_USE_RE = re.compile(r"""(?:^|\n)\s*use\s+([A-Za-z_][\w:]*)\s*;""")

# Java / C / C++ classes & functions/methods
_CLASS_STRUCT_RE = re.compile(
    r"""(?:^|[\n;{}])\s*(?:(?:public|private|protected|abstract|final|static)\s+)*(?:class|struct|interface)\s+([A-Za-z_]\w*)[^{;]*\{""",
)
_CFAMILY_FUNC_RE = re.compile(
    r"""(?:^|[\n;{}])\s*(?:(?:public|private|protected|static|final|virtual|inline|explicit|constexpr|synchronized|native|async)\s+)*(?:[A-Za-z_][\w:<>,*\s&~]*?\s+)+([A-Za-z_]\w*)\s*\([^;{}]*\)\s*(?:const\s*)?(?:noexcept\s*)?(?:override\s*)?(?:throws\s+[A-Za-z0-9_.,\s]+)?\{""",
)
_JAVA_IMPORT_RE = re.compile(r"""(?:^|\n)\s*import\s+(?:static\s+)?([A-Za-z_][\w.]*)\s*;""")
_CPP_INCLUDE_RE = re.compile(r"""#\s*include\s*[<"]([^>"]+)[>"]""")

_CALL_RE = re.compile(r"""(?<![.\w:])([A-Za-z_]\w*)\s*\(""")
_MEMBER_CALL_RE = re.compile(r"""(?:\.|->|::)([A-Za-z_]\w*)\s*\(""")


def _extract_calls(body: str) -> list[str]:
    calls = [
        m.group(1)
        for m in _CALL_RE.finditer(body)
        if m.group(1) not in _C_FAMILY_KEYWORDS
    ]
    calls.extend(
        m.group(1)
        for m in _MEMBER_CALL_RE.finditer(body)
        if m.group(1) not in _C_FAMILY_KEYWORDS
    )
    return calls


def analyze_c_family(source: str, rel_path: str, language: str) -> ParsedFile:
    """Analyze Go, Rust, Java, or C/C++ source code into symbols and call sites."""
    imports: list[str] = []
    if language == "go":
        imports.extend(_GO_IMPORT_RE.findall(source))
    elif language == "rust":
        imports.extend(m.replace("::", "/") for m in _RUST_USE_RE.findall(source))
    elif language == "java":
        imports.extend(_JAVA_IMPORT_RE.findall(source))
    elif language in {"c", "cpp"}:
        imports.extend(_CPP_INCLUDE_RE.findall(source))

    clean = _strip_comments_and_strings(source)
    newlines = _build_line_index(clean)

    symbols: list[SymbolNode] = []
    raw_calls: dict[str, list[str]] = {}
    seen_ids: set[str] = set()

    def register(
        name: str,
        kind: str,
        decl_start: int,
        open_brace: int,
        parent: str | None = None,
    ) -> int:
        close_idx = _find_matching_brace(clean, open_brace)
        qname = f"{parent}.{name}" if parent else name
        sym_id = f"{rel_path}::{qname}"
        if sym_id in seen_ids:
            return close_idx
        seen_ids.add(sym_id)

        symbols.append(
            SymbolNode(
                id=sym_id,
                name=name,
                kind=kind,
                file=rel_path,
                language=language,
                start_line=_line_at(newlines, decl_start),
                end_line=_line_at(newlines, close_idx),
                parent=parent,
            )
        )
        if kind != "class":
            raw_calls[sym_id] = _extract_calls(clean[open_brace + 1 : close_idx])
        return close_idx

    if language == "go":
        for m in _GO_FUNC_RE.finditer(clean):
            recv, name = m.group(1), m.group(2)
            kind = "method" if recv else "function"
            register(name, kind, m.start(2), m.end() - 1, parent=recv)

    elif language == "rust":
        impl_spans: list[tuple[int, int, str]] = []
        for m in _RUST_STRUCT_IMPL_RE.finditer(clean):
            cname = m.group(1)
            open_b = m.end() - 1
            close_b = _find_matching_brace(clean, open_b)
            # Only register struct/enum/trait once as a class node
            register(cname, "class", m.start(1), open_b)
            impl_spans.append((open_b, close_b, cname))

        for m in _RUST_FN_RE.finditer(clean):
            fn_name = m.group(1)
            pos = m.start(1)
            parent = None
            for ob, cb, cname in impl_spans:
                if ob < pos < cb:
                    parent = cname
                    break
            register(
                fn_name,
                "method" if parent else "function",
                pos,
                m.end() - 1,
                parent=parent,
            )

    else:
        # Java, C, C++
        class_spans: list[tuple[int, int, str]] = []
        for m in _CLASS_STRUCT_RE.finditer(clean):
            cname = m.group(1)
            if cname in _C_FAMILY_KEYWORDS:
                continue
            open_b = m.end() - 1
            close_b = register(cname, "class", m.start(1), open_b)
            class_spans.append((open_b, close_b, cname))

        for m in _CFAMILY_FUNC_RE.finditer(clean):
            fname = m.group(1)
            if fname in _C_FAMILY_KEYWORDS:
                continue
            pos = m.start(1)
            parent = None
            for ob, cb, cname in class_spans:
                if ob < pos < cb:
                    parent = cname
                    break
            register(
                fname,
                "method" if parent else "function",
                pos,
                m.end() - 1,
                parent=parent,
            )

    symbols.sort(key=lambda s: s.start_line)
    return ParsedFile(symbols=symbols, raw_calls=raw_calls, imports=imports)
