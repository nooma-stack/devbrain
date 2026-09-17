"""Local health checks and deterministic recovery for a DevBrain host.

The module deliberately exposes no generic command or shell action. Every
subprocess invocation is selected by a typed check or typed recovery action,
uses an argv list, and runs without a shell.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

logger = logging.getLogger("devbrain.resilience")

CHECK_TYPES = frozenset(
    {
        "docker_runtime",
        "docker_container",
        "http",
        "tcp",
        "launchd_label",
        "systemd_user_unit",
        "disk",
        "file_freshness",
    }
)
RECOVERY_TYPES = frozenset(
    {
        "runtime_start",
        "docker_compose_up",
        "launchd_kickstart",
        "systemd_restart",
        "homebrew_service_start",
    }
)

_EXECUTABLE_NAMES = frozenset(
    {"brew", "colima", "docker", "launchctl", "open", "systemctl"}
)
_EXECUTABLE_CANDIDATES: dict[str, tuple[str, ...]] = {
    "brew": ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"),
    "colima": ("/opt/homebrew/bin/colima", "/usr/local/bin/colima"),
    "docker": ("/usr/local/bin/docker", "/opt/homebrew/bin/docker", "/usr/bin/docker"),
    "launchctl": ("/bin/launchctl",),
    "open": ("/usr/bin/open",),
    "systemctl": ("/usr/bin/systemctl", "/bin/systemctl"),
}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@+-]{0,127}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_LAUNCHD_DOMAIN = re.compile(r"^(?:system|(?:gui|user)/[0-9]+)$")
_FORBIDDEN_CONFIG_KEYS = frozenset({"argv", "cmd", "command", "shell"})
_STATE_VERSION = 1
_HEARTBEAT_VERSION = 1


class ConfigError(ValueError):
    """Raised when a resilience JSON config is invalid or unsafe."""


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    check_type: str
    ok: bool
    detail: str
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.check_type,
            "ok": self.ok,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class ActionResult:
    check_id: str
    action_type: str
    ok: bool
    detail: str
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "type": self.action_type,
            "ok": self.ok,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class WebhookResult:
    configured: bool
    delivered: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "delivered": self.delivered,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CycleResult:
    heartbeat: dict[str, Any]
    webhook: WebhookResult


@dataclass(frozen=True)
class MonitorConfig:
    schema_version: int
    profile: str
    source_path: Path
    state_path: Path
    heartbeat_path: Path
    medic_config_path: Path | None
    interval_seconds: float
    failure_threshold: int
    cooldown_seconds: float
    max_attempts: int
    host: str
    checks: tuple[dict[str, Any], ...]
    executables: Mapping[str, Path]
    heartbeat: Mapping[str, Any] | None

    def executable(self, name: str) -> str:
        if name not in _EXECUTABLE_NAMES:
            raise RuntimeError(f"unsupported executable: {name}")
        configured = self.executables.get(name)
        if configured is not None:
            return str(configured)
        found = shutil.which(name)
        if found:
            return str(Path(found).resolve())
        for candidate in _EXECUTABLE_CANDIDATES[name]:
            if Path(candidate).is_file():
                return candidate
        raise RuntimeError(
            f"{name} executable not found; set executables.{name} to an absolute path"
        )


ProcessRunner = Callable[[Sequence[str], float, Path | None], ProcessResult]
CheckExecutor = Callable[[MonitorConfig, Mapping[str, Any]], CheckResult]
ActionExecutor = Callable[
    [MonitorConfig, Mapping[str, Any], Mapping[str, Any]], ActionResult
]
WebhookSender = Callable[[MonitorConfig, Mapping[str, Any]], WebhookResult]


def _timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _trim(value: str, limit: int = 500) -> str:
    compact = " ".join(value.strip().split())
    return compact[:limit]


def _run_process(
    argv: Sequence[str], timeout_seconds: float, cwd: Path | None = None
) -> ProcessResult:
    completed = subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
        shell=False,
    )
    return ProcessResult(completed.returncode, completed.stdout, completed.stderr)


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{field} must be a JSON object")
    return dict(value)


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a non-empty string")
    return value.strip()


def _number(
    value: Any,
    field: str,
    *,
    minimum: float,
    allow_integer_only: bool = False,
) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{field} must be a number")
    if allow_integer_only and not isinstance(value, int):
        raise ConfigError(f"{field} must be an integer")
    if value < minimum:
        raise ConfigError(f"{field} must be >= {minimum}")
    return value


def _absolute_path(value: Any, field: str) -> Path:
    raw = _require_string(value, field)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"{field} must be an absolute path")
    return path.resolve(strict=False)


def _reject_forbidden_keys(value: Any, field: str = "config") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _FORBIDDEN_CONFIG_KEYS:
                raise ConfigError(
                    f"{field}.{key} is forbidden; only typed recovery actions are supported"
                )
            _reject_forbidden_keys(child, f"{field}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden_keys(child, f"{field}[{index}]")


def _check_unknown_keys(
    value: Mapping[str, Any], allowed: set[str], field: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(f"{field} has unknown keys: {', '.join(unknown)}")


def _safe_name(value: Any, field: str) -> str:
    name = _require_string(value, field)
    if not _SAFE_NAME.fullmatch(name):
        raise ConfigError(f"{field} contains unsupported characters")
    return name


def _launchd_domain(value: Any, field: str) -> str:
    domain = _require_string(value, field)
    if not _LAUNCHD_DOMAIN.fullmatch(domain):
        raise ConfigError(f"{field} must be system, gui/<uid>, or user/<uid>")
    return domain


def _timeout(value: Any, field: str, default: float = 10.0) -> float:
    return float(_number(default if value is None else value, field, minimum=0.1))


def _normalize_action(raw: Any, field: str, check: Mapping[str, Any]) -> dict[str, Any]:
    action = _require_mapping(raw, field)
    action_type = _require_string(action.get("type"), f"{field}.type")
    if action_type not in RECOVERY_TYPES:
        raise ConfigError(
            f"{field}.type must be one of: {', '.join(sorted(RECOVERY_TYPES))}"
        )
    allowed = {"type", "timeout_seconds"}
    normalized: dict[str, Any] = {
        "type": action_type,
        "timeout_seconds": _timeout(
            action.get("timeout_seconds"), f"{field}.timeout_seconds", 60.0
        ),
    }
    if action_type == "runtime_start":
        allowed.add("runtime")
        runtime = _require_string(action.get("runtime"), f"{field}.runtime")
        if runtime not in {"colima", "docker_desktop", "orbstack"}:
            raise ConfigError(
                f"{field}.runtime must be colima, docker_desktop, or orbstack"
            )
        normalized["runtime"] = runtime
    elif action_type == "docker_compose_up":
        allowed.update({"context", "project_dir", "services"})
        if action.get("context") is not None or check.get("context") is not None:
            normalized["context"] = _safe_name(
                action.get("context", check.get("context")),
                f"{field}.context",
            )
        normalized["project_dir"] = str(
            _absolute_path(action.get("project_dir"), f"{field}.project_dir")
        )
        services = action.get("services", [])
        if not isinstance(services, list):
            raise ConfigError(f"{field}.services must be a JSON array")
        normalized["services"] = [
            _safe_name(service, f"{field}.services[{index}]")
            for index, service in enumerate(services)
        ]
    elif action_type == "launchd_kickstart":
        allowed.update({"label", "domain"})
        normalized["label"] = _safe_name(
            action.get("label", check.get("label")), f"{field}.label"
        )
        normalized["domain"] = _launchd_domain(
            action.get("domain", check.get("domain", f"gui/{os.getuid()}")),
            f"{field}.domain",
        )
    elif action_type == "systemd_restart":
        allowed.add("unit")
        normalized["unit"] = _safe_name(
            action.get("unit", check.get("unit")), f"{field}.unit"
        )
    elif action_type == "homebrew_service_start":
        allowed.add("service")
        normalized["service"] = _safe_name(action.get("service"), f"{field}.service")
    _check_unknown_keys(action, allowed, field)
    return normalized


def _normalize_check(raw: Any, index: int) -> dict[str, Any]:
    field = f"checks[{index}]"
    check = _require_mapping(raw, field)
    check_id = _require_string(check.get("id"), f"{field}.id")
    if not _SAFE_ID.fullmatch(check_id):
        raise ConfigError(f"{field}.id contains unsupported characters")
    check_type = _require_string(check.get("type"), f"{field}.type")
    if check_type not in CHECK_TYPES:
        raise ConfigError(
            f"{field}.type must be one of: {', '.join(sorted(CHECK_TYPES))}"
        )
    allowed = {"id", "type", "enabled", "required", "timeout_seconds", "settings"}
    enabled = check.get("enabled", True)
    required = check.get("required", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{field}.enabled must be boolean")
    if not isinstance(required, bool):
        raise ConfigError(f"{field}.required must be boolean")
    settings = _require_mapping(check.get("settings", {}), f"{field}.settings")
    settings_field = f"{field}.settings"
    settings_allowed = {
        "cooldown_seconds",
        "failure_threshold",
        "max_attempts",
        "recovery",
    }
    normalized: dict[str, Any] = {
        "id": check_id,
        "type": check_type,
        "enabled": enabled,
        "required": required,
        "timeout_seconds": _timeout(
            check.get("timeout_seconds"), f"{field}.timeout_seconds"
        ),
        "settings": {},
    }
    normalized_settings = normalized["settings"]
    if "failure_threshold" in settings:
        normalized_settings["failure_threshold"] = int(
            _number(
                settings["failure_threshold"],
                f"{settings_field}.failure_threshold",
                minimum=1,
                allow_integer_only=True,
            )
        )
    if "cooldown_seconds" in settings:
        normalized_settings["cooldown_seconds"] = float(
            _number(
                settings["cooldown_seconds"],
                f"{settings_field}.cooldown_seconds",
                minimum=0,
            )
        )
    if "max_attempts" in settings:
        normalized_settings["max_attempts"] = int(
            _number(
                settings["max_attempts"],
                f"{settings_field}.max_attempts",
                minimum=0,
                allow_integer_only=True,
            )
        )

    if check_type == "docker_runtime":
        settings_allowed.add("context")
        normalized_settings["context"] = _safe_name(
            settings.get("context", "default"),
            f"{settings_field}.context",
        )
    elif check_type == "docker_container":
        settings_allowed.update({"container", "context", "require_healthcheck"})
        normalized_settings["context"] = _safe_name(
            settings.get("context", "default"),
            f"{settings_field}.context",
        )
        normalized_settings["container"] = _safe_name(
            settings.get("container"), f"{settings_field}.container"
        )
        require_healthcheck = settings.get("require_healthcheck", False)
        if not isinstance(require_healthcheck, bool):
            raise ConfigError(f"{settings_field}.require_healthcheck must be boolean")
        normalized_settings["require_healthcheck"] = require_healthcheck
    elif check_type == "http":
        settings_allowed.update({"url", "method", "status_min", "status_max"})
        url = _require_string(settings.get("url"), f"{settings_field}.url")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ConfigError(f"{settings_field}.url must be an http or https URL")
        method = _require_string(
            settings.get("method", "GET"), f"{settings_field}.method"
        ).upper()
        if method not in {"GET", "HEAD"}:
            raise ConfigError(f"{settings_field}.method must be GET or HEAD")
        status_min = int(
            _number(
                settings.get("status_min", 200),
                f"{settings_field}.status_min",
                minimum=100,
                allow_integer_only=True,
            )
        )
        status_max = int(
            _number(
                settings.get("status_max", 399),
                f"{settings_field}.status_max",
                minimum=status_min,
                allow_integer_only=True,
            )
        )
        if status_max > 599:
            raise ConfigError(f"{settings_field}.status_max must be <= 599")
        normalized_settings.update(
            {
                "url": url,
                "method": method,
                "status_min": status_min,
                "status_max": status_max,
            }
        )
    elif check_type == "tcp":
        settings_allowed.update({"host", "port"})
        normalized_settings["host"] = _require_string(
            settings.get("host"), f"{settings_field}.host"
        )
        port = int(
            _number(
                settings.get("port"),
                f"{settings_field}.port",
                minimum=1,
                allow_integer_only=True,
            )
        )
        if port > 65535:
            raise ConfigError(f"{settings_field}.port must be <= 65535")
        normalized_settings["port"] = port
    elif check_type == "launchd_label":
        settings_allowed.update({"label", "domain", "require_running"})
        normalized_settings["label"] = _safe_name(
            settings.get("label"), f"{settings_field}.label"
        )
        normalized_settings["domain"] = _launchd_domain(
            settings.get("domain", f"gui/{os.getuid()}"),
            f"{settings_field}.domain",
        )
        require_running = settings.get("require_running", True)
        if not isinstance(require_running, bool):
            raise ConfigError(f"{settings_field}.require_running must be boolean")
        normalized_settings["require_running"] = require_running
    elif check_type == "systemd_user_unit":
        settings_allowed.add("unit")
        normalized_settings["unit"] = _safe_name(
            settings.get("unit"), f"{settings_field}.unit"
        )
    elif check_type == "disk":
        settings_allowed.update({"path", "min_free_percent", "min_free_bytes"})
        normalized_settings["path"] = str(
            _absolute_path(settings.get("path"), f"{settings_field}.path")
        )
        min_percent = float(
            _number(
                settings.get("min_free_percent", 10),
                f"{settings_field}.min_free_percent",
                minimum=0,
            )
        )
        if min_percent > 100:
            raise ConfigError(f"{settings_field}.min_free_percent must be <= 100")
        normalized_settings["min_free_percent"] = min_percent
        normalized_settings["min_free_bytes"] = int(
            _number(
                settings.get("min_free_bytes", 0),
                f"{settings_field}.min_free_bytes",
                minimum=0,
                allow_integer_only=True,
            )
        )
    elif check_type == "file_freshness":
        settings_allowed.update(
            {"path", "pattern", "max_age_seconds", "minimum_matches"}
        )
        normalized_settings["path"] = str(
            _absolute_path(settings.get("path"), f"{settings_field}.path")
        )
        pattern = _require_string(
            settings.get("pattern", "*"), f"{settings_field}.pattern"
        )
        if (
            Path(pattern).is_absolute()
            or ".." in Path(pattern).parts
            or "\x00" in pattern
            or len(pattern) > 256
        ):
            raise ConfigError(
                f"{settings_field}.pattern must be a bounded relative glob"
            )
        normalized_settings["pattern"] = pattern
        normalized_settings["max_age_seconds"] = float(
            _number(
                settings.get("max_age_seconds", 172800),
                f"{settings_field}.max_age_seconds",
                minimum=1,
            )
        )
        normalized_settings["minimum_matches"] = int(
            _number(
                settings.get("minimum_matches", 1),
                f"{settings_field}.minimum_matches",
                minimum=1,
                allow_integer_only=True,
            )
        )
    if "recovery" in settings:
        action_context = dict(normalized_settings)
        normalized_settings["recovery"] = _normalize_action(
            settings["recovery"], f"{settings_field}.recovery", action_context
        )
    _check_unknown_keys(settings, settings_allowed, settings_field)
    _check_unknown_keys(check, allowed, field)
    return normalized


def load_config(path: str | os.PathLike[str]) -> MonitorConfig:
    """Load, validate, and normalize a JSON configuration.

    All filesystem paths in the returned configuration are resolved absolute
    paths. Persistent/output paths are required explicitly so a service never
    depends on launchd/systemd's working directory.
    """

    source_path = Path(path).expanduser().resolve(strict=False)
    try:
        with source_path.open(encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {source_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {source_path}: {exc}") from exc
    config = _require_mapping(raw, "config")
    _reject_forbidden_keys(config)
    allowed = {
        "checks",
        "cooldown_seconds",
        "executables",
        "failure_threshold",
        "heartbeat",
        "heartbeat_path",
        "host",
        "interval_seconds",
        "max_attempts",
        "medic_config_path",
        "profile",
        "schema_version",
        "state_path",
    }
    _check_unknown_keys(config, allowed, "config")

    checks_raw = config.get("checks")
    if not isinstance(checks_raw, list) or not checks_raw:
        raise ConfigError("config.checks must be a non-empty JSON array")
    checks = tuple(
        _normalize_check(check, index) for index, check in enumerate(checks_raw)
    )
    ids = [check["id"] for check in checks]
    if len(ids) != len(set(ids)):
        raise ConfigError("config.checks contains duplicate ids")

    executables_raw = config.get("executables", {})
    executables_obj = _require_mapping(executables_raw, "config.executables")
    unknown_executables = sorted(set(executables_obj) - _EXECUTABLE_NAMES)
    if unknown_executables:
        raise ConfigError(
            "config.executables has unsupported names: "
            + ", ".join(unknown_executables)
        )
    executables = {
        name: _absolute_path(value, f"config.executables.{name}")
        for name, value in executables_obj.items()
    }

    schema_version = config.get("schema_version")
    if schema_version != 1:
        raise ConfigError("config.schema_version must be 1")
    profile = _require_string(config.get("profile"), "config.profile")

    heartbeat: dict[str, Any] | None = None
    if config.get("heartbeat") is not None:
        heartbeat_raw = _require_mapping(config["heartbeat"], "config.heartbeat")
        _check_unknown_keys(
            heartbeat_raw,
            {"url", "secret_env"},
            "config.heartbeat",
        )
        url_value = heartbeat_raw.get("url")
        if isinstance(url_value, str) and url_value.strip():
            url = url_value.strip()
            parsed = urllib.parse.urlsplit(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ConfigError(
                    "config.heartbeat.url must be an HTTPS URL without "
                    "embedded credentials"
                )
            heartbeat = {"url": url}
        elif url_value not in (None, ""):
            raise ConfigError("config.heartbeat.url must be a string")
        if "secret_env" in heartbeat_raw and heartbeat_raw["secret_env"]:
            env_name = _require_string(
                heartbeat_raw["secret_env"],
                "config.heartbeat.secret_env",
            )
            if not re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_]*$", env_name):
                raise ConfigError(
                    "config.heartbeat.secret_env must be an environment variable name"
                )
            if heartbeat is None:
                raise ConfigError(
                    "config.heartbeat.secret_env requires config.heartbeat.url"
                )
            heartbeat["secret_env"] = env_name

    return MonitorConfig(
        schema_version=schema_version,
        profile=profile,
        source_path=source_path,
        state_path=_absolute_path(config.get("state_path"), "config.state_path"),
        heartbeat_path=_absolute_path(
            config.get("heartbeat_path"), "config.heartbeat_path"
        ),
        medic_config_path=(
            _absolute_path(
                config.get("medic_config_path"),
                "config.medic_config_path",
            )
            if config.get("medic_config_path")
            else None
        ),
        interval_seconds=float(
            _number(
                config.get("interval_seconds", 120),
                "config.interval_seconds",
                minimum=1,
            )
        ),
        failure_threshold=int(
            _number(
                config.get("failure_threshold", 2),
                "config.failure_threshold",
                minimum=1,
                allow_integer_only=True,
            )
        ),
        cooldown_seconds=float(
            _number(
                config.get("cooldown_seconds", 300),
                "config.cooldown_seconds",
                minimum=0,
            )
        ),
        max_attempts=int(
            _number(
                config.get("max_attempts", 3),
                "config.max_attempts",
                minimum=0,
                allow_integer_only=True,
            )
        ),
        host=_require_string(config.get("host", socket.gethostname()), "config.host"),
        checks=checks,
        executables=executables,
        heartbeat=heartbeat,
    )


def _process_detail(result: ProcessResult) -> str:
    output = _trim(result.stderr or result.stdout)
    return output or f"exit {result.returncode}"


def run_check(
    config: MonitorConfig,
    check: Mapping[str, Any],
    *,
    process_runner: ProcessRunner | None = None,
    urlopen: Callable[..., Any] | None = None,
    socket_connector: Callable[..., Any] | None = None,
    disk_usage: Callable[[str], Any] | None = None,
    time_fn: Callable[[], float] | None = None,
) -> CheckResult:
    """Run one validated typed check."""

    process_runner = process_runner or _run_process
    urlopen = urlopen or urllib.request.urlopen
    socket_connector = socket_connector or socket.create_connection
    disk_usage = disk_usage or shutil.disk_usage
    time_fn = time_fn or time.time
    check_id = str(check["id"])
    check_type = str(check["type"])
    settings = check.get("settings", {})
    timeout_seconds = float(check.get("timeout_seconds", 10.0))
    started = time.monotonic()
    ok = False
    detail = ""
    try:
        if check_type == "docker_runtime":
            docker_argv = [config.executable("docker")]
            context = settings.get("context")
            if context:
                docker_argv.extend(["--context", str(context)])
            docker_argv.extend(["info", "--format", "{{.ServerVersion}}"])
            result = process_runner(
                docker_argv,
                timeout_seconds,
                None,
            )
            ok = result.returncode == 0
            detail = (
                f"docker server {_trim(result.stdout)}"
                if ok
                else f"docker unavailable: {_process_detail(result)}"
            )
        elif check_type == "docker_container":
            docker_argv = [config.executable("docker")]
            context = settings.get("context")
            if context:
                docker_argv.extend(["--context", str(context)])
            docker_argv.extend(
                [
                    "inspect",
                    "--format",
                    "{{json .State}}",
                    "--",
                    str(settings["container"]),
                ]
            )
            result = process_runner(
                docker_argv,
                timeout_seconds,
                None,
            )
            if result.returncode != 0:
                detail = f"container unavailable: {_process_detail(result)}"
            else:
                try:
                    state = json.loads(result.stdout)
                except json.JSONDecodeError as exc:
                    detail = f"invalid docker state JSON: {exc}"
                else:
                    running = bool(state.get("Running"))
                    health_obj = state.get("Health")
                    health = (
                        str(health_obj.get("Status", "")).lower()
                        if isinstance(health_obj, dict)
                        else ""
                    )
                    if not running:
                        detail = f"container {state.get('Status', 'not running')}"
                    elif health and health != "healthy":
                        detail = f"container running, health={health}"
                    elif settings.get("require_healthcheck") and not health:
                        detail = "container running, no healthcheck"
                    else:
                        ok = True
                        detail = (
                            f"container running, health={health}"
                            if health
                            else "container running"
                        )
        elif check_type == "http":
            request = urllib.request.Request(
                str(settings["url"]),
                method=str(settings.get("method", "GET")),
                headers={"User-Agent": "devbrain-resilience/1"},
            )
            try:
                response = urlopen(request, timeout=timeout_seconds)
                try:
                    status = int(response.status)
                finally:
                    response.close()
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
            ok = int(settings["status_min"]) <= status <= int(settings["status_max"])
            detail = f"HTTP {status}"
        elif check_type == "tcp":
            connection = socket_connector(
                (str(settings["host"]), int(settings["port"])),
                timeout=timeout_seconds,
            )
            try:
                ok = True
                detail = f"connected to {settings['host']}:{settings['port']}"
            finally:
                connection.close()
        elif check_type == "launchd_label":
            target = f"{settings['domain']}/{settings['label']}"
            result = process_runner(
                [config.executable("launchctl"), "print", target],
                timeout_seconds,
                None,
            )
            loaded = result.returncode == 0
            running = bool(re.search(r"(?m)^\s*state\s*=\s*running\s*$", result.stdout))
            ok = loaded and (running or not bool(settings.get("require_running", True)))
            detail = (
                "loaded and running"
                if ok and running
                else "loaded"
                if ok
                else f"not healthy: {_process_detail(result)}"
                if not loaded
                else "loaded but not running"
            )
        elif check_type == "systemd_user_unit":
            result = process_runner(
                [
                    config.executable("systemctl"),
                    "--user",
                    "is-active",
                    "--quiet",
                    "--",
                    str(settings["unit"]),
                ],
                timeout_seconds,
                None,
            )
            ok = result.returncode == 0
            detail = "active" if ok else f"inactive: {_process_detail(result)}"
        elif check_type == "disk":
            usage = disk_usage(str(settings["path"]))
            free_percent = (100.0 * usage.free / usage.total) if usage.total else 0.0
            min_percent = float(settings["min_free_percent"])
            min_bytes = int(settings["min_free_bytes"])
            ok = free_percent >= min_percent and usage.free >= min_bytes
            detail = (
                f"{free_percent:.1f}% free ({usage.free} bytes); "
                f"minimum {min_percent:.1f}%/{min_bytes} bytes"
            )
        elif check_type == "file_freshness":
            target = Path(str(settings["path"]))
            if target.is_file():
                matches = [target]
            elif target.is_dir():
                matches = [
                    path
                    for path in target.glob(str(settings["pattern"]))
                    if path.is_file()
                ]
            else:
                matches = []
            minimum_matches = int(settings["minimum_matches"])
            if len(matches) < minimum_matches:
                detail = f"{len(matches)} matching file(s); minimum {minimum_matches}"
            else:
                latest = max(path.stat().st_mtime for path in matches)
                age = max(0.0, float(time_fn()) - latest)
                maximum = float(settings["max_age_seconds"])
                ok = age <= maximum
                detail = (
                    f"newest of {len(matches)} file(s) is {age:.0f}s old; "
                    f"maximum {maximum:.0f}s"
                )
        else:  # load_config rejects this; preserve fail-closed behavior for callers.
            detail = f"unsupported check type: {check_type}"
    except subprocess.TimeoutExpired:
        detail = f"timed out after {timeout_seconds:g}s"
    except (OSError, RuntimeError, ValueError, urllib.error.URLError) as exc:
        detail = f"{type(exc).__name__}: {_trim(str(exc))}"
    duration_ms = int((time.monotonic() - started) * 1000)
    return CheckResult(check_id, check_type, ok, detail, duration_ms)


def execute_recovery(
    config: MonitorConfig,
    check: Mapping[str, Any],
    action: Mapping[str, Any],
    *,
    process_runner: ProcessRunner | None = None,
) -> ActionResult:
    """Execute one validated typed recovery action without a shell."""

    process_runner = process_runner or _run_process
    check_id = str(check["id"])
    action_type = str(action["type"])
    timeout_seconds = float(action.get("timeout_seconds", 60.0))
    started = time.monotonic()
    argv: list[str]
    cwd: Path | None = None
    try:
        if action_type == "runtime_start":
            runtime = str(action["runtime"])
            if runtime == "colima":
                argv = [config.executable("colima"), "start"]
            elif runtime == "docker_desktop":
                argv = [config.executable("open"), "-a", "Docker"]
            elif runtime == "orbstack":
                argv = [config.executable("open"), "-a", "OrbStack"]
            else:
                raise RuntimeError(f"unsupported runtime: {runtime}")
        elif action_type == "docker_compose_up":
            argv = [config.executable("docker")]
            if action.get("context"):
                argv.extend(["--context", str(action["context"])])
            argv.extend(["compose", "up", "-d"])
            argv.extend(str(service) for service in action.get("services", []))
            cwd = Path(str(action["project_dir"]))
        elif action_type == "launchd_kickstart":
            target = f"{action['domain']}/{action['label']}"
            argv = [config.executable("launchctl"), "kickstart", "-k", target]
        elif action_type == "systemd_restart":
            argv = [
                config.executable("systemctl"),
                "--user",
                "restart",
                "--",
                str(action["unit"]),
            ]
        elif action_type == "homebrew_service_start":
            argv = [
                config.executable("brew"),
                "services",
                "start",
                str(action["service"]),
            ]
        else:
            raise RuntimeError(f"unsupported recovery type: {action_type}")
        result = process_runner(argv, timeout_seconds, cwd)
        ok = result.returncode == 0
        detail = "started" if ok else _process_detail(result)
    except subprocess.TimeoutExpired:
        ok = False
        detail = f"timed out after {timeout_seconds:g}s"
    except (OSError, RuntimeError, ValueError) as exc:
        ok = False
        detail = f"{type(exc).__name__}: {_trim(str(exc))}"
    return ActionResult(
        check_id,
        action_type,
        ok,
        detail,
        int((time.monotonic() - started) * 1000),
    )


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically replace a JSON file and fsync both file and parent directory."""

    path = path.resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _load_state(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            state = json.load(handle)
        if (
            not isinstance(state, dict)
            or state.get("version") != _STATE_VERSION
            or not isinstance(state.get("checks"), dict)
        ):
            raise ValueError("unsupported state schema")
        return state
    except FileNotFoundError:
        return {"version": _STATE_VERSION, "checks": {}}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("ignoring unreadable resilience state %s: %s", path, exc)
        return {"version": _STATE_VERSION, "checks": {}}


def read_heartbeat(config: MonitorConfig) -> dict[str, Any]:
    try:
        with config.heartbeat_path.open(encoding="utf-8") as handle:
            heartbeat = json.load(handle)
    except FileNotFoundError as exc:
        raise RuntimeError(f"heartbeat not found: {config.heartbeat_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"heartbeat unreadable: {config.heartbeat_path}: {exc}"
        ) from exc
    if (
        not isinstance(heartbeat, dict)
        or heartbeat.get("version") != _HEARTBEAT_VERSION
    ):
        raise RuntimeError("heartbeat has an unsupported schema")
    return heartbeat


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def send_webhook(
    config: MonitorConfig,
    heartbeat: Mapping[str, Any],
    *,
    urlopen: Callable[..., Any] | None = None,
) -> WebhookResult:
    """POST a heartbeat to an optional HTTPS endpoint.

    If ``hmac_secret_env`` is configured but absent, delivery fails closed and
    no unsigned request is sent.
    """

    if config.heartbeat is None:
        return WebhookResult(False, False, "not configured")
    urlopen = urlopen or urllib.request.urlopen
    body = _canonical_json(heartbeat)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "devbrain-resilience/1",
    }
    secret_env = config.heartbeat.get("secret_env")
    if secret_env:
        secret = os.getenv(str(secret_env))
        if not secret:
            return WebhookResult(
                True, False, f"required HMAC environment variable {secret_env} is unset"
            )
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-DevBrain-Signature"] = f"sha256={digest}"
    request = urllib.request.Request(
        str(config.heartbeat["url"]), data=body, method="POST", headers=headers
    )
    try:
        response = urlopen(request, timeout=10.0)
        try:
            status = int(response.status)
        finally:
            response.close()
        if 200 <= status < 300:
            return WebhookResult(True, True, f"HTTP {status}")
        return WebhookResult(True, False, f"HTTP {status}")
    except urllib.error.HTTPError as exc:
        return WebhookResult(True, False, f"HTTP {exc.code}")
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return WebhookResult(True, False, f"{type(exc).__name__}: {_trim(str(exc))}")


class Monitor:
    """Run checks, apply bounded recovery policy, and persist local status."""

    def __init__(
        self,
        config: MonitorConfig,
        *,
        check_executor: CheckExecutor | None = None,
        action_executor: ActionExecutor | None = None,
        webhook_sender: WebhookSender | None = None,
        time_fn: Callable[[], float] | None = None,
    ):
        self.config = config
        self._check_executor = check_executor or run_check
        self._action_executor = action_executor or execute_recovery
        self._webhook_sender = webhook_sender or send_webhook
        self._time_fn = time_fn or time.time
        self._state = _load_state(config.state_path)

    def _persist_state(self, now: float, *, prune: bool = False) -> None:
        state_checks = self._state.setdefault("checks", {})
        if prune:
            configured_ids = {
                str(check["id"])
                for check in self.config.checks
                if bool(check.get("enabled", True))
            }
            self._state["checks"] = {
                check_id: value
                for check_id, value in state_checks.items()
                if check_id in configured_ids
            }
        self._state["version"] = _STATE_VERSION
        self._state["updated_at"] = now
        self._state["updated_at_iso"] = _timestamp(now)
        atomic_write_json(self.config.state_path, self._state)

    def run_targeted_recovery(self, check_id: str) -> ActionResult:
        """Apply the normal local policy to one fresh, signed heal request."""

        self._state = _load_state(self.config.state_path)
        now = float(self._time_fn())
        check = next(
            (
                candidate
                for candidate in self.config.checks
                if candidate["id"] == check_id and bool(candidate.get("enabled", True))
            ),
            None,
        )
        if check is None:
            return ActionResult(
                check_id,
                "policy",
                False,
                "unknown or disabled check; recovery not run",
            )
        settings = check.get("settings", {})
        action = settings.get("recovery")
        action_type = (
            str(action.get("type", "policy")) if isinstance(action, dict) else "policy"
        )
        if not isinstance(action, dict):
            return ActionResult(
                check_id,
                action_type,
                False,
                "check has no typed recovery",
            )

        try:
            result = self._check_executor(self.config, check)
        except Exception as exc:
            logger.exception("targeted check %s crashed", check_id)
            result = CheckResult(
                check_id,
                str(check["type"]),
                False,
                f"check crashed: {type(exc).__name__}: {_trim(str(exc))}",
            )

        state_checks = self._state.setdefault("checks", {})
        entry = state_checks.get(check_id)
        if not isinstance(entry, dict):
            entry = {}
            state_checks[check_id] = entry
        entry["last_checked_at"] = now
        entry["last_ok"] = result.ok
        entry["last_detail"] = result.detail
        if result.ok:
            entry["consecutive_failures"] = 0
            entry["recovery_attempts"] = 0
            self._persist_state(now)
            return ActionResult(
                check_id,
                action_type,
                False,
                "fresh check is healthy; recovery not run",
            )

        failures = int(entry.get("consecutive_failures", 0)) + 1
        attempts = int(entry.get("recovery_attempts", 0))
        threshold = int(
            settings.get("failure_threshold", self.config.failure_threshold)
        )
        cooldown = float(settings.get("cooldown_seconds", self.config.cooldown_seconds))
        max_attempts = int(settings.get("max_attempts", self.config.max_attempts))
        last_recovery_at = entry.get("last_recovery_at")
        entry["consecutive_failures"] = failures

        blocked_reason = ""
        if failures < threshold:
            blocked_reason = f"failure threshold not reached ({failures}/{threshold})"
        elif attempts >= max_attempts:
            blocked_reason = (
                f"maximum recovery attempts reached ({attempts}/{max_attempts})"
            )
        elif last_recovery_at is not None and now - float(last_recovery_at) < cooldown:
            remaining = cooldown - (now - float(last_recovery_at))
            blocked_reason = f"recovery cooldown active ({remaining:.0f}s remaining)"
        if blocked_reason:
            self._persist_state(now)
            return ActionResult(check_id, action_type, False, blocked_reason)

        try:
            recovery = self._action_executor(self.config, check, action)
        except Exception as exc:
            logger.exception("targeted recovery for %s crashed", check_id)
            recovery = ActionResult(
                check_id,
                action_type,
                False,
                f"recovery crashed: {type(exc).__name__}: {_trim(str(exc))}",
            )
        entry["recovery_attempts"] = attempts + 1
        entry["last_recovery_at"] = now
        entry["last_recovery_ok"] = recovery.ok
        entry["last_recovery_detail"] = recovery.detail
        self._persist_state(now)
        return recovery

    def run_cycle(self) -> CycleResult:
        # A signed medic request can update the same policy state between
        # regular cycles. Reload so cooldown and attempt limits stay coherent.
        self._state = _load_state(self.config.state_path)
        now = float(self._time_fn())
        state_checks = self._state.setdefault("checks", {})
        heartbeat_checks: dict[str, Any] = {}
        recoveries: list[dict[str, Any]] = []

        for check in self.config.checks:
            check_id = str(check["id"])
            required = bool(check.get("required", True))
            enabled = bool(check.get("enabled", True))
            if not enabled:
                heartbeat_checks[check_id] = {
                    "type": str(check["type"]),
                    "enabled": False,
                    "required": required,
                    "ok": None,
                    "detail": "disabled",
                    "duration_ms": 0,
                }
                state_checks.pop(check_id, None)
                continue
            try:
                result = self._check_executor(self.config, check)
            except Exception as exc:  # one broken probe must not suppress all others
                logger.exception("check %s crashed", check_id)
                result = CheckResult(
                    check_id,
                    str(check["type"]),
                    False,
                    f"check crashed: {type(exc).__name__}: {_trim(str(exc))}",
                )
            heartbeat_checks[check_id] = {
                **result.as_dict(),
                "enabled": True,
                "required": required,
            }
            entry = state_checks.get(check_id)
            if not isinstance(entry, dict):
                entry = {}
                state_checks[check_id] = entry
            entry["last_checked_at"] = now
            entry["last_ok"] = result.ok
            entry["last_detail"] = result.detail

            if result.ok:
                entry["consecutive_failures"] = 0
                entry["recovery_attempts"] = 0
                continue

            failures = int(entry.get("consecutive_failures", 0)) + 1
            entry["consecutive_failures"] = failures
            settings = check.get("settings", {})
            action = settings.get("recovery")
            threshold = int(
                settings.get("failure_threshold", self.config.failure_threshold)
            )
            cooldown = float(
                settings.get("cooldown_seconds", self.config.cooldown_seconds)
            )
            max_attempts = int(settings.get("max_attempts", self.config.max_attempts))
            attempts = int(entry.get("recovery_attempts", 0))
            last_recovery_at = entry.get("last_recovery_at")
            cooldown_elapsed = (
                last_recovery_at is None or now - float(last_recovery_at) >= cooldown
            )
            if (
                isinstance(action, dict)
                and failures >= threshold
                and attempts < max_attempts
                and cooldown_elapsed
            ):
                try:
                    recovery = self._action_executor(self.config, check, action)
                except Exception as exc:
                    logger.exception("recovery for %s crashed", check_id)
                    recovery = ActionResult(
                        check_id,
                        str(action.get("type", "unknown")),
                        False,
                        f"recovery crashed: {type(exc).__name__}: {_trim(str(exc))}",
                    )
                entry["recovery_attempts"] = attempts + 1
                entry["last_recovery_at"] = now
                entry["last_recovery_ok"] = recovery.ok
                entry["last_recovery_detail"] = recovery.detail
                recoveries.append(recovery.as_dict())

        self._persist_state(now, prune=True)

        healthy = all(
            bool(result["ok"])
            for result in heartbeat_checks.values()
            if bool(result["enabled"]) and bool(result["required"])
        )
        heartbeat: dict[str, Any] = {
            "version": _HEARTBEAT_VERSION,
            "schema_version": self.config.schema_version,
            "profile": self.config.profile,
            "host": self.config.host,
            "generated_at": _timestamp(now),
            "generated_at_epoch": now,
            "healthy": healthy,
            "checks": heartbeat_checks,
            "recoveries": recoveries,
            "config_path": str(self.config.source_path),
        }
        atomic_write_json(self.config.heartbeat_path, heartbeat)
        try:
            webhook = self._webhook_sender(self.config, heartbeat)
        except Exception as exc:
            logger.exception("webhook sender crashed")
            webhook = WebhookResult(
                self.config.heartbeat is not None,
                False,
                f"webhook crashed: {type(exc).__name__}: {_trim(str(exc))}",
            )
        return CycleResult(heartbeat, webhook)
