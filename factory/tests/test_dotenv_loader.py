"""Tests for the bash `_load_dotenv_no_override` function shared across
bin/devbrain, bin/devbrain-onboard, and mcp-server/run.sh.

These three scripts each carry their own copy of the function (kept in
sync by hand). We test by extracting the function from bin/devbrain
and exercising it in a bash subprocess against a controlled .env file
and pre-set caller env.

The contract under test:
  * Vars already set in env are preserved (env > .env precedence,
    matching the .env.example doc comment).
  * Quoted values have their surrounding quotes stripped (both " and ').
  * `export FOO=bar` lines are accepted.
  * Comment lines (#) and blank lines are ignored.
  * Values containing `=` are preserved verbatim past the first `=`.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEVBRAIN_SCRIPT = REPO_ROOT / "bin" / "devbrain"


def _extract_function() -> str:
    """Pull the _load_dotenv_no_override function definition out of
    bin/devbrain. Fails the test if the function name isn't found there,
    which is also a useful failsafe for future renames.
    """
    text = DEVBRAIN_SCRIPT.read_text()
    start = text.find("_load_dotenv_no_override() {")
    if start < 0:
        pytest.fail(
            f"_load_dotenv_no_override() not found in {DEVBRAIN_SCRIPT} — "
            "did the function get renamed?"
        )
    # Find the matching closing brace at column 0
    end = text.find("\n}\n", start)
    if end < 0:
        pytest.fail("could not locate end of _load_dotenv_no_override()")
    return text[start : end + 3]


def _run_bash_with_dotenv(
    envfile_contents: str,
    *,
    preset_env: dict[str, str] | None = None,
    probe_keys: list[str],
    tmp_path: Path,
) -> dict[str, str]:
    """Run a bash subprocess that sources the loader, applies it to a
    written .env, and prints each probe_key on its own line.

    Returns a dict of the values bash saw post-load. Empty strings are
    preserved; truly unset variables come back as `<UNSET>`.
    """
    envfile = tmp_path / ".env"
    envfile.write_text(envfile_contents)

    fn = _extract_function()

    # Compose the bash script: load the function, run it, dump probes.
    # We use `${VAR-<UNSET>}` so a truly-unset var is distinguishable
    # from one set to empty string.
    probes = "\n".join(
        f'echo "{k}=${{{k}-<UNSET>}}"' for k in probe_keys
    )
    script = textwrap.dedent(f"""
        set -u
        {fn}
        _load_dotenv_no_override {envfile!s}
        {probes}
    """)

    env = os.environ.copy()
    # Strip any leftover test pollution
    for k in probe_keys:
        env.pop(k, None)
    if preset_env:
        env.update(preset_env)

    result = subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    out = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


def test_caller_env_wins_over_dotenv(tmp_path):
    """env > .env: a var pre-set in env is preserved verbatim."""
    contents = "ALREADY_SET=from_dotenv\n"
    out = _run_bash_with_dotenv(
        contents,
        preset_env={"ALREADY_SET": "from_caller"},
        probe_keys=["ALREADY_SET"],
        tmp_path=tmp_path,
    )
    assert out["ALREADY_SET"] == "from_caller"


def test_dotenv_fills_in_missing(tmp_path):
    """.env fills in vars that weren't already set in env."""
    contents = "KEY_FROM_FILE=from_env_file\n"
    out = _run_bash_with_dotenv(
        contents,
        probe_keys=["KEY_FROM_FILE"],
        tmp_path=tmp_path,
    )
    assert out["KEY_FROM_FILE"] == "from_env_file"


def test_comments_and_blanks_ignored(tmp_path):
    """Lines starting with # and blank lines are skipped silently."""
    contents = textwrap.dedent("""
        # a comment

           # indented comment
        REAL_KEY=real_value
    """)
    out = _run_bash_with_dotenv(
        contents,
        probe_keys=["REAL_KEY"],
        tmp_path=tmp_path,
    )
    assert out["REAL_KEY"] == "real_value"


def test_double_quoted_value_has_quotes_stripped(tmp_path):
    contents = 'KEY_WITH_QUOTES="quoted_value"\n'
    out = _run_bash_with_dotenv(
        contents,
        probe_keys=["KEY_WITH_QUOTES"],
        tmp_path=tmp_path,
    )
    assert out["KEY_WITH_QUOTES"] == "quoted_value"


def test_single_quoted_value_has_quotes_stripped(tmp_path):
    contents = "KEY_WITH_SQUOTES='squoted_value'\n"
    out = _run_bash_with_dotenv(
        contents,
        probe_keys=["KEY_WITH_SQUOTES"],
        tmp_path=tmp_path,
    )
    assert out["KEY_WITH_SQUOTES"] == "squoted_value"


def test_export_prefix_stripped(tmp_path):
    """Bash-style `export KEY=value` lines work too."""
    contents = "export PREFIXED=prefixed_value\n"
    out = _run_bash_with_dotenv(
        contents,
        probe_keys=["PREFIXED"],
        tmp_path=tmp_path,
    )
    assert out["PREFIXED"] == "prefixed_value"


def test_value_with_equals_preserved(tmp_path):
    """A value containing `=` (e.g. a URL with a query) keeps everything
    after the first `=`."""
    contents = "URL=postgres://user:pass=word@host/db?ssl=true\n"
    out = _run_bash_with_dotenv(
        contents,
        probe_keys=["URL"],
        tmp_path=tmp_path,
    )
    assert out["URL"] == "postgres://user:pass=word@host/db?ssl=true"


def test_empty_value_allowed(tmp_path):
    """KEY= with nothing after is a valid empty string, not a syntax
    error and not skipped."""
    contents = "EMPTY_KEY=\nOTHER=non_empty\n"
    out = _run_bash_with_dotenv(
        contents,
        probe_keys=["EMPTY_KEY", "OTHER"],
        tmp_path=tmp_path,
    )
    assert out["EMPTY_KEY"] == ""
    assert out["OTHER"] == "non_empty"


def test_caller_empty_string_wins(tmp_path):
    """If caller pre-set a var to empty string, .env should NOT overwrite
    it. (Empty string is a deliberate value; "" != "unset".)"""
    contents = "DELIBERATELY_EMPTY=from_dotenv\n"
    out = _run_bash_with_dotenv(
        contents,
        preset_env={"DELIBERATELY_EMPTY": ""},
        probe_keys=["DELIBERATELY_EMPTY"],
        tmp_path=tmp_path,
    )
    assert out["DELIBERATELY_EMPTY"] == ""


def test_missing_dotenv_is_silent_noop(tmp_path):
    """No .env at all — loader returns 0, no error, no vars set."""
    out = _run_bash_with_dotenv(
        "",  # we'll write empty, then delete it
        probe_keys=["ANYTHING"],
        tmp_path=tmp_path,
    )
    (tmp_path / ".env").unlink(missing_ok=True)
    # Re-run after the unlink
    fn = _extract_function()
    result = subprocess.run(
        ["bash", "-c", f"set -u\n{fn}\n_load_dotenv_no_override {tmp_path}/.env\necho ok"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "ok" in result.stdout


def test_function_present_in_all_three_scripts():
    """All three entry-point scripts must carry the same loader function
    (kept in sync by hand). Catches drift."""
    for rel in ("bin/devbrain", "bin/devbrain-onboard", "mcp-server/run.sh"):
        script = REPO_ROOT / rel
        assert script.exists(), f"missing entry-point: {rel}"
        text = script.read_text()
        assert "_load_dotenv_no_override() {" in text, (
            f"{rel} missing the shared _load_dotenv_no_override loader — "
            f"its .env behavior will drift from the other entrypoints"
        )
