"""Language analyzer registry."""

from __future__ import annotations

from pathlib import Path

from .base import ParsedFile
from .c_family_analyzer import analyze_c_family
from .js_analyzer import analyze_js
from .python_analyzer import analyze_python
from .treesitter_analyzer import analyze_with_treesitter, has_treesitter

MAX_ANALYZE_BYTES = 2_000_000

EXTENSION_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".svelte": "svelte",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
}

_C_FAMILY_LANGS = frozenset({"go", "rust", "java", "c", "cpp"})
_MINIFIABLE_LANGS = frozenset({"javascript", "typescript"})


def _looks_minified_source(source: str, lang: str) -> bool:
    """Detect minified JS/TS bundles from already-loaded source without re-opening the file."""
    if lang not in _MINIFIABLE_LANGS:
        return False
    pos = 0
    for _ in range(10):
        nl = source.find("\n", pos)
        line_len = (len(source) - pos) if nl == -1 else (nl - pos)
        if line_len > 700:
            return True
        if nl == -1:
            break
        pos = nl + 1
    return False


def analyze_file(path: Path, rel_path: str) -> tuple[str, int, ParsedFile] | None:
    """Analyze a source file if its extension is supported."""
    lang = EXTENSION_LANGUAGE.get(path.suffix.lower())
    if not lang:
        return None

    try:
        if path.stat().st_size > MAX_ANALYZE_BYTES:
            return None
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    if _looks_minified_source(source, lang):
        return None

    line_count = source.count("\n") + (1 if source else 0)
    if lang == "python":
        parsed = analyze_python(source, rel_path)
    else:
        parsed = analyze_with_treesitter(source, rel_path, language=lang)
        if parsed is None:
            if lang in _C_FAMILY_LANGS:
                parsed = analyze_c_family(source, rel_path, language=lang)
            else:
                parsed = analyze_js(source, rel_path, language=lang)
    return lang, line_count, parsed
