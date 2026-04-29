"""Tests for AICliAdapter ABC + AdapterRegistry."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ai_clis.base import (
    AdapterRegistry,
    AICliAdapter,
    LoginResult,
    SpawnArgs,
)


class _FakeAdapter(AICliAdapter):
    """Concrete adapter for testing the ABC contract."""

    name = "fake"
    oauth_callback_ports: list[int] = []

    def spawn_args(self, dev, profile_dir: Path) -> SpawnArgs:
        return SpawnArgs(env={"FAKE": "1"}, argv_prefix=["fake"])

    def login(self, dev, profile_dir: Path) -> LoginResult:
        return LoginResult(success=True)

    def is_logged_in(self, dev, profile_dir: Path) -> bool:
        return True

    def required_dotfiles(self) -> list[str]:
        return [".fake/auth.json"]


def test_abc_cannot_instantiate_directly():
    with pytest.raises(TypeError):
        AICliAdapter()  # type: ignore[abstract]


def test_concrete_adapter_implements_contract():
    a = _FakeAdapter()
    fake_dev = MagicMock()
    profile = Path("/tmp/fake-profile")
    assert a.name == "fake"
    spawn = a.spawn_args(fake_dev, profile)
    assert isinstance(spawn, SpawnArgs)
    assert spawn.env == {"FAKE": "1"}
    assert spawn.argv_prefix == ["fake"]
    assert a.login(fake_dev, profile).success is True
    assert a.is_logged_in(fake_dev, profile) is True
    assert a.required_dotfiles() == [".fake/auth.json"]


def test_spawn_args_dataclass_defaults():
    s = SpawnArgs()
    assert s.env == {}
    assert s.argv_prefix == []


def test_login_result_dataclass():
    r1 = LoginResult(success=True)
    assert r1.success is True
    assert r1.error is None
    assert r1.hint is None

    r2 = LoginResult(success=False, error="bad", hint="do X")
    assert r2.error == "bad"
    assert r2.hint == "do X"


def test_registry_register_and_get():
    reg = AdapterRegistry()
    reg.register(_FakeAdapter)
    assert reg.get("fake") is _FakeAdapter


def test_registry_get_unknown_raises_keyerror():
    reg = AdapterRegistry()
    with pytest.raises(KeyError):
        reg.get("nope")


def test_registry_register_duplicate_raises():
    reg = AdapterRegistry()
    reg.register(_FakeAdapter)
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_FakeAdapter)


def test_registry_list_names():
    reg = AdapterRegistry()
    reg.register(_FakeAdapter)
    assert reg.list_names() == ["fake"]


def test_registry_all_returns_classes():
    reg = AdapterRegistry()
    reg.register(_FakeAdapter)
    all_adapters = reg.all()
    assert len(all_adapters) == 1
    assert all_adapters[0] is _FakeAdapter


def test_registry_register_rejects_class_without_name():
    class NamelessAdapter(AICliAdapter):
        name = ""
        oauth_callback_ports: list[int] = []

        def spawn_args(self, dev, profile_dir):
            return SpawnArgs()

        def login(self, dev, profile_dir):
            return LoginResult(success=True)

        def is_logged_in(self, dev, profile_dir):
            return False

        def required_dotfiles(self):
            return []

    reg = AdapterRegistry()
    with pytest.raises(ValueError, match="must define"):
        reg.register(NamelessAdapter)
