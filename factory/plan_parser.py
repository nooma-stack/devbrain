"""Plan artifact parser for extracting file paths from factory plan documents.

The planning phase produces a plan doc listing files to create/modify. This
module extracts those file paths so the file registry can lock them before
implementation begins.
"""
import re

# File extensions we recognize as "real" source/config files.
_VALID_EXTENSIONS = {
    "py", "ts", "tsx", "js", "jsx", "sql", "md", "yaml", "yml", "json",
    "toml", "sh", "go", "rs", "java", "c", "cpp", "h", "css", "html",
    "txt", "env",
}

# Characters we strip from the ends of a candidate path (trailing punctuation
# like commas, periods, semicolons, parentheses from prose).
_TRAILING_STRIP = ".,;:!?)]}>\"'"
_LEADING_STRIP = "([{<\"'"

# Matches content inside single backticks.
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")

# Matches action keywords followed by a path. The path may be backticked or
# bare. Captures the path portion (with or without backticks).
_ACTION_RE = re.compile(
    r"(?i)\b(?:create|modify|test|edit|update|add)\s*:\s*`?([^\s`,;]+)`?"
)


def _looks_like_file_path(candidate: str) -> bool:
    """Return True if `candidate` looks like a real file path.

    A file path is non-empty, contains no spaces, has a dot, and the text
    after the final dot matches one of the recognized extensions. This
    excludes things like `FactoryDB.get_job` (the "extension" `get_job`
    isn't in our whitelist) and bare identifiers like `foo`.
    """
    if not candidate or " " in candidate:
        return False
    if "." not in candidate:
        return False
    ext = candidate.rsplit(".", 1)[-1].lower()
    return ext in _VALID_EXTENSIONS


def _clean(candidate: str) -> str:
    """Strip surrounding punctuation/quotes from a candidate token."""
    return candidate.strip().lstrip(_LEADING_STRIP).rstrip(_TRAILING_STRIP)


def extract_files_from_plan(plan_text: str) -> list[str]:
    """Extract a sorted, deduplicated list of file paths from a plan doc.

    Pulls paths from:
      1. Backticked tokens (e.g. `src/foo.py`)
      2. Action keywords (Create/Modify/Test/Edit/Update/Add: path)

    Only tokens matching `_looks_like_file_path` are returned, which filters
    out method references, variable names, and other non-path backticks.
    """
    if not plan_text:
        return []

    found: set[str] = set()

    # 1. Action keywords (case-insensitive). Run first so we catch bare
    #    (non-backticked) paths after keywords like `Create: path`.
    for match in _ACTION_RE.finditer(plan_text):
        candidate = _clean(match.group(1))
        if _looks_like_file_path(candidate):
            found.add(candidate)

    # 2. Backticked tokens anywhere in the doc.
    for match in _BACKTICK_RE.finditer(plan_text):
        candidate = _clean(match.group(1))
        if _looks_like_file_path(candidate):
            found.add(candidate)

    return sorted(found)
