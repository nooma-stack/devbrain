"""Tests for factory.profiles."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import profiles
from profiles import (
    ProfileInfo,
    delete_profile,
    get_profile_dir,
    list_profiles,
    populate_gitconfig,
    profiles_root,
    refresh_shared_symlinks,
    validate_dev_id,
)


@pytest.fixture
def isolated_root(tmp_path: Path, monkeypatch):
    """Redirect profiles_root() to a tmp dir for the test."""
    fake_root = tmp_path / "profiles-root"
    fake_root.mkdir()
    monkeypatch.setattr(profiles, "_PROFILES_ROOT_OVERRIDE", fake_root, raising=False)
    yield fake_root


# --- validate_dev_id ---


@pytest.mark.parametrize("dev_id", ["alice", "bob_2", "pkrelay-dev", "a", "x123_45"])
def test_validate_dev_id_accepts_valid(dev_id: str):
    validate_dev_id(dev_id)  # no raise


@pytest.mark.parametrize("dev_id", [
    "../etc/passwd",
    "alice/../bob",
    "/abs/path",
    "alice/sub",
    ".",
    "..",
])
def test_validate_dev_id_rejects_path_traversal(dev_id: str):
    with pytest.raises(ValueError):
        validate_dev_id(dev_id)


@pytest.mark.parametrize("dev_id", ["", "x" * 65])
def test_validate_dev_id_rejects_empty_or_too_long(dev_id: str):
    with pytest.raises(ValueError):
        validate_dev_id(dev_id)


@pytest.mark.parametrize("dev_id", ["alice!", "alice@host", "alice space", "ALICE", "alice."])
def test_validate_dev_id_rejects_special_chars(dev_id: str):
    with pytest.raises(ValueError):
        validate_dev_id(dev_id)


# --- get_profile_dir ---


def test_get_profile_dir_creates_if_missing(isolated_root: Path):
    p = get_profile_dir("alice")
    assert p == isolated_root / "alice"
    assert p.exists()
    assert p.is_dir()


def test_get_profile_dir_returns_existing(isolated_root: Path):
    (isolated_root / "alice").mkdir()
    p = get_profile_dir("alice")
    assert p == isolated_root / "alice"


def test_get_profile_dir_validates_id_first(isolated_root: Path):
    with pytest.raises(ValueError):
        get_profile_dir("../etc/passwd")
    # Confirm nothing was created outside the root
    assert not (isolated_root.parent / "etc").exists()


# --- list_profiles ---


def test_list_profiles_empty(isolated_root: Path):
    assert list_profiles() == []


def test_list_profiles_returns_dev_ids(isolated_root: Path):
    (isolated_root / "alice").mkdir()
    (isolated_root / "bob").mkdir()
    profs = list_profiles()
    assert {p.dev_id for p in profs} == {"alice", "bob"}
    assert all(isinstance(p, ProfileInfo) for p in profs)


def test_list_profiles_skips_hidden_dirs(isolated_root: Path):
    (isolated_root / "alice").mkdir()
    (isolated_root / ".cache").mkdir()
    profs = list_profiles()
    assert {p.dev_id for p in profs} == {"alice"}


def test_list_profiles_skips_files(isolated_root: Path):
    (isolated_root / "alice").mkdir()
    (isolated_root / "stray.txt").write_text("x")
    profs = list_profiles()
    assert {p.dev_id for p in profs} == {"alice"}


# --- populate_gitconfig ---


def test_populate_gitconfig_writes_user_block(tmp_path: Path):
    populate_gitconfig(tmp_path, "Alice Smith", "alice@example.com")
    contents = (tmp_path / ".gitconfig").read_text()
    assert "[user]" in contents
    assert "name = Alice Smith" in contents
    assert "email = alice@example.com" in contents


def test_populate_gitconfig_overwrites_existing(tmp_path: Path):
    (tmp_path / ".gitconfig").write_text("[user]\n  name = Old Name\n")
    populate_gitconfig(tmp_path, "Alice Smith", "alice@example.com")
    contents = (tmp_path / ".gitconfig").read_text()
    assert "Old Name" not in contents
    assert "Alice Smith" in contents


def test_populate_gitconfig_escapes_special_chars(tmp_path: Path):
    populate_gitconfig(tmp_path, "Alice \"Quoted\" Smith", "alice@x.com")
    contents = (tmp_path / ".gitconfig").read_text()
    # name field shouldn't break parsing — just verify roundtrip
    assert "Alice" in contents
    assert "alice@x.com" in contents


# --- refresh_shared_symlinks ---


def test_refresh_shared_symlinks_creates_symlinks(tmp_path: Path):
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    (fake_home / ".npmrc").write_text("//registry.npmjs.org/:_authToken=secret\n")
    profile = tmp_path / "profile"
    profile.mkdir()

    refresh_shared_symlinks(profile, host_home=fake_home, shared_paths=[".npmrc"])

    link = profile / ".npmrc"
    assert link.is_symlink()
    assert link.resolve() == (fake_home / ".npmrc").resolve()


def test_refresh_shared_symlinks_skips_missing_source(tmp_path: Path):
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    profile = tmp_path / "profile"
    profile.mkdir()

    refresh_shared_symlinks(profile, host_home=fake_home, shared_paths=[".npmrc"])
    assert not (profile / ".npmrc").exists()


def test_refresh_shared_symlinks_replaces_existing_symlink(tmp_path: Path):
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    old_target = tmp_path / "old"
    old_target.write_text("old")
    new_target = fake_home / ".npmrc"
    new_target.write_text("new")

    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / ".npmrc").symlink_to(old_target)

    refresh_shared_symlinks(profile, host_home=fake_home, shared_paths=[".npmrc"])

    link = profile / ".npmrc"
    assert link.is_symlink()
    assert link.resolve() == new_target.resolve()


def test_refresh_shared_symlinks_handles_nested_paths(tmp_path: Path):
    fake_home = tmp_path / "fake-home"
    (fake_home / ".config" / "gcloud").mkdir(parents=True)
    (fake_home / ".config" / "gcloud" / "credentials.json").write_text("{}")
    profile = tmp_path / "profile"
    profile.mkdir()

    refresh_shared_symlinks(profile, host_home=fake_home, shared_paths=[".config/gcloud"])

    link = profile / ".config" / "gcloud"
    assert link.is_symlink()
    assert link.resolve() == (fake_home / ".config" / "gcloud").resolve()


# --- delete_profile ---


def test_delete_profile_removes_dir(isolated_root: Path):
    p = get_profile_dir("alice")
    (p / ".claude.json").write_text("{}")
    delete_profile("alice")
    assert not p.exists()


def test_delete_profile_idempotent(isolated_root: Path):
    delete_profile("alice")  # Doesn't exist — should not raise


def test_delete_profile_validates_id(isolated_root: Path):
    with pytest.raises(ValueError):
        delete_profile("../etc/passwd")


# --- profiles_root ---


def test_profiles_root_uses_devbrain_home_subdir():
    """Without override, profiles_root resolves to <DEVBRAIN_HOME>/profiles."""
    # Just verify it returns a Path under DEVBRAIN_HOME
    from config import DEVBRAIN_HOME

    root = profiles_root()
    assert root == Path(DEVBRAIN_HOME) / "profiles"
