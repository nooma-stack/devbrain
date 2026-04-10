"""Tests for the DevBrain dashboard Textual app."""
import pytest

from dashboard.app import DashboardApp


@pytest.mark.asyncio
async def test_dashboard_mounts():
    """Dashboard app can be mounted and has the expected title."""
    app = DashboardApp()
    async with app.run_test() as pilot:
        # If we reach here, the app mounted successfully.
        assert app.title == "DevBrain Factory Dashboard"


@pytest.mark.asyncio
async def test_dashboard_refresh_updates_loading():
    """Calling refresh_data updates the loading label (success or error)."""
    from textual.widgets import Static

    app = DashboardApp()
    async with app.run_test() as pilot:
        await pilot.pause(0.1)  # let on_mount fire
        loading = app.query_one("#loading", Static)
        # Static exposes its current content via .render()
        text = str(loading.render())
        # After refresh the label should no longer say "Loading..."
        assert "Active jobs" in text or "Error" in text


@pytest.mark.asyncio
async def test_dashboard_has_bindings():
    """Quit and refresh bindings are registered on the app."""
    app = DashboardApp()
    async with app.run_test() as pilot:
        # BINDINGS entries may be tuples or Binding objects depending on
        # how they were declared. Normalize by extracting the first element.
        binding_keys = []
        for b in app.BINDINGS:
            if isinstance(b, tuple):
                binding_keys.append(b[0])
            else:
                binding_keys.append(b.key)
        assert "q" in binding_keys
        assert "r" in binding_keys
