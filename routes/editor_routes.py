"""
Code Editor API routes.
Provides file browsing, reading, writing, and backup for the web IDE.
"""
import logging
import os
import time
import shutil
from pathlib import Path
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional

logger = logging.getLogger("routes.editor")

# Project root — all paths are relative to this
PROJECT_ROOT = Path("/app")

# Directories the editor can access
ALLOWED_DIRS = [
    "",              # Root .py files (main.py, device.py, mqtt.py etc.)
    "core",
    "routes",
    "modules",
    "handlers",
    "static",
    "static/js",
    "static/js/modal",
    "static/css",
    "config",
    "docs",
    "data",
]

# Editable file extensions
EDITABLE_EXTENSIONS = {
    ".py", ".js", ".css", ".html", ".yaml", ".yml",
    ".json", ".md", ".txt", ".conf", ".sh",
}

# Max file size for editing (2MB)
MAX_FILE_SIZE = 2 * 1024 * 1024

# Backup directory
BACKUP_DIR = PROJECT_ROOT / ".editor_backups"


class FileSaveRequest(BaseModel):
    path: str
    content: str
    create_backup: bool = True


class FileCreateRequest(BaseModel):
    path: str
    content: str = ""


def _resolve_path(relative_path: str) -> Optional[Path]:
    """Resolve and validate a file path. Returns None if outside project."""
    try:
        # Normalise and resolve
        clean = relative_path.replace("\\", "/").lstrip("/")
        full = (PROJECT_ROOT / clean).resolve()

        # Must be within project root
        if not str(full).startswith(str(PROJECT_ROOT.resolve())):
            return None

        return full
    except Exception:
        return None


def _is_editable(path: Path) -> bool:
    """Check if a file is editable by extension."""
    return path.suffix.lower() in EDITABLE_EXTENSIONS


def _get_file_info(path: Path, relative_to: Path = PROJECT_ROOT) -> dict:
    """Build file info dict."""
    rel = str(path.relative_to(relative_to))
    stat = path.stat()
    return {
        "name": path.name,
        "path": rel,
        "size": stat.st_size,
        "modified": int(stat.st_mtime),
        "editable": _is_editable(path),
        "is_dir": path.is_dir(),
        "extension": path.suffix.lower(),
    }


def register_editor_routes(app: FastAPI, get_zigbee_service):
    """Register code editor API routes."""

    @app.get("/api/editor/tree")
    async def get_file_tree():
        """Get project file tree for the sidebar."""
        tree = []
        for rel_dir in ALLOWED_DIRS:
            dir_path = PROJECT_ROOT / rel_dir
            if not dir_path.exists():
                continue

            dir_entry = {
                "name": rel_dir or "root",
                "path": rel_dir or ".",
                "is_dir": True,
                "children": [],
            }

            try:
                for item in sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
                    if item.name.startswith(".") or item.name == "__pycache__":
                        continue

                    if item.is_file() and _is_editable(item):
                        dir_entry["children"].append(_get_file_info(item))
                    # Subdirs are listed as their own sections — don't add folder entries
            except PermissionError:
                continue

            if dir_entry["children"]:
                tree.append(dir_entry)

        return {"success": True, "tree": tree}

    @app.get("/api/editor/file")
    async def read_file(path: str):
        """Read a file's content."""
        full = _resolve_path(path)
        if not full or not full.exists():
            return {"success": False, "error": "File not found"}
        if not full.is_file():
            return {"success": False, "error": "Not a file"}
        if not _is_editable(full):
            return {"success": False, "error": f"File type not editable: {full.suffix}"}
        if full.stat().st_size > MAX_FILE_SIZE:
            return {"success": False, "error": "File too large (max 2MB)"}

        try:
            content = full.read_text(encoding="utf-8")
            return {
                "success": True,
                "path": path,
                "content": content,
                "size": len(content),
                "modified": int(full.stat().st_mtime),
                "language": _detect_language(full),
            }
        except UnicodeDecodeError:
            return {"success": False, "error": "Binary file, cannot edit"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/editor/save")
    async def save_file(request: FileSaveRequest):
        """Save file content with optional backup."""
        full = _resolve_path(request.path)
        if not full:
            return {"success": False, "error": "Invalid path"}
        if not _is_editable(full):
            return {"success": False, "error": f"File type not editable: {full.suffix}"}

        # Backup existing file
        backup_path = None
        if request.create_backup and full.exists():
            try:
                BACKUP_DIR.mkdir(parents=True, exist_ok=True)
                ts = time.strftime("%Y%m%d_%H%M%S")
                safe_name = request.path.replace("/", "_").replace("\\", "_")
                backup_name = f"{safe_name}.{ts}.bak"
                backup_path = BACKUP_DIR / backup_name
                shutil.copy2(full, backup_path)
                logger.info(f"Backup created: {backup_path}")
            except Exception as e:
                logger.warning(f"Backup failed (saving anyway): {e}")

        try:
            # Ensure parent directory exists
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(request.content, encoding="utf-8")
            logger.info(f"File saved via editor: {request.path} ({len(request.content)} bytes)")
            return {
                "success": True,
                "path": request.path,
                "size": len(request.content),
                "backup": str(backup_path.relative_to(PROJECT_ROOT)) if backup_path else None,
            }
        except Exception as e:
            logger.error(f"File save failed: {e}")
            return {"success": False, "error": str(e)}

    @app.post("/api/editor/create")
    async def create_file(request: FileCreateRequest):
        """Create a new file."""
        full = _resolve_path(request.path)
        if not full:
            return {"success": False, "error": "Invalid path"}
        if full.exists():
            return {"success": False, "error": "File already exists"}
        if not _is_editable(full):
            return {"success": False, "error": f"File type not allowed: {full.suffix}"}

        try:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(request.content, encoding="utf-8")
            logger.info(f"File created via editor: {request.path}")
            return {"success": True, "path": request.path}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.post("/api/editor/validate")
    async def validate_file(data: dict):
        """
        Validate file content without saving.
        Returns syntax errors with line/column for Monaco markers.
        Supports: Python (ast), JSON, YAML, JavaScript, HTML.
        """
        try:
            content = data.get("content", "")
            language = data.get("language", "")
            path = data.get("path", "")

            # Auto-detect language from path if not provided
            if not language and path:
                language = _detect_language(Path(path))

            errors = []

            if language == "python":
                errors = _validate_python(content)
            elif language == "json":
                errors = _validate_json(content)
            elif language == "yaml":
                errors = _validate_yaml(content)
            elif language == "javascript":
                errors = _validate_javascript(content)
            elif language == "html":
                errors = _validate_html(content)
            else:
                return {"success": True, "errors": [], "message": f"No validator for {language}"}

            return {
                "success": True,
                "valid": len([e for e in errors if e.get("severity") == "error"]) == 0,
                "errors": errors,
                "language": language,
            }
        except Exception as e:
            logger.error(f"Validation error: {e}")
            return {"success": False, "error": str(e)}

    @app.get("/api/editor/backups")
    async def list_backups(path: str = None):
        """List backups, optionally filtered by original file path."""
        if not BACKUP_DIR.exists():
            return {"success": True, "backups": []}

        backups = []
        for f in sorted(BACKUP_DIR.iterdir(), reverse=True):
            if not f.is_file() or not f.name.endswith(".bak"):
                continue
            if path:
                safe_name = path.replace("/", "_").replace("\\", "_")
                if not f.name.startswith(safe_name + "."):
                    continue
            backups.append({
                "name": f.name,
                "size": f.stat().st_size,
                "created": int(f.stat().st_mtime),
            })

        return {"success": True, "backups": backups[:50]}

    @app.post("/api/editor/restore")
    async def restore_backup(data: dict):
        """Restore a file from backup."""
        backup_name = data.get("backup")
        target_path = data.get("path")

        if not backup_name or not target_path:
            return {"success": False, "error": "backup and path required"}

        backup_file = BACKUP_DIR / backup_name
        target_file = _resolve_path(target_path)

        if not backup_file.exists():
            return {"success": False, "error": "Backup not found"}
        if not target_file:
            return {"success": False, "error": "Invalid target path"}

        try:
            shutil.copy2(backup_file, target_file)
            logger.info(f"Restored {target_path} from backup {backup_name}")
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @app.get("/api/editor/search")
    async def search_files(query: str, path: str = None):
        """Search for text across project files."""
        if len(query) < 2:
            return {"success": False, "error": "Query too short (min 2 chars)"}

        results = []
        search_dirs = [path] if path else ALLOWED_DIRS

        for rel_dir in search_dirs:
            dir_path = PROJECT_ROOT / rel_dir
            if not dir_path.exists():
                continue

            for item in dir_path.iterdir():
                if not item.is_file() or not _is_editable(item):
                    continue
                if item.stat().st_size > MAX_FILE_SIZE:
                    continue

                try:
                    content = item.read_text(encoding="utf-8")
                    for line_num, line in enumerate(content.splitlines(), 1):
                        if query.lower() in line.lower():
                            results.append({
                                "path": str(item.relative_to(PROJECT_ROOT)),
                                "line": line_num,
                                "text": line.strip()[:200],
                            })
                            if len(results) >= 100:
                                return {"success": True, "results": results, "truncated": True}
                except (UnicodeDecodeError, PermissionError):
                    continue

        return {"success": True, "results": results, "truncated": False}


def _detect_language(path: Path) -> str:
    """Map file extension to Monaco language ID."""
    return {
        ".py": "python",
        ".js": "javascript",
        ".css": "css",
        ".html": "html",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".json": "json",
        ".md": "markdown",
        ".sh": "shell",
        ".txt": "plaintext",
        ".conf": "ini",
    }.get(path.suffix.lower(), "plaintext")


def _validate_python(content: str) -> list:
    """Validate Python syntax using ast.parse."""
    import ast
    import re

    errors = []

    # Phase 1: ast.parse — catches all syntax errors with line/col
    try:
        ast.parse(content)
    except SyntaxError as e:
        errors.append({
            "line": e.lineno or 1,
            "column": e.offset or 1,
            "endLine": e.end_lineno or e.lineno or 1,
            "endColumn": e.end_offset or (e.offset + 1 if e.offset else 2),
            "message": e.msg,
            "severity": "error",
        })
        return errors

    # Phase 2: Check for used-but-not-imported stdlib modules
    STDLIB_MODULES = {
        "asyncio", "json", "os", "sys", "time", "logging", "re",
        "subprocess", "shutil", "signal", "uuid", "traceback",
        "threading", "functools", "pathlib", "datetime",
    }
    imported_names = set()
    tree = ast.parse(content)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_names.add(node.module.split(".")[0])
            for alias in node.names:
                imported_names.add(alias.asname or alias.name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            used = node.value.id
            if used in STDLIB_MODULES and used not in imported_names:
                errors.append({
                    "line": node.lineno,
                    "column": node.col_offset + 1,
                    "endLine": node.lineno,
                    "endColumn": node.col_offset + len(used) + 1,
                    "message": f"'{used}' is used but not imported — will cause NameError at runtime",
                    "severity": "error",
                })


    # Phase 3: Basic warnings
    lines = content.splitlines()
    for i, line in enumerate(lines, 1):
        stripped = line.rstrip()

        if stripped != line and stripped:
            errors.append({
                "line": i, "column": len(stripped) + 1,
                "message": "Trailing whitespace",
                "severity": "warning",
            })

        if line and line[0] in (' ', '\t'):
            leading = line[:len(line) - len(line.lstrip())]
            if '\t' in leading and ' ' in leading:
                errors.append({
                    "line": i, "column": 1,
                    "message": "Mixed tabs and spaces in indentation",
                    "severity": "warning",
                })

        if 'import *' in line:
            errors.append({
                "line": i, "column": line.index('import *') + 1,
                "message": "Wildcard import (import *) — consider explicit imports",
                "severity": "info",
            })

        if re.match(r'\s*except\s*:', stripped):
            errors.append({
                "line": i, "column": 1,
                "message": "Bare except — consider catching specific exceptions",
                "severity": "warning",
            })

    return errors


def _validate_json(content: str) -> list:
    """Validate JSON syntax."""
    import json

    try:
        json.loads(content)
        return []
    except json.JSONDecodeError as e:
        return [{
            "line": e.lineno,
            "column": e.colno,
            "message": e.msg,
            "severity": "error",
        }]


def _validate_yaml(content: str) -> list:
    """Validate YAML syntax."""
    import yaml

    try:
        yaml.safe_load(content)
        return []
    except yaml.YAMLError as e:
        line = 1
        col = 1
        msg = str(e)
        if hasattr(e, 'problem_mark') and e.problem_mark:
            line = e.problem_mark.line + 1
            col = e.problem_mark.column + 1
            msg = getattr(e, 'problem', str(e))
        return [{
            "line": line,
            "column": col,
            "message": msg,
            "severity": "error",
        }]


def _validate_javascript(content: str) -> list:
    """
    JavaScript validation — bracket/brace/paren matching with proper handling of:
      - Template literals with ${...} expressions (nested)
      - Regex literals /.../ (including character classes [...])
      - Single-line // and multi-line /* */ comments
      - String escapes in all string types
      - Spread syntax [...x]
      - Optional chaining ?.( and ?.[
    """
    errors = []
    lines = content.splitlines()
    chars = list(content)
    n = len(chars)

    # We work on the flat character stream for accurate parsing,
    # but track line/col for error reporting.
    # Build a line/col map for each char index.
    line_of = []
    col_of = []
    current_line = 1
    current_col = 1
    for ch in chars:
        line_of.append(current_line)
        col_of.append(current_col)
        if ch == '\n':
            current_line += 1
            current_col = 1
        else:
            current_col += 1

    stack = []              # (char, line, col)
    template_depth = []     # stack of brace-depth when entering ${...}
    i = 0

    def peek(offset=1):
        pos = i + offset
        return chars[pos] if pos < n else ''

    def _could_be_regex():
        """
        Heuristic: a '/' starts a regex if the previous meaningful token is one of:
          - start of input
        - an operator or punctuation that cannot end an expression
          ( , ; = [ ! & | ? : { } ~ ^ % + - * / > < return typeof void delete
          instanceof in new throw case
        We scan backwards from current position skipping whitespace/newlines.
        """
        j = i - 1
        while j >= 0 and chars[j] in (' ', '\t', '\n', '\r'):
            j -= 1
        if j < 0:
            return True
        c = chars[j]
        # These characters before / always mean regex
        if c in ('=', '(', '[', '{', '}', ';', ',', '!', '&', '|',
                 '?', ':', '~', '^', '%', '+', '-', '*', '<', '>',
                 '\n'):
            return True
        # Check for keyword endings: return, typeof, void, delete, etc.
        # Look for a word boundary
        if c.isalpha():
            word_end = j
            while j >= 0 and (chars[j].isalpha() or chars[j] == '_'):
                j -= 1
            word = ''.join(chars[j + 1:word_end + 1])
            if word in ('return', 'typeof', 'void', 'delete', 'instanceof',
                        'in', 'new', 'throw', 'case', 'yield', 'await',
                        'of', 'else'):
                return True
        return False

    while i < n:
        c = chars[i]

        # ── Single-line comment ──
        if c == '/' and peek() == '/':
            # Skip to end of line
            while i < n and chars[i] != '\n':
                i += 1
            continue

        # ── Multi-line comment ──
        if c == '/' and peek() == '*':
            i += 2
            while i < n:
                if chars[i] == '*' and peek() == '/':
                    i += 2
                    break
                i += 1
            continue

        # ── Single/double quoted strings ──
        if c in ('"', "'"):
            quote = c
            i += 1
            while i < n:
                if chars[i] == '\\':
                    i += 2  # skip escape
                    continue
                if chars[i] == quote:
                    i += 1
                    break
                if chars[i] == '\n':
                    # Unterminated string (single-line)
                    break
                i += 1
            continue

        # ── Template literal ──
        if c == '`':
            i += 1
            while i < n:
                if chars[i] == '\\':
                    i += 2
                    continue
                if chars[i] == '`':
                    i += 1
                    break
                if chars[i] == '$' and peek() == '{':
                    # Enter template expression
                    # Record current stack depth so we know when the
                    # matching } returns us to the template
                    template_depth.append(len(stack))
                    stack.append(('{', line_of[i], col_of[i]))
                    i += 2  # skip ${
                    break  # return to main loop to parse the expression
                i += 1
            continue

        # ── Regex literal ──
        if c == '/' and _could_be_regex():
            i += 1  # skip opening /
            in_char_class = False
            while i < n:
                rc = chars[i]
                if rc == '\\':
                    i += 2  # skip escaped char in regex
                    continue
                if rc == '[':
                    in_char_class = True
                elif rc == ']':
                    in_char_class = False
                elif rc == '/' and not in_char_class:
                    i += 1
                    # Skip regex flags (g, i, m, s, u, y, d, v)
                    while i < n and chars[i].isalpha():
                        i += 1
                    break
                elif rc == '\n':
                    # Unterminated regex
                    break
                i += 1
            continue

        # ── Bracket tracking ──
        if c in ('(', '[', '{'):
            stack.append((c, line_of[i], col_of[i]))
            i += 1
            continue

        if c in (')', ']', '}'):
            expected_opener = {')': '(', ']': '[', '}': '{'}[c]

            if stack and stack[-1][0] == expected_opener:
                stack.pop()

                # If closing } and we are inside a template expression,
                # check if this } returns us to the template literal
                if c == '}' and template_depth and len(stack) == template_depth[-1]:
                    template_depth.pop()
                    # Resume parsing inside the template literal
                    i += 1
                    while i < n:
                        if chars[i] == '\\':
                            i += 2
                            continue
                        if chars[i] == '`':
                            i += 1
                            break
                        if chars[i] == '$' and peek() == '{':
                            template_depth.append(len(stack))
                            stack.append(('{', line_of[i], col_of[i]))
                            i += 2
                            break
                        i += 1
                    continue
            elif stack:
                match_map = {'(': ')', '[': ']', '{': '}'}
                expected = match_map.get(stack[-1][0], '?')
                errors.append({
                    "line": line_of[i], "column": col_of[i],
                    "message": f"Unexpected '{c}' — expected '{expected}'",
                    "severity": "error",
                })
            else:
                errors.append({
                    "line": line_of[i], "column": col_of[i],
                    "message": f"Unexpected '{c}' — no matching opening bracket",
                    "severity": "error",
                })
            i += 1
            continue

        i += 1

    # Report unclosed brackets
    for char, ln, col in stack:
        errors.append({
            "line": ln, "column": col,
            "message": f"Unclosed '{char}'",
            "severity": "error",
        })

    # ── Phase 2: line-level warnings ──
    for idx, line in enumerate(lines, 1):
        stripped = line.rstrip()

        # Trailing whitespace
        if stripped != line and stripped:
            errors.append({
                "line": idx, "column": len(stripped) + 1,
                "message": "Trailing whitespace",
                "severity": "warning",
            })

    return errors


def _validate_html(content: str) -> list:
    """Basic HTML validation — unclosed tags and common issues."""
    from html.parser import HTMLParser

    errors = []

    class Validator(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tag_stack = []  # (tag, line, col)
            self.self_closing = {
                'br', 'hr', 'img', 'input', 'meta', 'link',
                'area', 'base', 'col', 'embed', 'source', 'track', 'wbr'
            }

        def handle_starttag(self, tag, attrs):
            if tag.lower() not in self.self_closing:
                line, col = self.getpos()
                self.tag_stack.append((tag.lower(), line, col))

        def handle_endtag(self, tag):
            line, col = self.getpos()
            if self.tag_stack and self.tag_stack[-1][0] == tag.lower():
                self.tag_stack.pop()
            elif self.tag_stack:
                errors.append({
                    "line": line, "column": col,
                    "message": f"Unexpected </{tag}> — expected </{self.tag_stack[-1][0]}>",
                    "severity": "error",
                })
            else:
                errors.append({
                    "line": line, "column": col,
                    "message": f"Unexpected </{tag}> — no matching opening tag",
                    "severity": "error",
                })

    try:
        v = Validator()
        v.feed(content)
        # Report unclosed tags
        for tag, line, col in v.tag_stack:
            errors.append({
                "line": line, "column": col,
                "message": f"Unclosed <{tag}>",
                "severity": "warning",
            })
    except Exception as e:
        errors.append({
            "line": 1, "column": 1,
            "message": f"Parse error: {e}",
            "severity": "error",
        })

    return errors