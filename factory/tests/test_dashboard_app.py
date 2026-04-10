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
async def test_dashboard_shows_all_panels():
    """All four panels are mounted on the dashboard."""
    from dashboard.widgets.jobs_panel import ActiveJobsPanel
    from dashboard.widgets.events_panel import RecentEventsPanel
    from dashboard.widgets.locks_panel import FileLocksPanel
    from dashboard.widgets.completed_panel import RecentCompletedPanel

    app = DashboardApp()
    async with app.run_test() as pilot:
        await pilot.pause(0.1)  # let on_mount fire
        assert app.query_one(ActiveJobsPanel) is not None
        assert app.query_one(RecentEventsPanel) is not None
        assert app.query_one(FileLocksPanel) is not None
        assert app.query_one(RecentCompletedPanel) is not None


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
