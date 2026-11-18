"""Language analyzer registry."""

from __future__ import annotations

from pathlib import Path

from .base import ParsedFile
from .js_analyzer import analyze_js
from .python_analyzer import analyze_python

EXTENSION_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".svelte": "svelte",
}


def analyze_file(path: Path, rel_path: str) -> tuple[str, int, ParsedFile] | None:
    """Analyze a source file if its extension is supported."""
    lang = EXTENSION_LANGUAGE.get(path.suffix.lower())
    if not lang:
        return None

    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    line_count = source.count("\n") + (1 if source else 0)
    if lang == "python":
        parsed = analyze_python(source, rel_path)
    else:
        parsed = analyze_js(source, rel_path, language=lang)
    return lang, line_count, parsed
