"""refresh_validator_apy_windows is manual/emergency-only until Release B drops the MVs."""

from unittest.mock import MagicMock, patch

from apps.metagraph import tasks


def test_refresh_task_refreshes_both_views():
    cursor = MagicMock()
    cursor_ctx = MagicMock()
    cursor_ctx.__enter__.return_value = cursor

    with patch.object(tasks.connection, "cursor", return_value=cursor_ctx):
        tasks.refresh_validator_apy_windows()

    executed = " ".join(call.args[0] for call in cursor.execute.call_args_list)
    assert "mv_validator_apy_windows" in executed
    assert "mv_subnet_validator_apy_epochs" in executed
