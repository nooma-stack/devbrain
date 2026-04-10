"""Tests for extracting file paths from plan artifacts."""
import pytest
from plan_parser import extract_files_from_plan


def test_extracts_simple_create_paths():
    plan = """
    ## Files
    - Create: `src/auth/login.py`
    - Modify: `src/auth/middleware.py`
    - Test: `tests/test_login.py`
    """
    files = extract_files_from_plan(plan)
    assert "src/auth/login.py" in files
    assert "src/auth/middleware.py" in files
    assert "tests/test_login.py" in files


def test_extracts_paths_from_code_blocks():
    plan = """
    ### Task 1
    Create `factory/new_module.py` with this content:
    ```python
    def foo(): pass
    ```
    """
    files = extract_files_from_plan(plan)
    assert "factory/new_module.py" in files


def test_ignores_non_file_backticks():
    plan = """
    Call the `FactoryDB.get_job` method.
    Use the `foo` variable.
    Create: `src/real_file.py`
    """
    files = extract_files_from_plan(plan)
    assert "src/real_file.py" in files
    assert "FactoryDB.get_job" not in files
    assert "foo" not in files


def test_deduplicates_paths():
    plan = """
    - Create: `src/foo.py`
    - Modify: `src/foo.py`
    - Also update `src/foo.py`
    """
    files = extract_files_from_plan(plan)
    # sorted list should have exactly one occurrence
    assert files.count("src/foo.py") == 1


def test_extracts_paths_with_subdirs():
    plan = """
    - `mcp-server/src/tools/new_tool.ts`
    - `migrations/005_new_migration.sql`
    """
    files = extract_files_from_plan(plan)
    assert "mcp-server/src/tools/new_tool.ts" in files
    assert "migrations/005_new_migration.sql" in files


def test_empty_plan_returns_empty_list():
    assert extract_files_from_plan("") == []
    assert extract_files_from_plan("Just prose with no code.") == []


def test_strips_trailing_punctuation():
    plan = """
    Edit `src/foo.py`, then run tests.
    """
    files = extract_files_from_plan(plan)
    assert "src/foo.py" in files
    assert "src/foo.py," not in files
