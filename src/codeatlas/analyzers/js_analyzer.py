"""Structural analyzer for JavaScript, TypeScript, and Svelte files."""

from __future__ import annotations

import bisect
import re

from ..models import SymbolNode
from .base import ParsedFile

_KEYWORDS = frozenset({
    "if", "else", "for", "while", "do", "switch", "case", "catch", "finally",
    "try", "return", "throw", "typeof", "instanceof", "new", "delete", "void",
    "await", "yield", "import", "export", "from", "class", "function", "super",
    "this", "constructor", "get", "set", "static", "async",
})

_SVELTE_SCRIPT_RE = re.compile(
    r"<script(?:\s[^>]*)?>(.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)
_IMPORT_RE = re.compile(r"""import\s+([^;]*?)\s+from\s+['"]([^'"]+)['"]""")
_NAMED_ALIAS_RE = re.compile(r"""([A-Za-z_$][\w$]*)\s+as\s+([A-Za-z_$][\w$]*)""")
_FUNC_DECL_RE = re.compile(
    r"""(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::\s*[^\{]+)?\{""",
)
_ARROW_DECL_RE = re.compile(
    r"""(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::\s*[^=]+)?=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::\s*[^=]+)?=>\s*\{""",
)
_CLASS_DECL_RE = re.compile(
    r"""(?:export\s+(?:default\s+)?)?class\s+([A-Za-z_$][\w$]*)(?:\s+(?:extends|implements)\s+[A-Za-z_$][\w$.<> ,]*)?\s*\{""",
)
_METHOD_DECL_RE = re.compile(
    r"""(?:^|[\n;{}])\s*(?:(?:public|private|protected|static|async|override|readonly|get|set)\s+)*([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::\s*[^\{]+)?\{""",
)
_CLASS_ARROW_PROP_RE = re.compile(
    r"""(?:^|[\n;{}])\s*(?:(?:public|private|protected|static|override|readonly)\s+)*([A-Za-z_$][\w$]*)\s*(?::\s*[^=]+)?=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::\s*[^=]+)?=>\s*\{""",
)
_CALL_RE = re.compile(r"""(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(""")
_METHOD_CALL_RE = re.compile(r"""\.([A-Za-z_$][\w$]*)\s*\(""")


def _extract_svelte_scripts(source: str) -> str:
    """Keep only <script> contents while preserving original line numbers."""
    chars = list(source)
    keep = [False] * len(chars)
    for match in _SVELTE_SCRIPT_RE.finditer(source):
        for idx in range(match.start(1), match.end(1)):
            keep[idx] = True

    return "".join(ch if (keep[idx] or ch == "\n") else " " for idx, ch in enumerate(chars))


def _strip_comments_and_strings(code: str) -> str:
    """Replace comments and string/template contents with spaces, keeping newlines."""
    out: list[str] = []
    i = 0
    n = len(code)
    while i < n:
        ch = code[i]
        nxt = code[i + 1] if i + 1 < n else ""

        if ch == "/" and nxt == "/":
            while i < n and code[i] != "\n":
                out.append(" ")
                i += 1
            continue

        if ch == "/" and nxt == "*":
            out.append("  ")
            i += 2
            while i < n and not (code[i] == "*" and i + 1 < n and code[i + 1] == "/"):
                out.append("\n" if code[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append("  ")
                i += 2
            continue

        if ch in ('"', "'", "`"):
            quote = ch
            out.append(quote)
            i += 1
            while i < n:
                c = code[i]
                if c == "\\":
                    out.append(" ")
                    if i + 1 < n:
                        out.append("\n" if code[i + 1] == "\n" else " ")
                        i += 2
                    else:
                        i += 1
                    continue
                if c == quote:
                    out.append(quote)
                    i += 1
                    break
                out.append("\n" if c == "\n" else " ")
                i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def _find_matching_brace(code: str, open_idx: int) -> int:
    """Return index of closing '}' matching the '{' at open_idx."""
    depth = 0
    for idx in range(open_idx, len(code)):
        if code[idx] == "{":
            depth += 1
        elif code[idx] == "}":
            depth -= 1
            if depth == 0:
                return idx
    return len(code) - 1


def _build_line_index(code: str) -> list[int]:
    """Build sorted byte/char offsets of all newlines for O(log L) line lookup."""
    return [idx for idx, ch in enumerate(code) if ch == "\n"]


def _line_at(newlines: list[int], idx: int) -> int:
    return bisect.bisect_left(newlines, idx) + 1


def _extract_calls(body: str) -> list[str]:
    calls = [
        m.group(1)
        for m in _CALL_RE.finditer(body)
        if m.group(1) not in _KEYWORDS
    ]
    calls.extend(
        m.group(1)
        for m in _METHOD_CALL_RE.finditer(body)
        if m.group(1) not in _KEYWORDS
    )
    return calls


def analyze_js(source: str, rel_path: str, language: str = "javascript") -> ParsedFile:
    """Analyze JS/TS/Svelte source code for symbols, methods, aliases, and call sites."""
    working = _extract_svelte_scripts(source) if language == "svelte" else source

    imports: list[str] = []
    aliases: dict[str, str] = {}
    for clause, mod in _IMPORT_RE.findall(working):
        imports.append(mod)
        for orig, alias in _NAMED_ALIAS_RE.findall(clause):
            aliases[alias] = orig

    clean = _strip_comments_and_strings(working)
    newlines = _build_line_index(clean)

    # Collect raw spans: (decl_start, open_brace, close_brace, name, kind)
    raw_spans: list[tuple[int, int, int, str, str]] = []
    seen_open_braces: set[int] = set()

    def add_span(name: str, kind: str, decl_start: int, open_brace: int) -> int:
        close_idx = _find_matching_brace(clean, open_brace)
        if open_brace in seen_open_braces:
            return close_idx
        seen_open_braces.add(open_brace)
        raw_spans.append((decl_start, open_brace, close_idx, name, kind))
        return close_idx

    # 1. Classes and direct class body methods/arrow properties
    for match in _CLASS_DECL_RE.finditer(clean):
        class_open = match.end() - 1
        class_close = add_span(match.group(1), "class", match.start(), class_open)
        class_body = clean[class_open + 1 : class_close]
        offset = class_open + 1

        # Precompute depth array once for O(1) depth check at any match position
        depth_at: list[int] = [0] * (len(class_body) + 1)
        cur_depth = 0
        for i, ch in enumerate(class_body):
            depth_at[i] = cur_depth
            if ch == "{":
                cur_depth += 1
            elif ch == "}":
                cur_depth -= 1
        depth_at[len(class_body)] = cur_depth

        for pat in (_METHOD_DECL_RE, _CLASS_ARROW_PROP_RE):
            for m in pat.finditer(class_body):
                method_name = m.group(1)
                if method_name in _KEYWORDS:
                    continue
                # Check brace depth right at the method identifier start
                if depth_at[m.start(1)] != 0:
                    continue
                add_span(method_name, "method", offset + m.start(1), offset + m.end() - 1)

    # 2. Functions and const/let/var arrow functions (including nested scopes)
    for pattern in (_FUNC_DECL_RE, _ARROW_DECL_RE):
        for match in pattern.finditer(clean):
            add_span(match.group(1), "function", match.start(), match.end() - 1)

    # Sort by source position and compute hierarchical qualified IDs
    raw_spans.sort(key=lambda sp: sp[0])
    scope_stack: list[tuple[int, str, str]] = []  # (close_idx, name, kind)

    symbols: list[SymbolNode] = []
    raw_calls: dict[str, list[str]] = {}
    seen_ids: set[str] = set()

    for decl_start, open_brace, close_idx, name, kind in raw_spans:
        while scope_stack and scope_stack[-1][0] < decl_start:
            scope_stack.pop()

        parent = scope_stack[-1][1] if scope_stack else None
        qname = ".".join([*(s[1] for s in scope_stack), name])
        sym_id = f"{rel_path}::{qname}"
        if sym_id in seen_ids:
            continue
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

        scope_stack.append((close_idx, name, kind))

    return ParsedFile(symbols=symbols, raw_calls=raw_calls, imports=imports, aliases=aliases)
