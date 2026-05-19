"""Install + configure the cognify launchd jobs on the current macOS user.

Reads source plist templates from this package's `launchd/` subdirectory,
substitutes the four required placeholders, writes the resulting plists
to `~/Library/LaunchAgents/` with `chmod 0600` on the two files that
carry Anthropic credentials, and optionally reloads `launchctl`.

Why this exists
---------------
The launchd plist templates in `factory/cognify/launchd/*.plist` contain
shell-style `${PLACEHOLDER}` tokens that launchd itself does *not*
substitute. Copying them directly into `~/Library/LaunchAgents/` leaves
the placeholders literal, which produces the runtime error:

    Error: Project '${PROJECT_SLUG}' not found.

This was the regression that broke cognify_extract on Mac Studio on
2026-05-11. This installer is the only correct way to render and place
these plists.

Placeholders
------------
- ``${DEVBRAIN_HOME}`` — absolute path to the devbrain checkout
  (typically `/Users/lhtdev/devbrain` on Mac Studio).
- ``${USER}`` — current user's login name; used to compose the log
  paths under `/Users/<user>/.devbrain/logs/`.
- ``${PROJECT_SLUG}`` — slug of the project that the LLM-cost passes
  (extract + edges + strengthen) should scope to.
- ``@CREDENTIAL_ENV_BLOCK@`` — XML marker replaced by either an
  ``ANTHROPIC_API_KEY`` or ``CLAUDE_CODE_OAUTH_TOKEN`` env entry in
  the two LLM-cost plists (extract + edges). The non-LLM plists
  (decay + strengthen + gc) don't carry this marker.

Idempotency
-----------
Safe to re-run with the same args — existing installed plists are
replaced atomically (write to temp file in the same directory, then
rename). With ``reload=True``, an already-loaded job is unloaded and
reloaded; an unloaded job is just loaded.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

_TEMPLATE_DIR = Path(__file__).resolve().parent / "launchd"

# Plists that don't carry credentials — emitted with mode 0644.
_NO_CRED_PLISTS = (
    "com.devbrain.cognify-decay.plist",
    "com.devbrain.cognify-strengthen.plist",
    "com.devbrain.cognify-gc.plist",
)

# Plists that DO carry credentials — emitted with mode 0600.
_CRED_PLISTS = (
    "com.devbrain.cognify-extract.plist",
    "com.devbrain.cognify-edges.plist",
    "com.devbrain.cognify-fanout.plist",  # Phase 8 cross-project fan-out
)

# All cognify plists, in installation order.
ALL_PLISTS = _NO_CRED_PLISTS + _CRED_PLISTS

# The marker the credential block replaces. Multi-line; the installer
# replaces the *entire* HTML comment block plus the marker line.
_CRED_MARKER = "<!-- @CREDENTIAL_ENV_BLOCK@"


@dataclass(frozen=True)
class CredentialChoice:
    """The Anthropic credential to bake into LLM-cost plists.

    ``env_name`` is the env-var key launchd will inject into the
    cognify process (``ANTHROPIC_API_KEY`` or
    ``CLAUDE_CODE_OAUTH_TOKEN``). ``value`` is the secret itself.
    """

    env_name: str
    value: str

    def __post_init__(self) -> None:
        if self.env_name not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            raise ValueError(
                f"unsupported credential env_name: {self.env_name!r} — "
                "must be ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN"
            )
        if not self.value:
            raise ValueError("credential value must be non-empty")


def resolve_credential_from_env() -> CredentialChoice | None:
    """Pick the right Anthropic credential from the current process env.

    Precedence matches ``cognify._anthropic_auth.resolve_anthropic_auth``:
    Console API key first, OAuth token second. Returns None if neither
    is present — caller must decide whether to error (LLM plists need
    one) or skip (non-LLM plists don't).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return CredentialChoice("ANTHROPIC_API_KEY", api_key)

    bearer = (
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    )
    if bearer:
        return CredentialChoice("CLAUDE_CODE_OAUTH_TOKEN", bearer)

    return None


def render_plist(
    template_text: str,
    *,
    devbrain_home: str,
    user: str,
    project_slug: str,
    credential: CredentialChoice | None,
) -> str:
    """Substitute the four placeholder kinds in a template.

    Pure function; no I/O. Tested directly in unit tests.

    Raises ValueError if the template carries `@CREDENTIAL_ENV_BLOCK@`
    but no `credential` was supplied — that combination would emit
    a plist that can never authenticate.
    """
    out = template_text.replace("${DEVBRAIN_HOME}", devbrain_home)
    out = out.replace("${USER}", user)
    out = out.replace("${PROJECT_SLUG}", project_slug)

    if _CRED_MARKER in out:
        if credential is None:
            raise ValueError(
                "template carries @CREDENTIAL_ENV_BLOCK@ but no credential "
                "was supplied — extract + edges plists require one of "
                "ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN"
            )
        cred_xml = (
            f"        <key>{credential.env_name}</key>\n"
            f"        <string>{credential.value}</string>"
        )
        # Replace the entire two-line comment block + marker with the
        # rendered key/string pair. The template uses:
        #   <!-- @CREDENTIAL_ENV_BLOCK@ — installer emits ANTHROPIC_API_KEY or  -->
        #   <!-- CLAUDE_CODE_OAUTH_TOKEN here based on env at install time.    -->
        # We match from the marker to the closing `-->` of the SECOND line.
        marker_pos = out.find(_CRED_MARKER)
        # Find the end of the second comment line: search for the next
        # `-->` after the marker, twice.
        first_close = out.index("-->", marker_pos) + len("-->")
        second_close = out.index("-->", first_close) + len("-->")
        # Preserve leading whitespace on the marker line for indentation.
        line_start = out.rfind("\n", 0, marker_pos) + 1
        prefix = out[line_start:marker_pos]  # e.g. "        "
        out = out[:line_start] + cred_xml.lstrip() + out[second_close:]
        # Re-indent the credential xml to match the original marker indent.
        out = out.replace(
            cred_xml.lstrip(),
            "\n".join(prefix + line.lstrip() for line in cred_xml.splitlines()),
            1,
        )

    return out


def install_one(
    template_path: Path,
    *,
    target_dir: Path,
    devbrain_home: str,
    user: str,
    project_slug: str,
    credential: CredentialChoice | None,
) -> Path:
    """Render one template and write it to ``target_dir``.

    chmod 0600 on plists that carry credentials, 0644 otherwise.
    Atomic write: temp file in target_dir, then rename.

    Returns the absolute path of the installed plist.
    """
    text = template_path.read_text()
    rendered = render_plist(
        text,
        devbrain_home=devbrain_home,
        user=user,
        project_slug=project_slug,
        credential=credential,
    )
    target = target_dir / template_path.name
    needs_secret_perms = template_path.name in _CRED_PLISTS

    target_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=template_path.name + ".", suffix=".tmp", dir=str(target_dir)
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(rendered)
        os.chmod(tmp_path, 0o600 if needs_secret_perms else 0o644)
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target


def install_cognify_launchd(
    *,
    project_slug: str,
    credential: CredentialChoice | None = None,
    devbrain_home: str | None = None,
    user: str | None = None,
    target_dir: Path | None = None,
    reload: bool = False,
    runner: callable = subprocess.run,  # injection point for tests
) -> list[Path]:
    """Render and install all five cognify launchd plists.

    Defaults:
      * ``credential`` ← ``resolve_credential_from_env()``
      * ``devbrain_home`` ← parent-parent of this file
      * ``user`` ← $USER
      * ``target_dir`` ← ``~/Library/LaunchAgents``

    With ``reload=True``, runs ``launchctl unload`` (ignoring "not
    loaded" errors) and then ``launchctl load`` on each plist.

    Returns the list of installed plist paths.
    """
    if credential is None:
        credential = resolve_credential_from_env()
        # OK if it's None — render_plist will raise only on plists
        # that need it.

    if devbrain_home is None:
        # /factory/cognify/setup_launchd.py → devbrain root is 2 levels up
        devbrain_home = str(Path(__file__).resolve().parent.parent.parent)

    if user is None:
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        if not user:
            raise RuntimeError("could not determine current user from env")

    if target_dir is None:
        target_dir = Path.home() / "Library" / "LaunchAgents"

    installed: list[Path] = []
    for name in ALL_PLISTS:
        template = _TEMPLATE_DIR / name
        if not template.exists():
            raise FileNotFoundError(
                f"missing cognify plist template: {template}"
            )
        out_path = install_one(
            template,
            target_dir=target_dir,
            devbrain_home=devbrain_home,
            user=user,
            project_slug=project_slug,
            credential=credential,
        )
        installed.append(out_path)

    if reload:
        for path in installed:
            # `launchctl unload` errors when the job isn't loaded —
            # swallow that case but propagate everything else.
            runner(
                ["launchctl", "unload", str(path)],
                check=False,
                capture_output=True,
            )
            runner(
                ["launchctl", "load", str(path)],
                check=True,
                capture_output=True,
            )

    return installed
