"""Unit tests for the __main__ entry point."""

from unittest.mock import patch

import tail_cw.__main__
from tail_cw.__main__ import main


def test_main_function_exists():
    """Test that main function is defined."""
    assert callable(main)


def test_main_returns_zero_on_success():
    """Test successful execution."""
    with patch('tail_cw.__main__.LogTailApp') as mock_app:
        # Mock the run method to do nothing
        mock_instance = mock_app.return_value
        mock_instance.run.return_value = None

        # Call main
        result = tail_cw.__main__.main()

        # Should return 0 for success
        assert result == 0

        # LogTailApp should be created
        mock_app.assert_called_once()
        mock_instance.run.assert_called_once()


def test_main_creates_app_instance():
    """Test that main creates LogTailApp."""
    with patch('tail_cw.__main__.LogTailApp') as mock_app:
        mock_instance = mock_app.return_value
        mock_instance.run.return_value = None

        tail_cw.__main__.main()

        # LogTailApp should be instantiated
        mock_app.assert_called_once_with()
        # run() should be called on the instance
        mock_instance.run.assert_called_once_with()


def test_main_handles_keyboard_interrupt():
    """Test graceful handling of Ctrl+C."""
    with patch('tail_cw.__main__.LogTailApp') as mock_app:
        # Mock run to raise KeyboardInterrupt
        mock_instance = mock_app.return_value
        mock_instance.run.side_effect = KeyboardInterrupt()

        # Call main
        result = tail_cw.__main__.main()

        # Should return 0 (graceful exit)
        assert result == 0


def test_main_handles_generic_exception(capsys):
    """Test error handling."""
    with patch('tail_cw.__main__.LogTailApp') as mock_app:
        # Mock run to raise generic exception
        mock_instance = mock_app.return_value
        mock_instance.run.side_effect = Exception('test error')

        # Call main
        result = tail_cw.__main__.main()

        # Should return 1 (error exit code)
        assert result == 1

        # Error should be printed to stderr
        captured = capsys.readouterr()
        assert 'test error' in captured.err


def test_main_with_no_events():
    """Test that app starts with empty state."""
    with patch('tail_cw.__main__.LogTailApp') as mock_app:
        mock_instance = mock_app.return_value
        mock_instance.run.return_value = None

        tail_cw.__main__.main()

        # LogTailApp should be called with no arguments (default empty state)
        mock_app.assert_called_once_with()


def test_main_exception_does_not_crash():
    """Test that exceptions are caught and don't crash."""
    with patch('tail_cw.__main__.LogTailApp') as mock_app:
        mock_instance = mock_app.return_value
        mock_instance.run.side_effect = RuntimeError('unexpected error')

        # Should not raise, just return exit code
        result = tail_cw.__main__.main()

        assert result == 1
        # No exception propagated


def test_main_multiple_calls():
    """Test that main can be called multiple times."""
    with patch('tail_cw.__main__.LogTailApp') as mock_app:
        mock_instance = mock_app.return_value
        mock_instance.run.return_value = None

        # Call main multiple times
        result1 = tail_cw.__main__.main()
        result2 = tail_cw.__main__.main()

        # Both should succeed
        assert result1 == 0
        assert result2 == 0

        # Should be called twice
        assert mock_app.call_count == 2
