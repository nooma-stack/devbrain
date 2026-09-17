"""Render and install the DevBrain resilience service.

This module owns only service-manager integration and the generated runtime
configuration.  The watchdog itself is provided by ``python -m
ops.resilience`` and has the stable interface::

    python -m ops.resilience --config PATH run-once|watch|status

The installer intentionally keeps credentials out of service definitions and
the install manifest.  Heartbeat authentication is represented only by the
name of an environment variable that the runtime resolves.
"""

from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
import platform as platform_module
import pwd
import re
import socket
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape

SCHEMA_VERSION = 1
SERVICE_LABEL = "com.devbrain.resilience"
SYSTEMD_UNIT = "devbrain-resilience.service"
HEARTBEAT_SECRET_ENV_DEFAULT = "DEVBRAIN_HEARTBEAT_TOKEN"
DEFAULT_AGENT_BUS_URL = "http://localhost:18900/healthz"
SERVICE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DOCKER_CONTEXT_RE = re.compile(r"^[A-Za-z0-9][-A-Za-z0-9._+]*$")
_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_CONTAINER_RUNTIMES = {
    "colima": "colima",
    "docker-desktop": "docker_desktop",
    "docker_desktop": "docker_desktop",
    "docker-engine": "docker_engine",
    "docker_engine": "docker_engine",
    "orbstack": "orbstack",
}
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_REPO_ROOT = Path(__file__).resolve().parents[2]


class InstallError(ValueError):
    """Raised for an unsafe or internally inconsistent install request."""


Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class InstallRequest:
    """Fully resolved inputs for rendering one service installation."""

    profile: str
    platform: str
    repo_root: Path
    home: Path
    username: str
    uid: int
    python_executable: Path
    config_path: Path
    manifest_path: Path
    container_runtime: str
    docker_context: str
    with_heartbeat: bool = False
    heartbeat_url: str | None = None
    heartbeat_secret_env: str | None = None
    with_tunnel_check: bool = False
    tunnel_label: str | None = None
    with_agent_bus_check: bool = False
    agent_bus_url: str | None = None
    with_backup_check: bool = False
    backup_path: Path | None = None
    medic_mode: str | None = None
    medic_instance_id: str | None = None
    medic_primary_public_key: str | None = None
    medic_confirm_public_key: str | None = None
    medic_allowed_heal_checks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.profile not in ("workstation", "studio"):
            raise InstallError(
                f"unsupported profile {self.profile!r}; expected workstation or studio"
            )
        if self.platform not in ("macos", "linux"):
            raise InstallError(
                f"unsupported platform {self.platform!r}; expected macos or linux"
            )
        if (
            not self.username
            or self.username == "root"
            or not _USER_RE.fullmatch(self.username)
        ):
            raise InstallError(
                "the resilience service must run as the invoking non-root user"
            )
        if self.uid < 0:
            raise InstallError("uid must be a non-negative integer")
        if self.container_runtime not in tuple(_CONTAINER_RUNTIMES.values()):
            raise InstallError(
                "container_runtime must be colima, docker-desktop, "
                "docker-engine, or orbstack"
            )
        if not _DOCKER_CONTEXT_RE.fullmatch(self.docker_context):
            raise InstallError("docker_context contains unsupported characters")

        for name, path in {
            "repo_root": self.repo_root,
            "home": self.home,
            "python_executable": self.python_executable,
            "config_path": self.config_path,
            "manifest_path": self.manifest_path,
            **({"backup_path": self.backup_path} if self.backup_path else {}),
        }.items():
            if not path.is_absolute():
                raise InstallError(f"{name} must be an absolute path: {path}")
            if "\n" in str(path) or "\x00" in str(path):
                raise InstallError(f"{name} contains an unsafe character")
        expected_state_dir = self.home / ".devbrain" / "resilience"
        if self.manifest_path != expected_state_dir / "install-manifest.json":
            raise InstallError(
                "manifest_path must use the fixed per-user resilience location"
            )
        if self.config_path != expected_state_dir / "config.json":
            raise InstallError(
                "config_path must use the fixed per-user resilience location"
            )
        owned_paths = {
            self.config_path,
            self.manifest_path,
            self.service_path,
            self.state_dir / "state.json",
            self.state_dir / "heartbeat.json",
        }
        expected_paths = 5
        if self.medic_mode is not None:
            owned_paths.add(self.medic_config_path)
            expected_paths += 1
        if len(owned_paths) != expected_paths:
            raise InstallError("resilience managed paths must be distinct")

        if self.with_heartbeat:
            _validate_http_url(
                self.heartbeat_url,
                "heartbeat URL",
                require_https=True,
            )
            _validate_env_name(
                self.heartbeat_secret_env, "heartbeat secret environment variable"
            )
        elif self.heartbeat_url or self.heartbeat_secret_env:
            raise InstallError("heartbeat URL/secret-env require --with-heartbeat")

        if self.with_tunnel_check:
            if not self.tunnel_label or not _LABEL_RE.fullmatch(self.tunnel_label):
                raise InstallError(
                    "--with-tunnel-check requires a valid --tunnel-label"
                )
        elif self.tunnel_label:
            raise InstallError("--tunnel-label requires --with-tunnel-check")

        if self.with_agent_bus_check:
            _validate_http_url(self.agent_bus_url, "agent-bus URL")
        elif self.agent_bus_url:
            raise InstallError("agent-bus URL requires --with-agent-bus-check")

        if self.with_backup_check:
            if self.backup_path is None:
                raise InstallError(
                    "--with-backup-check requires an explicit --backup-path"
                )
        elif self.backup_path is not None:
            raise InstallError("--backup-path requires --with-backup-check")

        if self.medic_mode is None:
            if (
                self.medic_instance_id
                or self.medic_primary_public_key
                or self.medic_confirm_public_key
                or self.medic_allowed_heal_checks
            ):
                raise InstallError("medic settings require --with-medic")
        else:
            try:
                from .signing import encode_public_key, load_public_key
            except ImportError as exc:
                raise InstallError(
                    "signed medic requires the cryptography package; "
                    "install DevBrain requirements first"
                ) from exc
            if self.medic_mode not in {"diagnose", "heal"}:
                raise InstallError("medic mode must be diagnose or heal")
            if not self.medic_instance_id or not _LABEL_RE.fullmatch(
                self.medic_instance_id
            ):
                raise InstallError("medic instance ID contains unsupported characters")
            if not self.medic_primary_public_key:
                raise InstallError("--with-medic requires --medic-primary-public-key")
            try:
                primary = load_public_key(self.medic_primary_public_key)
            except ValueError as exc:
                raise InstallError("medic primary public key is invalid") from exc
            if self.medic_mode == "diagnose":
                if self.medic_confirm_public_key:
                    raise InstallError(
                        "a confirm key is valid only with --with-medic heal"
                    )
                if self.medic_allowed_heal_checks:
                    raise InstallError(
                        "diagnose-only medic cannot allow healing checks"
                    )
            else:
                if not self.medic_confirm_public_key:
                    raise InstallError(
                        "medic heal mode requires --medic-confirm-public-key"
                    )
                try:
                    confirm = load_public_key(self.medic_confirm_public_key)
                except ValueError as exc:
                    raise InstallError("medic confirm public key is invalid") from exc
                if encode_public_key(primary) == encode_public_key(confirm):
                    raise InstallError(
                        "medic primary and confirm keys must be distinct"
                    )
                if not self.medic_allowed_heal_checks:
                    raise InstallError(
                        "medic heal mode requires at least one --medic-allow-check"
                    )
            for check_id in self.medic_allowed_heal_checks:
                if not _LABEL_RE.fullmatch(check_id):
                    raise InstallError(
                        f"medic heal check contains unsupported characters: {check_id}"
                    )

    @property
    def state_dir(self) -> Path:
        return self.manifest_path.parent

    @property
    def log_dir(self) -> Path:
        return self.home / ".devbrain" / "logs"

    @property
    def medic_config_path(self) -> Path:
        return self.state_dir / "medic.json"

    @property
    def service_manager(self) -> str:
        return "launchd" if self.platform == "macos" else "systemd-user"

    @property
    def service_scope(self) -> str:
        if self.platform == "macos" and self.profile == "studio":
            return "system"
        return "user"

    @property
    def service_path(self) -> Path:
        if self.platform == "linux":
            return self.home / ".config" / "systemd" / "user" / SYSTEMD_UNIT
        if self.profile == "studio":
            return Path("/Library/LaunchDaemons") / f"{SERVICE_LABEL}.plist"
        return self.home / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"

    @property
    def interval_seconds(self) -> int:
        return 60 if self.profile == "studio" else 300


@dataclass(frozen=True)
class InstallPlan:
    """Rendered, side-effect-free installation artifacts."""

    request: InstallRequest
    config: dict[str, Any]
    config_text: str
    medic_config: dict[str, Any] | None
    medic_config_text: str | None
    service_text: str
    manifest: dict[str, Any]
    manifest_text: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": "install",
            "profile": self.request.profile,
            "platform": self.request.platform,
            "scope": self.request.service_scope,
            "config_path": str(self.request.config_path),
            "service_path": str(self.request.service_path),
            "manifest_path": str(self.request.manifest_path),
            "config": self.config,
            "medic": self.medic_config,
            "service": self.service_text,
            "manifest": self.manifest,
        }


def _validate_http_url(
    value: str | None,
    label: str,
    *,
    require_https: bool = False,
) -> None:
    if not value:
        raise InstallError(f"{label} is required")
    parsed = urlparse(value)
    allowed_schemes = {"https"} if require_https else {"http", "https"}
    if parsed.scheme not in allowed_schemes or not parsed.hostname:
        protocol = "HTTPS" if require_https else "http(s)"
        raise InstallError(f"{label} must be an absolute {protocol} URL")
    try:
        parsed.port
    except ValueError as exc:
        raise InstallError(f"{label} contains an invalid port") from exc
    if parsed.username or parsed.password:
        raise InstallError(f"{label} must not contain embedded credentials")


def _validate_env_name(value: str | None, label: str) -> None:
    if not value or not _ENV_NAME_RE.fullmatch(value):
        raise InstallError(f"{label} must be a valid environment-variable name")


def _read_dotenv_values(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE entries without evaluating shell syntax."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise InstallError(f"cannot read environment file {path}: {exc}") from exc
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not _ENV_NAME_RE.fullmatch(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def normalize_platform(value: str | None = None) -> str:
    raw = (value or platform_module.system()).strip().lower()
    if raw in {"darwin", "mac", "macos"}:
        return "macos"
    if raw == "linux":
        return "linux"
    raise InstallError(
        f"unsupported platform {raw!r}; resilience services support macOS and Linux"
    )


def resolve_invoking_user(
    env: Mapping[str, str] | None = None,
) -> tuple[str, int, Path]:
    """Resolve the non-root identity that will own and run the service."""

    source = os.environ if env is None else env
    if os.geteuid() == 0:
        raise InstallError(
            "run the installer as the non-root service user, without sudo; "
            "studio installs request sudo only for LaunchDaemon operations"
        )

    uid = os.getuid()
    try:
        entry = pwd.getpwuid(uid)
        username = entry.pw_name
        home = Path(entry.pw_dir).resolve()
    except KeyError:
        username = getpass.getuser()
        home_value = source.get("HOME")
        if not home_value:
            raise InstallError("could not determine the invoking user's home directory")
        home = Path(home_value).expanduser().resolve()

    if username == "root" or uid == 0:
        raise InstallError(
            "do not run the installer from a root login; invoke it as the "
            "non-root service user (sudo is requested only for LaunchDaemon steps)"
        )
    return username, uid, home


def create_request(
    *,
    profile: str = "workstation",
    platform_name: str | None = None,
    repo_root: Path | str | None = None,
    home: Path | str | None = None,
    username: str | None = None,
    uid: int | None = None,
    python_executable: Path | str | None = None,
    config_path: Path | str | None = None,
    manifest_path: Path | str | None = None,
    container_runtime: str | None = None,
    docker_context: str | None = None,
    with_heartbeat: bool = False,
    heartbeat_url: str | None = None,
    heartbeat_secret_env: str | None = None,
    with_tunnel_check: bool = False,
    tunnel_label: str | None = None,
    with_agent_bus_check: bool = False,
    agent_bus_url: str | None = None,
    with_backup_check: bool = False,
    backup_path: Path | str | None = None,
    medic_mode: str | None = None,
    medic_instance_id: str | None = None,
    medic_primary_public_key_path: Path | str | None = None,
    medic_confirm_public_key_path: Path | str | None = None,
    medic_allowed_heal_checks: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
) -> InstallRequest:
    """Resolve defaults and validate an install request.

    Explicit identity and path arguments are primarily useful for tests and
    automated provisioning.  Normal callers let the function derive them from
    the invoking user.
    """

    source = os.environ if env is None else env
    if username is None or uid is None or home is None:
        detected_user, detected_uid, detected_home = resolve_invoking_user(source)
        username = username or detected_user
        uid = detected_uid if uid is None else uid
        home = home or detected_home

    resolved_platform = normalize_platform(platform_name)
    resolved_home = Path(home).expanduser().resolve()
    resolved_repo = Path(repo_root or _REPO_ROOT).expanduser().resolve()
    dotenv = _read_dotenv_values(resolved_repo / ".env")

    def configured_value(name: str) -> str | None:
        return source.get(name) or dotenv.get(name)

    # Keep a virtual-environment executable path intact. Resolving this symlink
    # to the Homebrew/system interpreter would silently discard the venv when
    # launchd or systemd starts the monitor.
    resolved_python = Path(python_executable or sys.executable).expanduser().absolute()
    state_dir = resolved_home / ".devbrain" / "resilience"
    resolved_config = (
        Path(config_path or state_dir / "config.json").expanduser().resolve()
    )
    resolved_manifest = (
        Path(manifest_path or state_dir / "install-manifest.json")
        .expanduser()
        .resolve()
    )
    runtime_input = container_runtime or (
        "colima"
        if profile == "studio" and resolved_platform == "macos"
        else "docker-desktop"
        if resolved_platform == "macos"
        else "docker-engine"
    )
    resolved_runtime = _CONTAINER_RUNTIMES.get(runtime_input, runtime_input)
    # The existing full installer historically calls the Linux Docker Engine
    # path "docker-desktop". Normalize that alias for a safe detection-only
    # watchdog policy rather than rendering a macOS `open -a Docker` recovery.
    if resolved_platform == "linux" and resolved_runtime == "docker_desktop":
        resolved_runtime = "docker_engine"
    if resolved_platform == "macos" and resolved_runtime == "docker_engine":
        raise InstallError("docker-engine is supported only on Linux")
    default_contexts = {
        "colima": "colima",
        "docker_desktop": "desktop-linux",
        "docker_engine": "default",
        "orbstack": "orbstack",
    }
    resolved_context = docker_context or default_contexts.get(resolved_runtime)
    if resolved_context is None:
        raise InstallError("could not determine the Docker context")

    if with_heartbeat:
        heartbeat_url = heartbeat_url or configured_value("DEVBRAIN_HEARTBEAT_URL")
        heartbeat_secret_env = heartbeat_secret_env or HEARTBEAT_SECRET_ENV_DEFAULT
        if not dotenv.get(heartbeat_secret_env):
            raise InstallError(
                f"{heartbeat_secret_env} must be set in {resolved_repo / '.env'} "
                "so the background service can sign heartbeats"
            )
    if with_tunnel_check:
        tunnel_label = tunnel_label or configured_value("DEVBRAIN_TUNNEL_LABEL")
    if with_agent_bus_check:
        agent_bus_url = (
            agent_bus_url
            or configured_value("DEVBRAIN_AGENT_BUS_HEALTH_URL")
            or DEFAULT_AGENT_BUS_URL
        )
    resolved_backup_path: Path | None = None
    if with_backup_check:
        backup_path_value = backup_path or configured_value("DEVBRAIN_BACKUP_PATH")
        if not backup_path_value:
            raise InstallError(
                "--with-backup-check requires --backup-path or "
                "DEVBRAIN_BACKUP_PATH"
            )
        resolved_backup_path = Path(backup_path_value).expanduser().resolve()
    elif backup_path is not None:
        resolved_backup_path = Path(backup_path).expanduser().resolve()

    primary_public_key: str | None = None
    confirm_public_key: str | None = None
    if medic_mode is not None:
        medic_instance_id = (
            medic_instance_id
            or configured_value("DEVBRAIN_INSTANCE_ID")
            or socket.gethostname().split(".", 1)[0]
        )
        primary_path_value = medic_primary_public_key_path or configured_value(
            "DEVBRAIN_MEDIC_PRIMARY_PUBLIC_KEY_FILE"
        )
        if primary_path_value:
            try:
                primary_public_key = (
                    Path(primary_path_value)
                    .expanduser()
                    .resolve()
                    .read_text(encoding="utf-8")
                    .strip()
                )
            except OSError as exc:
                raise InstallError(
                    f"cannot read medic primary public key: {exc}"
                ) from exc
        confirm_path_value = medic_confirm_public_key_path or configured_value(
            "DEVBRAIN_MEDIC_CONFIRM_PUBLIC_KEY_FILE"
        )
        if confirm_path_value:
            try:
                confirm_public_key = (
                    Path(confirm_path_value)
                    .expanduser()
                    .resolve()
                    .read_text(encoding="utf-8")
                    .strip()
                )
            except OSError as exc:
                raise InstallError(
                    f"cannot read medic confirm public key: {exc}"
                ) from exc

    return InstallRequest(
        profile=profile,
        platform=resolved_platform,
        repo_root=resolved_repo,
        home=resolved_home,
        username=str(username),
        uid=int(uid),
        python_executable=resolved_python,
        config_path=resolved_config,
        manifest_path=resolved_manifest,
        container_runtime=resolved_runtime,
        docker_context=resolved_context,
        with_heartbeat=with_heartbeat,
        heartbeat_url=heartbeat_url,
        heartbeat_secret_env=heartbeat_secret_env,
        with_tunnel_check=with_tunnel_check,
        tunnel_label=tunnel_label,
        with_agent_bus_check=with_agent_bus_check,
        agent_bus_url=agent_bus_url,
        with_backup_check=with_backup_check,
        backup_path=resolved_backup_path,
        medic_mode=medic_mode,
        medic_instance_id=medic_instance_id,
        medic_primary_public_key=primary_public_key,
        medic_confirm_public_key=confirm_public_key,
        medic_allowed_heal_checks=tuple(medic_allowed_heal_checks),
    )


def _default_checks(request: InstallRequest) -> list[dict[str, Any]]:
    """Return the typed, secret-free checks for a deployment profile."""

    is_studio = request.profile == "studio"
    launch_domain = f"gui/{request.uid}"
    ingest_type = (
        "launchd_label" if request.platform == "macos" else "systemd_user_unit"
    )
    ingest_settings: dict[str, Any]
    tunnel_settings: dict[str, Any]
    if request.platform == "macos":
        ingest_settings = {
            "label": "com.devbrain.ingest",
            "domain": launch_domain,
            "require_running": True,
            "recovery": {
                "type": "launchd_kickstart",
                "label": "com.devbrain.ingest",
                "domain": launch_domain,
            },
        }
        tunnel_label = request.tunnel_label or "com.devbrain.tunnel"
        tunnel_settings = {
            "label": tunnel_label,
            "domain": launch_domain,
            "require_running": True,
        }
        if request.with_tunnel_check:
            tunnel_settings["recovery"] = {
                "type": "launchd_kickstart",
                "label": tunnel_label,
                "domain": launch_domain,
            }
    else:
        ingest_settings = {
            "unit": "devbrain-ingest.service",
        }
        tunnel_unit = request.tunnel_label or "devbrain-tunnel.service"
        tunnel_settings = {
            "unit": tunnel_unit,
        }
        if request.with_tunnel_check:
            tunnel_settings["recovery"] = {
                "type": "systemd_restart",
                "unit": tunnel_unit,
            }

    for settings in (ingest_settings, tunnel_settings):
        settings.update(
            {
                "failure_threshold": 2,
                "cooldown_seconds": 300,
                "max_attempts": 3,
            }
        )

    agent_bus = urlparse(request.agent_bus_url or DEFAULT_AGENT_BUS_URL)
    agent_bus_port = agent_bus.port or (443 if agent_bus.scheme == "https" else 80)

    runtime_settings: dict[str, Any] = {
        "context": request.docker_context,
        "failure_threshold": 2,
        "cooldown_seconds": 300,
        "max_attempts": 3,
    }
    if request.container_runtime != "docker_engine":
        runtime_settings["recovery"] = {
            "type": "runtime_start",
            "runtime": request.container_runtime,
        }

    checks = [
        {
            "id": "container_runtime",
            "type": "docker_runtime",
            "enabled": True,
            "required": True,
            "timeout_seconds": 10,
            "settings": runtime_settings,
        },
        {
            "id": "postgres",
            "type": "docker_container",
            "enabled": True,
            "required": True,
            "timeout_seconds": 5,
            "settings": {
                "container": "devbrain-db",
                "context": request.docker_context,
                "require_healthcheck": True,
                "recovery": {
                    "type": "docker_compose_up",
                    "context": request.docker_context,
                    "project_dir": str(request.repo_root),
                    "services": ["devbrain-db"],
                },
                "failure_threshold": 2,
                "cooldown_seconds": 300,
                "max_attempts": 3,
            },
        },
        {
            "id": "ollama",
            "type": "http",
            "enabled": True,
            "required": True,
            "timeout_seconds": 5,
            "settings": {
                "url": "http://localhost:11434/api/tags",
                **(
                    {
                        "recovery": {
                            "type": "homebrew_service_start",
                            "service": "ollama",
                        }
                    }
                    if request.platform == "macos"
                    else {}
                ),
                "failure_threshold": 2,
                "cooldown_seconds": 300,
                "max_attempts": 3,
            },
        },
        {
            "id": "ingest",
            "type": ingest_type,
            "enabled": request.platform == "macos",
            "required": False,
            "timeout_seconds": 5,
            "settings": ingest_settings,
        },
        {
            "id": "disk",
            "type": "disk",
            "enabled": True,
            "required": True,
            "timeout_seconds": 5,
            "settings": {
                "path": "/",
                "min_free_percent": 10,
                "min_free_bytes": (10 if is_studio else 5) * 1024**3,
                "failure_threshold": 1,
                "cooldown_seconds": 3600,
                "max_attempts": 0,
            },
        },
        {
            "id": "tunnel",
            "type": ingest_type,
            "enabled": request.with_tunnel_check,
            "required": False,
            "timeout_seconds": 5,
            "settings": tunnel_settings,
        },
        {
            "id": "agent_bus",
            "type": "tcp",
            "enabled": request.with_agent_bus_check,
            "required": False,
            "timeout_seconds": 5,
            "settings": {
                "host": agent_bus.hostname or "localhost",
                "port": agent_bus_port,
                "failure_threshold": 2,
                "cooldown_seconds": 300,
                "max_attempts": 0,
            },
        },
    ]
    if request.with_backup_check:
        checks.insert(
            5,
            {
                "id": "backup",
                "type": "file_freshness",
                "enabled": True,
                "required": False,
                "timeout_seconds": 5,
                "settings": {
                    "path": str(request.backup_path),
                    "pattern": "*",
                    "max_age_seconds": 172800,
                    "minimum_matches": 1,
                },
            },
        )
    return checks


def render_config(request: InstallRequest) -> dict[str, Any]:
    """Build the JSON object consumed by the resilience runtime."""

    return {
        "schema_version": SCHEMA_VERSION,
        "profile": request.profile,
        "state_path": str(request.state_dir / "state.json"),
        "heartbeat_path": str(request.state_dir / "heartbeat.json"),
        "medic_config_path": (
            str(request.medic_config_path) if request.medic_mode is not None else None
        ),
        "interval_seconds": request.interval_seconds,
        "failure_threshold": 2,
        "cooldown_seconds": 300,
        "max_attempts": 3,
        "checks": _default_checks(request),
        "heartbeat": (
            {
                "url": request.heartbeat_url,
                "secret_env": request.heartbeat_secret_env,
            }
            if request.with_heartbeat
            else None
        ),
    }


def render_medic_config(request: InstallRequest) -> dict[str, Any] | None:
    """Render the public-key-only medic policy for an explicit opt-in."""

    if request.medic_mode is None:
        return None
    try:
        from .signing import encode_public_key, load_public_key
    except ImportError as exc:
        raise InstallError(
            "signed medic requires the cryptography package; "
            "install DevBrain requirements first"
        ) from exc
    recoverable = {
        str(check["id"])
        for check in _default_checks(request)
        if check.get("enabled", True)
        and isinstance(check.get("settings", {}).get("recovery"), dict)
    }
    unknown = sorted(set(request.medic_allowed_heal_checks) - recoverable)
    if unknown:
        raise InstallError(
            "medic heal checks are not enabled/recoverable: " + ", ".join(unknown)
        )
    primary = load_public_key(str(request.medic_primary_public_key))
    keys = [
        {
            "key_id": "operator-primary",
            "role": "primary",
            "public_key": encode_public_key(primary),
        }
    ]
    if request.medic_mode == "heal":
        confirm = load_public_key(str(request.medic_confirm_public_key))
        keys.append(
            {
                "key_id": "operator-confirm",
                "role": "confirm",
                "public_key": encode_public_key(confirm),
            }
        )
    return {
        "schema_version": 1,
        "instance_id": request.medic_instance_id,
        "mode": request.medic_mode,
        "queue_dir": str(request.state_dir / "medic-queue"),
        "poll_interval_seconds": 10,
        "keys": keys,
        "allowed_heal_checks": list(request.medic_allowed_heal_checks),
    }


def _render_template(name: str, values: Mapping[str, str], *, xml: bool) -> str:
    template_path = _TEMPLATE_DIR / name
    if not template_path.exists():
        raise InstallError(f"missing service template: {template_path}")
    output = template_path.read_text()
    for key, raw_value in values.items():
        value = xml_escape(raw_value) if xml else _systemd_escape(raw_value)
        output = output.replace(f"@{key}@", value)

    unresolved = sorted(set(re.findall(r"@[A-Z][A-Z0-9_]*@", output)))
    if unresolved:
        raise InstallError(
            f"unresolved template placeholder(s) in {name}: {', '.join(unresolved)}"
        )
    return output


def _systemd_escape(value: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise InstallError("systemd template value contains an unsafe character")
    # Every substituted value occupies one systemd argument or assignment.
    # Double quotes handle whitespace; percent must be doubled to avoid unit
    # specifier expansion.
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def render_service(request: InstallRequest) -> str:
    values = {
        "LABEL": SERVICE_LABEL,
        "RUN_AS_USER": request.username,
        "PYTHON": str(request.python_executable),
        "CONFIG_PATH": str(request.config_path),
        "DEVBRAIN_HOME": str(request.repo_root),
        "HOME": str(request.home),
        "PATH": SERVICE_PATH,
        "STDOUT_PATH": str(request.log_dir / "resilience.log"),
        "STDERR_PATH": str(request.log_dir / "resilience.err.log"),
        "DEVBRAIN_ENV": f"DEVBRAIN_HOME={request.repo_root}",
        "HOME_ENV": f"HOME={request.home}",
        "PATH_ENV": f"PATH={SERVICE_PATH}",
        "STDOUT_TARGET": f"append:{request.log_dir / 'resilience.log'}",
        "STDERR_TARGET": f"append:{request.log_dir / 'resilience.err.log'}",
    }
    if request.platform == "linux":
        return _render_template(
            "linux-systemd-user.service.template", values, xml=False
        )
    template = (
        "macos-launchdaemon.plist.template"
        if request.profile == "studio"
        else "macos-launchagent.plist.template"
    )
    return _render_template(template, values, xml=True)


def render_manifest(request: InstallRequest) -> dict[str, Any]:
    service: dict[str, Any] = {
        "manager": request.service_manager,
        "scope": request.service_scope,
        "path": str(request.service_path),
    }
    if request.platform == "macos":
        service["label"] = SERVICE_LABEL
        service["uid"] = request.uid
    else:
        service["unit"] = SYSTEMD_UNIT

    managed_files = [
        {"kind": "config", "path": str(request.config_path)},
        {"kind": "service", "path": str(request.service_path)},
        {
            "kind": "runtime_state",
            "path": str(request.state_dir / "state.json"),
        },
        {
            "kind": "heartbeat",
            "path": str(request.state_dir / "heartbeat.json"),
        },
    ]
    if request.medic_mode is not None:
        managed_files.append(
            {"kind": "medic_config", "path": str(request.medic_config_path)}
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_by": "devbrain-resilience-installer",
        "profile": request.profile,
        "platform": request.platform,
        "container_runtime": request.container_runtime.replace("_", "-"),
        "docker_context": request.docker_context,
        "backup_path": (
            str(request.backup_path) if request.with_backup_check else None
        ),
        "run_as_user": request.username,
        "config_path": str(request.config_path),
        "medic_config_path": (
            str(request.medic_config_path) if request.medic_mode is not None else None
        ),
        "medic_queue_dir": (
            str(request.state_dir / "medic-queue")
            if request.medic_mode is not None
            else None
        ),
        "service": service,
        "managed_files": managed_files,
    }


def build_plan(request: InstallRequest) -> InstallPlan:
    config = render_config(request)
    medic_config = render_medic_config(request)
    manifest = render_manifest(request)
    return InstallPlan(
        request=request,
        config=config,
        config_text=json.dumps(config, indent=2, sort_keys=True) + "\n",
        medic_config=medic_config,
        medic_config_text=(
            json.dumps(medic_config, indent=2, sort_keys=True) + "\n"
            if medic_config is not None
            else None
        ),
        service_text=render_service(request),
        manifest=manifest,
        manifest_text=json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )


def _atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _default_runner(args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), **kwargs)


def _run(
    runner: Runner,
    args: Sequence[str],
    *,
    check: bool,
) -> subprocess.CompletedProcess:
    return runner(
        list(args),
        check=check,
        capture_output=True,
        text=True,
    )


def _write_service(
    request: InstallRequest,
    content: str,
    runner: Runner,
) -> None:
    if request.platform == "macos" and request.profile == "studio":
        request.state_dir.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{SERVICE_LABEL}.", suffix=".plist", dir=str(request.state_dir)
        )
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o644)
            if os.geteuid() == 0:
                _atomic_write(request.service_path, content, 0o644)
            else:
                _run(
                    runner,
                    [
                        "sudo",
                        "install",
                        "-o",
                        "root",
                        "-g",
                        "wheel",
                        "-m",
                        "0644",
                        temporary,
                        str(request.service_path),
                    ],
                    check=True,
                )
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        return
    _atomic_write(request.service_path, content, 0o644)


def _deactivate_service(service: Mapping[str, Any], runner: Runner) -> None:
    manager = service.get("manager")
    if manager == "launchd":
        scope = service.get("scope")
        label = service.get("label")
        path = service.get("path")
        uid = service.get("uid")
        if not isinstance(label, str) or not isinstance(path, str):
            raise InstallError("manifest launchd service is missing label/path")
        if scope == "system":
            _run(
                runner,
                ["sudo", "launchctl", "bootout", f"system/{label}"],
                check=False,
            )
            probe = _run(
                runner,
                ["sudo", "launchctl", "print", f"system/{label}"],
                check=False,
            )
        elif scope == "user" and isinstance(uid, int):
            _run(
                runner,
                ["launchctl", "bootout", f"gui/{uid}", path],
                check=False,
            )
            probe = _run(
                runner,
                ["launchctl", "print", f"gui/{uid}/{label}"],
                check=False,
            )
        else:
            raise InstallError("manifest launchd service has an invalid scope/uid")
        if probe.returncode == 0:
            raise InstallError(
                f"refusing to remove resilience files while {label} is still loaded"
            )
        if probe.returncode != 113:
            raise InstallError(
                "could not verify that the resilience launchd service stopped "
                f"(launchctl print exited {probe.returncode})"
            )
        return

    if manager == "systemd-user":
        unit = service.get("unit")
        if not isinstance(unit, str):
            raise InstallError("manifest systemd service is missing its unit")
        _run(
            runner,
            ["systemctl", "--user", "disable", "--now", unit],
            check=False,
        )
        probe = _run(
            runner,
            ["systemctl", "--user", "is-active", "--quiet", unit],
            check=False,
        )
        if probe.returncode == 0:
            raise InstallError(
                f"refusing to remove resilience files while {unit} is still active"
            )
        if probe.returncode not in {3, 4}:
            raise InstallError(
                "could not verify that the resilience systemd service stopped "
                f"(systemctl is-active exited {probe.returncode})"
            )
        return

    raise InstallError(f"manifest has unsupported service manager: {manager!r}")


def _activate_service(request: InstallRequest, runner: Runner) -> None:
    if request.platform == "linux":
        if request.profile == "studio":
            # A headless Studio must keep its user manager alive across logout
            # and boot. Do not disable linger on uninstall: other user units
            # may depend on it.
            _run(
                runner,
                ["sudo", "loginctl", "enable-linger", request.username],
                check=True,
            )
        _run(runner, ["systemctl", "--user", "daemon-reload"], check=True)
        _run(
            runner,
            ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT],
            check=True,
        )
        _run(
            runner,
            ["systemctl", "--user", "restart", SYSTEMD_UNIT],
            check=True,
        )
        return

    if request.profile == "studio":
        _run(
            runner,
            ["sudo", "launchctl", "bootout", f"system/{SERVICE_LABEL}"],
            check=False,
        )
        _run(
            runner,
            ["sudo", "launchctl", "bootstrap", "system", str(request.service_path)],
            check=True,
        )
        return

    domain = f"gui/{request.uid}"
    _run(
        runner,
        ["launchctl", "bootout", domain, str(request.service_path)],
        check=False,
    )
    _run(
        runner,
        ["launchctl", "bootstrap", domain, str(request.service_path)],
        check=True,
    )


def _load_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot read install manifest {path}: {exc}") from exc
    _validate_manifest(value, path)
    return value


def _validate_manifest(value: Any, manifest_path: Path) -> None:
    if not isinstance(value, dict):
        raise InstallError("install manifest must contain a JSON object")
    if value.get("generated_by") != "devbrain-resilience-installer":
        raise InstallError("refusing an install manifest not generated by DevBrain")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise InstallError(
            f"unsupported install manifest schema: {value.get('schema_version')!r}"
        )
    profile = value.get("profile")
    platform_name = value.get("platform")
    container_runtime = value.get("container_runtime")
    docker_context = value.get("docker_context")
    run_as_user = value.get("run_as_user")
    if profile not in {"workstation", "studio"}:
        raise InstallError("install manifest profile is invalid")
    if platform_name not in {"macos", "linux"}:
        raise InstallError("install manifest platform is invalid")
    if container_runtime not in {
        runtime.replace("_", "-") for runtime in _CONTAINER_RUNTIMES.values()
    }:
        raise InstallError("install manifest container runtime is invalid")
    if not isinstance(docker_context, str) or not _DOCKER_CONTEXT_RE.fullmatch(
        docker_context
    ):
        raise InstallError("install manifest Docker context is invalid")
    if (
        not isinstance(run_as_user, str)
        or run_as_user == "root"
        or not _USER_RE.fullmatch(run_as_user)
    ):
        raise InstallError("install manifest run-as user is invalid")
    if (
        manifest_path.name != "install-manifest.json"
        or manifest_path.parent.name != "resilience"
        or manifest_path.parent.parent.name != ".devbrain"
    ):
        raise InstallError(
            "install manifest must be the DevBrain per-user resilience manifest"
        )
    inferred_home = manifest_path.parents[2]
    service = value.get("service")
    managed = value.get("managed_files")
    if not isinstance(service, dict) or not isinstance(managed, list):
        raise InstallError("install manifest is missing service/managed_files")
    manager = service.get("manager")
    scope = service.get("scope")
    service_path = service.get("path")
    if manager == "launchd":
        if set(service) != {"manager", "scope", "path", "label", "uid"}:
            raise InstallError("install manifest launchd service fields are invalid")
        if service.get("label") != SERVICE_LABEL:
            raise InstallError("install manifest launchd label is invalid")
        uid = service.get("uid")
        if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
            raise InstallError("install manifest launchd uid is invalid")
        if scope == "system":
            expected_service_path = str(
                Path("/Library/LaunchDaemons") / f"{SERVICE_LABEL}.plist"
            )
        elif scope == "user":
            expected_service_path = str(
                inferred_home / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
            )
        else:
            raise InstallError("install manifest launchd scope is invalid")
    elif manager == "systemd-user":
        if set(service) != {"manager", "scope", "path", "unit"}:
            raise InstallError("install manifest systemd service fields are invalid")
        if scope != "user" or service.get("unit") != SYSTEMD_UNIT:
            raise InstallError("install manifest systemd service is invalid")
        expected_service_path = str(
            inferred_home / ".config" / "systemd" / "user" / SYSTEMD_UNIT
        )
    else:
        raise InstallError("install manifest service manager is invalid")
    if platform_name == "macos" and manager != "launchd":
        raise InstallError("install manifest platform/service manager mismatch")
    if platform_name == "linux" and manager != "systemd-user":
        raise InstallError("install manifest platform/service manager mismatch")
    expected_scope = (
        "system"
        if platform_name == "macos" and profile == "studio"
        else "user"
    )
    if scope != expected_scope:
        raise InstallError("install manifest profile/service scope mismatch")
    if service_path != expected_service_path:
        raise InstallError(
            "install manifest service artifact is outside its fixed location"
        )
    config_path = value.get("config_path")
    expected_config_path = str(manifest_path.parent / "config.json")
    if config_path != expected_config_path:
        raise InstallError(
            "install manifest config artifact is outside its fixed location"
        )
    expected_paths = {
        "config": config_path,
        "service": service_path,
        "runtime_state": str(manifest_path.parent / "state.json"),
        "heartbeat": str(manifest_path.parent / "heartbeat.json"),
    }
    medic_path = value.get("medic_config_path")
    medic_queue_dir = value.get("medic_queue_dir")
    if medic_path is not None:
        expected_medic_path = str(manifest_path.parent / "medic.json")
        if medic_path != expected_medic_path:
            raise InstallError(
                "install manifest medic artifact does not match its declared path"
            )
        expected_paths["medic_config"] = medic_path
        expected_queue_dir = str(manifest_path.parent / "medic-queue")
        if medic_queue_dir != expected_queue_dir:
            raise InstallError(
                "install manifest medic queue is outside its fixed location"
            )
    elif medic_queue_dir is not None:
        raise InstallError(
            "install manifest medic queue requires a medic configuration"
        )
    seen_kinds: set[str] = set()
    seen_paths: set[str] = set()
    for item in managed:
        if (
            not isinstance(item, dict)
            or item.get("kind") not in set(expected_paths)
            or not isinstance(item.get("path"), str)
            or not Path(item["path"]).is_absolute()
        ):
            raise InstallError("install manifest contains an invalid managed file")
        kind = str(item["kind"])
        item_path = str(item["path"])
        if kind in seen_kinds or item_path in seen_paths:
            raise InstallError("install manifest contains duplicate managed files")
        seen_kinds.add(kind)
        seen_paths.add(item_path)
        if item_path != expected_paths[kind]:
            raise InstallError(
                f"install manifest {kind} artifact does not match its declared path"
            )
    if seen_kinds != set(expected_paths):
        raise InstallError("install manifest does not list the exact managed artifacts")


def _remove_managed_path(
    item: Mapping[str, Any],
    service: Mapping[str, Any],
    runner: Runner,
) -> None:
    path = Path(str(item["path"]))
    is_privileged_service = (
        item.get("kind") == "service"
        and service.get("manager") == "launchd"
        and service.get("scope") == "system"
    )
    if is_privileged_service and os.geteuid() != 0:
        _run(runner, ["sudo", "rm", "-f", str(path)], check=True)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _medic_has_pending(queue_dir: Path) -> bool:
    for name in ("inbox", "processing"):
        source = queue_dir / name
        if source.is_symlink():
            return True
        if source.is_dir():
            try:
                next(source.iterdir())
            except StopIteration:
                continue
            return True
        if source.exists():
            return True
    return False


@contextmanager
def _medic_consumer_lock(queue_dir: Path) -> Iterator[None]:
    """Exclude task consumers while changing or removing medic policy."""

    if queue_dir.is_symlink():
        raise InstallError("medic queue directory must not be a symlink")
    queue_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(queue_dir, 0o700)
    lock_path = queue_dir / "consumer.lock"
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise InstallError(f"cannot open medic consumer lock: {exc}") from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise InstallError("medic consumer lock must be a regular file")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstallError(
                "medic queue is active; retry after the current task finishes"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _quarantine_pending_medic(queue_dir: Path) -> list[str]:
    """Atomically retire pending inputs while retaining audit history."""

    if not _medic_has_pending(queue_dir):
        return []
    quarantine_root = queue_dir / "quarantine"
    quarantine_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(quarantine_root, 0o700)
    session_dir = Path(
        tempfile.mkdtemp(prefix="policy-change-", dir=str(quarantine_root))
    )
    quarantined: list[str] = []
    for name in ("inbox", "processing"):
        source = queue_dir / name
        if not (source.exists() or source.is_symlink()):
            continue
        if source.is_dir() and not source.is_symlink():
            try:
                next(source.iterdir())
            except StopIteration:
                continue
        destination = session_dir / name
        os.replace(source, destination)
        quarantined.append(str(destination))
    return quarantined


def _load_previous_medic_policy(
    manifest: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not manifest or manifest.get("medic_config_path") is None:
        return None
    path = Path(str(manifest["medic_config_path"]))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _load_previous_runtime_config(
    manifest: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not manifest:
        return None
    path = Path(str(manifest["config_path"]))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _apply_install(
    request: InstallRequest,
    plan: InstallPlan,
    previous: Mapping[str, Any] | None,
    runner: Runner,
) -> InstallPlan:
    previous_medic_policy = _load_previous_medic_policy(previous)
    previous_runtime_config = _load_previous_runtime_config(previous)
    queue_dir = request.state_dir / "medic-queue"
    policy_unchanged = (
        previous_medic_policy is not None
        and plan.medic_config is not None
        and previous_medic_policy == plan.medic_config
        and previous_runtime_config == plan.config
    )
    previous_service_deactivated = False
    if previous and previous.get("medic_config_path") is not None:
        # Stop the old signed-task consumer before changing any key, allowlist,
        # or typed recovery binding. This closes the reconfiguration window in
        # which a newly arrived task could otherwise execute under old policy.
        _deactivate_service(previous["service"], runner)
        previous_service_deactivated = True
    if _medic_has_pending(queue_dir) and not policy_unchanged:
        _quarantine_pending_medic(queue_dir)
    request.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    request.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(request.state_dir, 0o700)
    os.chmod(request.log_dir, 0o700)
    dotenv_path = request.repo_root / ".env"
    if dotenv_path.exists():
        os.chmod(dotenv_path, 0o600)
    _atomic_write(request.config_path, plan.config_text, 0o600)
    if plan.medic_config_text is not None:
        _atomic_write(request.medic_config_path, plan.medic_config_text, 0o600)
    _write_service(request, plan.service_text, runner)

    # Persist the exact artifact list before activation.  If activation fails,
    # a subsequent --uninstall still has complete cleanup information.
    _atomic_write(request.manifest_path, plan.manifest_text, 0o600)

    if previous:
        old_service = previous["service"]
        same_service = (
            old_service.get("manager") == plan.manifest["service"].get("manager")
            and old_service.get("scope") == plan.manifest["service"].get("scope")
            and old_service.get("path") == plan.manifest["service"].get("path")
        )
        if not same_service and not previous_service_deactivated:
            _deactivate_service(old_service, runner)
        new_paths = {item["path"] for item in plan.manifest["managed_files"]}
        for item in previous["managed_files"]:
            if item["path"] not in new_paths:
                _remove_managed_path(item, old_service, runner)

    _activate_service(request, runner)
    return plan


def install(
    request: InstallRequest,
    *,
    dry_run: bool = False,
    runner: Runner = _default_runner,
) -> InstallPlan:
    """Install or idempotently upgrade the configured resilience service."""

    plan = build_plan(request)
    if dry_run:
        return plan

    previous = _load_manifest(request.manifest_path)
    queue_dir = request.state_dir / "medic-queue"
    medic_lifecycle = (
        previous is not None
        and previous.get("medic_config_path") is not None
    ) or plan.medic_config is not None or _medic_has_pending(queue_dir)
    lock = (
        _medic_consumer_lock(queue_dir)
        if medic_lifecycle
        else nullcontext()
    )
    with lock:
        return _apply_install(request, plan, previous, runner)


def _activate_manifest_service(
    manifest: Mapping[str, Any],
    runner: Runner,
) -> None:
    """Activate an already rendered service without rewriting its policy."""

    service = manifest["service"]
    manager = service["manager"]
    if manager == "launchd":
        path = str(service["path"])
        if service["scope"] == "system":
            _run(
                runner,
                ["sudo", "launchctl", "bootstrap", "system", path],
                check=True,
            )
        else:
            _run(
                runner,
                [
                    "launchctl",
                    "bootstrap",
                    f"gui/{service['uid']}",
                    path,
                ],
                check=True,
            )
        return

    if manager == "systemd-user":
        if manifest["profile"] == "studio":
            _run(
                runner,
                [
                    "sudo",
                    "loginctl",
                    "enable-linger",
                    str(manifest["run_as_user"]),
                ],
                check=True,
            )
        unit = str(service["unit"])
        _run(runner, ["systemctl", "--user", "daemon-reload"], check=True)
        _run(
            runner,
            ["systemctl", "--user", "enable", "--now", unit],
            check=True,
        )
        _run(
            runner,
            ["systemctl", "--user", "restart", unit],
            check=True,
        )
        return

    raise InstallError(f"manifest has unsupported service manager: {manager!r}")


def restart(
    manifest_path: Path | str,
    *,
    dry_run: bool = False,
    runner: Runner = _default_runner,
) -> dict[str, Any]:
    """Restart an installed service while preserving its rendered policy."""

    path = Path(manifest_path).expanduser().resolve()
    manifest = _load_manifest(path)
    if manifest is None:
        return {
            "operation": "restart",
            "manifest_path": str(path),
            "installed": False,
        }
    result = {
        "operation": "restart",
        "manifest_path": str(path),
        "installed": True,
        "service": manifest["service"],
    }
    if dry_run:
        return result

    queue_dir = path.parent / "medic-queue"
    lock = (
        _medic_consumer_lock(queue_dir)
        if manifest.get("medic_config_path") is not None
        else nullcontext()
    )
    with lock:
        _deactivate_service(manifest["service"], runner)
        _activate_manifest_service(manifest, runner)
    return result


def uninstall(
    manifest_path: Path | str,
    *,
    dry_run: bool = False,
    runner: Runner = _default_runner,
) -> dict[str, Any]:
    """Remove only artifacts recorded in the exact install manifest."""

    path = Path(manifest_path).expanduser().resolve()
    manifest = _load_manifest(path)
    if manifest is None:
        return {
            "operation": "uninstall",
            "manifest_path": str(path),
            "installed": False,
            "managed_files": [],
        }

    result = {
        "operation": "uninstall",
        "manifest_path": str(path),
        "installed": True,
        "service": manifest["service"],
        "managed_files": list(manifest["managed_files"]),
    }
    if dry_run:
        return result

    service = manifest["service"]
    queue_dir = path.parent / "medic-queue"
    lock = (
        _medic_consumer_lock(queue_dir)
        if manifest.get("medic_config_path") is not None
        else nullcontext()
    )
    with lock:
        _deactivate_service(service, runner)
        result["quarantined_pending"] = _quarantine_pending_medic(queue_dir)
        for item in manifest["managed_files"]:
            _remove_managed_path(item, service, runner)

        try:
            path.unlink()
        except FileNotFoundError:
            pass

        if service.get("manager") == "systemd-user":
            _run(runner, ["systemctl", "--user", "daemon-reload"], check=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install-resilience",
        description="Install the DevBrain resilience service",
        allow_abbrev=False,
    )
    parser.add_argument("--profile", choices=("workstation", "studio"))
    parser.add_argument(
        "--container-runtime",
        choices=("colima", "docker-desktop", "docker-engine", "orbstack"),
    )
    parser.add_argument("--docker-context")
    parser.add_argument("--with-heartbeat", action="store_true")
    parser.add_argument("--heartbeat-url")
    parser.add_argument("--heartbeat-secret-env")
    parser.add_argument("--with-tunnel-check", action="store_true")
    parser.add_argument("--tunnel-label")
    parser.add_argument("--with-agent-bus-check", action="store_true")
    parser.add_argument("--agent-bus-url")
    parser.add_argument("--with-backup-check", action="store_true")
    parser.add_argument("--backup-path", type=Path)
    parser.add_argument("--with-medic", choices=("diagnose", "heal"))
    parser.add_argument("--medic-instance-id")
    parser.add_argument("--medic-primary-public-key", type=Path)
    parser.add_argument("--medic-confirm-public-key", type=Path)
    parser.add_argument("--medic-allow-check", action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--uninstall", action="store_true")
    operation.add_argument("--restart", action="store_true")
    parser.add_argument("--yes", action="store_true")
    return parser


def _reject_incompatible_args(args: argparse.Namespace) -> None:
    if args.uninstall or args.restart:
        operation = "--uninstall" if args.uninstall else "--restart"
        incompatible = []
        for name in (
            "profile",
            "container_runtime",
            "docker_context",
            "with_heartbeat",
            "heartbeat_url",
            "heartbeat_secret_env",
            "with_tunnel_check",
            "tunnel_label",
            "with_agent_bus_check",
            "agent_bus_url",
            "with_backup_check",
            "backup_path",
            "with_medic",
            "medic_instance_id",
            "medic_primary_public_key",
            "medic_confirm_public_key",
            "medic_allow_check",
        ):
            if getattr(args, name):
                incompatible.append("--" + name.replace("_", "-"))
        if incompatible:
            raise InstallError(
                f"{operation} is incompatible with install options: "
                + ", ".join(incompatible)
            )
        if args.yes and args.restart:
            raise InstallError("--yes is only valid with --uninstall")
        return

    if not args.with_heartbeat and (args.heartbeat_url or args.heartbeat_secret_env):
        raise InstallError(
            "--heartbeat-url/--heartbeat-secret-env require --with-heartbeat"
        )
    if not args.with_tunnel_check and args.tunnel_label:
        raise InstallError("--tunnel-label requires --with-tunnel-check")
    if not args.with_agent_bus_check and args.agent_bus_url:
        raise InstallError("--agent-bus-url requires --with-agent-bus-check")
    if not args.with_backup_check and args.backup_path:
        raise InstallError("--backup-path requires --with-backup-check")
    if not args.with_medic and (
        args.medic_instance_id
        or args.medic_primary_public_key
        or args.medic_confirm_public_key
        or args.medic_allow_check
    ):
        raise InstallError("medic options require --with-medic diagnose|heal")
    if args.yes:
        raise InstallError("--yes is only valid with --uninstall")


def _confirm_uninstall(manifest_path: Path) -> bool:
    if not sys.stdin.isatty():
        raise InstallError(
            "--uninstall requires --yes when no interactive terminal is available"
        )
    answer = input(
        f"Uninstall the exact resilience artifacts in {manifest_path}? [y/N]: "
    ).strip()
    return answer.lower() in {"y", "yes"}


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _reject_incompatible_args(args)
        username, uid, home = resolve_invoking_user(env)
        manifest_path = home / ".devbrain" / "resilience" / "install-manifest.json"

        if args.restart:
            result = restart(manifest_path, dry_run=args.dry_run)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0

        if args.uninstall:
            if (
                not args.dry_run
                and not args.yes
                and not _confirm_uninstall(manifest_path)
            ):
                print("Uninstall cancelled.")
                return 0
            result = uninstall(manifest_path, dry_run=args.dry_run)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0

        request = create_request(
            profile=args.profile or "workstation",
            username=username,
            uid=uid,
            home=home,
            container_runtime=args.container_runtime,
            docker_context=args.docker_context,
            with_heartbeat=args.with_heartbeat,
            heartbeat_url=args.heartbeat_url,
            heartbeat_secret_env=args.heartbeat_secret_env,
            with_tunnel_check=args.with_tunnel_check,
            tunnel_label=args.tunnel_label,
            with_agent_bus_check=args.with_agent_bus_check,
            agent_bus_url=args.agent_bus_url,
            with_backup_check=args.with_backup_check,
            backup_path=args.backup_path,
            medic_mode=args.with_medic,
            medic_instance_id=args.medic_instance_id,
            medic_primary_public_key_path=args.medic_primary_public_key,
            medic_confirm_public_key_path=args.medic_confirm_public_key,
            medic_allowed_heal_checks=args.medic_allow_check,
            env=env,
        )
        plan = install(request, dry_run=args.dry_run)
        output = (
            plan.as_dict()
            if args.dry_run
            else {
                "operation": "install",
                "profile": request.profile,
                "platform": request.platform,
                "scope": request.service_scope,
                "config_path": str(request.config_path),
                "service_path": str(request.service_path),
                "manifest_path": str(request.manifest_path),
            }
        )
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    except InstallError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
