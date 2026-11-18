"""CodeAtlas: Zero-config local codebase architecture & call-graph visualizer."""

from .models import CallEdge, CodeGraph, FileSummary, SymbolNode
from .scanner import scan_directory

__version__ = "0.3.1"

__all__ = [
    "__version__",
    "CallEdge",
    "CodeGraph",
    "FileSummary",
    "SymbolNode",
    "scan_directory",
]
