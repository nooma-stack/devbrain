"""Tests for NotificationChannel base and registry."""
import pytest
from notifications.base import NotificationChannel, ChannelResult, ChannelRegistry


class DummyChannel(NotificationChannel):
    name = "dummy"

    def is_configured(self) -> bool:
        return self.config.get("enabled", False)

    def send(self, address, title, body):
        return ChannelResult(
            delivered=True,
            channel=self.name,
            metadata={"address": address},
        )


def test_channel_result_defaults():
    r = ChannelResult(delivered=True, channel="dummy")
    assert r.delivered is True
    assert r.error is None
    assert r.metadata == {}


def test_dummy_channel_protocol():
    ch = DummyChannel(enabled=True)
    assert ch.name == "dummy"
    assert ch.is_configured() is True
    result = ch.send("test@example.com", "T", "B")
    assert result.delivered is True


def test_registry_register_and_get():
    registry = ChannelRegistry()
    registry.register("dummy", DummyChannel)
    assert registry.get("dummy") is DummyChannel


def test_registry_get_unknown():
    registry = ChannelRegistry()
    assert registry.get("nonexistent") is None


def test_registry_list_types():
    registry = ChannelRegistry()
    registry.register("dummy1", DummyChannel)
    registry.register("dummy2", DummyChannel)
    assert set(registry.list_types()) == {"dummy1", "dummy2"}


def test_registry_instantiate():
    registry = ChannelRegistry()
    registry.register("dummy", DummyChannel)
    ch = registry.instantiate("dummy", enabled=True)
    assert isinstance(ch, DummyChannel)
    assert ch.is_configured() is True


def test_registry_instantiate_unknown():
    registry = ChannelRegistry()
    assert registry.instantiate("unknown") is None
