"""Unit tests for the TUI application (LogTailApp)."""

from datetime import UTC, datetime

import pytest

from tail_cw.aws.client import LogEvent
from tail_cw.tui.app import LogTailApp


def _make_test_log_events(count: int = 5) -> list[LogEvent]:
    """Create test LogEvent instances for testing.

    Args:
        count: Number of events to generate

    Returns:
        List of test LogEvent instances with varied data
    """
    events = []
    for i in range(count):
        # Alternate between plain text and JSON messages
        if i % 2 == 0:
            message = f'Test log message {i}'
        else:
            message = f'{{"level":"INFO","index":{i},"message":"Test JSON {i}"}}'

        # Create timestamps that handle large counts properly
        base_time = datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC)
        timestamp = base_time.replace(second=i % 60, microsecond=(i // 60) * 1000)
        ingestion_time = timestamp.replace(microsecond=timestamp.microsecond + 1000)

        events.append(
            LogEvent(
                timestamp=timestamp,
                message=message,
                log_group=f'/aws/test/group{i % 2}',
                log_stream=f'stream-{i}',
                event_id=f'event-{i:04d}',
                ingestion_time=ingestion_time,
            ),
        )
    return events


def test_app_initialization():
    """Test basic app initialization with no events."""
    app = LogTailApp()

    assert app._log_events == []
    assert app._table is None
    assert app.title == 'CloudWatch Logs Viewer'


def test_app_initialization_with_events():
    """Test initialization with events."""
    events = _make_test_log_events(3)
    app = LogTailApp(log_events=events)

    assert app._log_events == events
    assert len(app._log_events) == 3


def test_app_initialization_with_custom_title():
    """Test initialization with custom title."""
    app = LogTailApp(title='Custom Title')

    assert app.title == 'Custom Title'


@pytest.mark.asyncio
async def test_app_compose_structure():
    """Test UI composition has required widgets."""
    app = LogTailApp()

    async with app.run_test() as pilot:
        # Check for Header, Footer, DataTable, and status Label
        header = app.query_one('Header')
        footer = app.query_one('Footer')
        table = app.query_one('#log_table')
        status = app.query_one('#status')

        assert header is not None
        assert footer is not None
        assert table is not None
        assert status is not None


@pytest.mark.asyncio
async def test_table_columns_setup():
    """Test column configuration."""
    events = _make_test_log_events(2)
    app = LogTailApp(log_events=events)

    async with app.run_test() as pilot:
        from textual.widgets import DataTable

        table = app.query_one('#log_table', DataTable)

        # Check column count (5 columns)
        assert len(table.columns) == 5

        # Check column keys
        column_keys = [col.key for col in table.columns.values()]
        assert 'timestamp' in column_keys
        assert 'log_group' in column_keys
        assert 'log_stream' in column_keys
        assert 'message' in column_keys
        assert 'event_id' in column_keys


@pytest.mark.asyncio
async def test_load_events_on_mount():
    """Test that events are loaded on mount."""
    events = _make_test_log_events(5)
    app = LogTailApp(log_events=events)

    async with app.run_test() as pilot:
        from textual.widgets import DataTable, Label

        table = app.query_one('#log_table', DataTable)
        status = app.query_one('#status', Label)

        # Check row count matches event count
        assert table.row_count == 5

        # Check status label shows correct count
        assert '5 events' in str(status.render()).lower()


@pytest.mark.asyncio
async def test_empty_app_shows_no_logs_message():
    """Test empty state."""
    app = LogTailApp()

    async with app.run_test() as pilot:
        from textual.widgets import DataTable, Label

        table = app.query_one('#log_table', DataTable)
        status = app.query_one('#status', Label)

        # Check table is empty
        assert table.row_count == 0

        # Check status message
        assert 'no logs loaded' in str(status.render()).lower()


@pytest.mark.asyncio
async def test_keyboard_navigation():
    """Test cursor movement in table."""
    events = _make_test_log_events(5)
    app = LogTailApp(log_events=events)

    async with app.run_test() as pilot:
        from textual.widgets import DataTable

        table = app.query_one('#log_table', DataTable)

        # Focus the table
        table.focus()
        await pilot.pause()

        # Initial cursor should be at row 0
        assert table.cursor_row == 0

        # Press down arrow
        await pilot.press('down')
        assert table.cursor_row == 1

        # Press down again
        await pilot.press('down')
        assert table.cursor_row == 2


@pytest.mark.asyncio
async def test_quit_binding():
    """Test quit keyboard shortcut."""
    app = LogTailApp()

    async with app.run_test() as pilot:
        # Press 'q' to quit
        await pilot.press('q')
        # App should exit (test completes without error)


@pytest.mark.asyncio
async def test_show_detail_no_selection():
    """Test Enter with no row selected doesn't crash."""
    events = _make_test_log_events(3)
    app = LogTailApp(log_events=events)

    async with app.run_test() as pilot:
        # Don't focus or move cursor, just press Enter
        # This should handle gracefully without error
        await pilot.press('enter')

        # No modal should be shown, screen stack should be 1 (main screen)
        assert len(app.screen_stack) == 1


@pytest.mark.asyncio
async def test_search_placeholder():
    """Test search binding shows notification."""
    app = LogTailApp()

    async with app.run_test() as pilot:
        # Press '/' for search
        await pilot.press('/')

        # Check notification was shown (can't easily assert notification content,
        # but no error means it worked)
        await pilot.pause()


@pytest.mark.asyncio
async def test_refresh_placeholder():
    """Test refresh binding shows notification."""
    app = LogTailApp()

    async with app.run_test() as pilot:
        # Press 'r' for refresh
        await pilot.press('r')

        # No error means notification was shown
        await pilot.pause()


@pytest.mark.asyncio
async def test_load_events_method():
    """Test public load_events method."""
    initial_events = _make_test_log_events(3)
    app = LogTailApp(log_events=initial_events)

    async with app.run_test() as pilot:
        from textual.widgets import DataTable, Label

        table = app.query_one('#log_table', DataTable)
        status = app.query_one('#status', Label)

        # Initial state
        assert table.row_count == 3
        assert '3 events' in str(status.render()).lower()

        # Load new events
        new_events = _make_test_log_events(7)
        app.load_events(new_events)
        await pilot.pause()

        # Check updated state
        assert table.row_count == 7
        assert '7 events' in str(status.render()).lower()


@pytest.mark.asyncio
async def test_timestamp_formatting():
    """Test that timestamps are formatted correctly."""
    events = _make_test_log_events(1)
    app = LogTailApp(log_events=events)

    async with app.run_test() as pilot:
        from textual.widgets import DataTable

        table = app.query_one('#log_table', DataTable)

        # Get first row
        row_key = list(table.rows.keys())[0]
        cells = [table.get_cell(row_key, col.key) for col in table.columns.values()]

        # First cell should be the timestamp (Rich Text object)
        timestamp_cell = cells[0]

        # Check format matches pattern (YYYY-MM-DD HH:MM:SS.mmm)
        # The timestamp should be a Rich Text or string
        timestamp_str = str(timestamp_cell)
        assert '2025-01-15' in timestamp_str
        assert '10:00:00' in timestamp_str


@pytest.mark.asyncio
async def test_message_truncation():
    """Test that long messages are truncated in table."""
    # Create event with very long message
    long_message = 'A' * 200
    event = LogEvent(
        timestamp=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
        message=long_message,
        log_group='/aws/test/group',
        log_stream='stream-0',
        event_id='event-0001',
        ingestion_time=datetime(2025, 1, 15, 10, 0, 1, tzinfo=UTC),
    )

    app = LogTailApp(log_events=[event])

    async with app.run_test() as pilot:
        from textual.widgets import DataTable

        table = app.query_one('#log_table', DataTable)

        # Get message cell (column index 3)
        row_key = list(table.rows.keys())[0]
        message_col = list(table.columns.values())[3]
        message_cell = table.get_cell(row_key, message_col.key)

        # Message should be truncated (max 100 chars + '...')
        message_str = str(message_cell)
        assert len(message_str) <= 103  # 100 + '...'
        assert '...' in message_str


@pytest.mark.asyncio
async def test_batch_loading_performance():
    """Test batch loading with many events."""
    # Create 1000 events
    events = _make_test_log_events(1000)
    app = LogTailApp(log_events=events)

    async with app.run_test() as pilot:
        from textual.widgets import DataTable

        table = app.query_one('#log_table', DataTable)

        # All events should be loaded
        assert table.row_count == 1000

        # App should be responsive (no timeout)
        await pilot.pause()


@pytest.mark.asyncio
async def test_events_with_none_ingestion_time():
    """Test events with None ingestion_time don't cause errors."""
    event = LogEvent(
        timestamp=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
        message='Test message',
        log_group='/aws/test/group',
        log_stream='stream-0',
        event_id='event-0001',
        ingestion_time=None,  # None value
    )

    app = LogTailApp(log_events=[event])

    async with app.run_test() as pilot:
        from textual.widgets import DataTable

        table = app.query_one('#log_table', DataTable)

        # Should load without error
        assert table.row_count == 1


@pytest.mark.asyncio
async def test_events_with_special_characters():
    """Test messages with special characters are handled correctly."""
    event = LogEvent(
        timestamp=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
        message='Special chars: <>&"\\n\\t\u2603',  # Unicode snowman
        log_group='/aws/test/group',
        log_stream='stream-0',
        event_id='event-0001',
        ingestion_time=datetime(2025, 1, 15, 10, 0, 1, tzinfo=UTC),
    )

    app = LogTailApp(log_events=[event])

    async with app.run_test() as pilot:
        from textual.widgets import DataTable

        table = app.query_one('#log_table', DataTable)

        # Should load without error
        assert table.row_count == 1


@pytest.mark.asyncio
async def test_empty_message():
    """Test event with empty message."""
    event = LogEvent(
        timestamp=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
        message='',  # Empty message
        log_group='/aws/test/group',
        log_stream='stream-0',
        event_id='event-0001',
        ingestion_time=datetime(2025, 1, 15, 10, 0, 1, tzinfo=UTC),
    )

    app = LogTailApp(log_events=[event])

    async with app.run_test() as pilot:
        from textual.widgets import DataTable

        table = app.query_one('#log_table', DataTable)

        # Should load without error
        assert table.row_count == 1
