from unittest.mock import Mock
import pytest
from app.sessions import SessionManager


def test_reset_failure_preserves_tracking_and_propagates():
    manager = SessionManager()
    manager.touch('one')
    manager.checkpointer = Mock()
    manager.checkpointer.delete_thread.side_effect = RuntimeError('failed')
    with pytest.raises(RuntimeError):
        manager.reset_session('one')
    assert 'one' in manager.get_active_sessions()


def test_successful_reset_only_deletes_requested_thread():
    manager = SessionManager()
    manager.touch('one')
    manager.touch('two')
    manager.checkpointer = Mock()
    assert manager.reset_session('one') is True
    manager.checkpointer.delete_thread.assert_called_once_with('one')
    assert list(manager.get_active_sessions()) == ['two']
