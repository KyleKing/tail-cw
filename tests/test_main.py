"""Unit tests for the __main__ entry point."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import tail_cw.__main__
from tail_cw.__main__ import main
from tail_cw.cli import FetchRequest, TailRequest
from tail_cw.config import TailCWConfig


def test_main_function_exists():
    """Test that main function is defined."""
    assert callable(main)


def test_main_without_subcommand_prints_help(capsys):
    """Bare invocation should print help and exit 2 instead of opening the TUI."""
    result = main([])

    assert result == 2
    assert 'usage: tail-cw' in capsys.readouterr().err


def test_main_returns_run_cli_exit_code():
    """Main should return the exit code produced by run_cli."""
    with patch('tail_cw.__main__.run_cli', return_value=0) as mock_run:
        result = main(['fetch', '/g'])

    assert result == 0
    mock_run.assert_called_once()


def test_main_handles_keyboard_interrupt():
    """Test graceful handling of Ctrl+C."""
    with patch('tail_cw.__main__.run_cli', side_effect=KeyboardInterrupt()):
        result = main(['fetch', '/g'])

    assert result == 0


def test_main_handles_generic_exception(capsys):
    """Unexpected errors should be reported to stderr with exit code 1."""
    with patch('tail_cw.__main__.run_cli', side_effect=RuntimeError('test error')):
        result = main(['fetch', '/g'])

    assert result == 1
    assert 'test error' in capsys.readouterr().err


def test_main_handles_config_error(tmp_path, capsys):
    """Configuration errors should result in exit code 1."""
    config_path = tmp_path / 'config.toml'
    config_path.write_text('not valid toml [', encoding='utf-8')

    result = main(['fetch', '/g', '--config', str(config_path)])

    assert result == 1
    assert 'Configuration error' in capsys.readouterr().err


def test_run_tui_wires_app(tmp_path):
    """The TUI runner should configure the app with the parquet source and fetch context."""
    config = TailCWConfig()
    parquet_path = tmp_path / 'events.parquet'
    request = FetchRequest(
        log_group='/g',
        start_time=datetime.now(tz=UTC) - timedelta(hours=1),
        end_time=datetime.now(tz=UTC),
    )

    with patch('tail_cw.__main__.LogTailApp') as mock_app:
        instance = mock_app.return_value

        tail_cw.__main__._run_tui(config, parquet_path, request)

    mock_app.assert_called_once_with(config=config, parquet_path=parquet_path)
    instance.set_fetch_context.assert_called_once()
    assert instance.set_fetch_context.call_args.args[0] is request
    instance.run.assert_called_once_with()


def test_run_tail_tui_wires_live_stream():
    """The tail TUI runner should start live mode with a stream factory."""
    config = TailCWConfig()
    request = TailRequest(log_groups=('/g',))

    with patch('tail_cw.__main__.LogTailApp') as mock_app:
        instance = mock_app.return_value

        tail_cw.__main__._run_tail_tui(config, request)

    mock_app.assert_called_once_with(config=config, title='CloudWatch Live Tail')
    instance.start_live_tail.assert_called_once()
    assert callable(instance.start_live_tail.call_args.args[0])
    instance.run.assert_called_once_with()
