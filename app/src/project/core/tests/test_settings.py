import logging
from collections.abc import Generator
from datetime import timedelta
from importlib import import_module

import pytest
import sentry_sdk
import structlog
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.types import Event

from ...settings import _drop_structlog_duplicates


@pytest.fixture
def sentry_events(settings) -> Generator[list[Event]]:
    events: list[Event] = []
    client = sentry_sdk.Client(
        dsn="https://public@sentry.invalid/0",
        transport=events.append,
        default_integrations=False,
        integrations=[LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)],
        before_send=_drop_structlog_duplicates,  # type: ignore[arg-type]
        before_breadcrumb=_drop_structlog_duplicates,
    )
    processor = settings.LOGGING_SENTRY_PROCESSOR
    was_active = processor.active
    processor.active = True
    try:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(client)
            yield events
    finally:
        processor.active = was_active


def test__settings__celery_beat_schedule(settings):
    """Ensure that CELERY_BEAT_SCHEDULE points to existing tasks"""

    if not hasattr(settings, "CELERY_BEAT_SCHEDULE"):
        pytest.skip("CELERY_BEAT_SCHEDULE is not defined")

    paths = {task["task"] for task in settings.CELERY_BEAT_SCHEDULE.values()}
    for path in paths:
        module_path, task_name = path.rsplit(".", maxsplit=1)
        try:
            module = import_module(module_path)
        except ImportError:
            pytest.fail(f"The module '{module_path}' does not exist")

        if not hasattr(module, task_name):
            pytest.fail(f"The task '{task_name}' does not exist in {module_path}")


def test__settings__celery_beat_entries_expire_before_the_next_one_is_due(settings):
    """Every periodic tick must be discarded once its successor is due.

    Without `expires`, a worker that stops consuming turns the broker into a
    replay buffer: on 2026-09-16 a restarted worker found ~9 days of queued
    ticks (1006 messages) and ran them back to back, pinning the database disk
    at 90% for a day. An expired tick is dropped instead, so a recovered
    worker does at most one run per entry.
    """
    assert settings.CELERY_BEAT_SCHEDULE, "no beat entries to check"
    for name, entry in settings.CELERY_BEAT_SCHEDULE.items():
        expires = entry.get("options", {}).get("expires")
        assert expires is not None, f"beat entry {name!r} has no options.expires"

        schedule = entry["schedule"]
        # crontab() schedules carry no interval; every one here is daily.
        interval_seconds = schedule.total_seconds() if isinstance(schedule, timedelta) else 24 * 60 * 60
        assert 0 < expires < interval_seconds, (
            f"beat entry {name!r} expires after {expires}s, which is not inside its {interval_seconds}s interval"
        )


def test__settings__structlog_exceptions_reach_sentry_with_stacktrace(sentry_events):
    logger = structlog.get_logger("test")

    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("task failed")

    assert [
        (value["type"], value["value"], bool(value["stacktrace"]["frames"]))
        for event in sentry_events
        for value in event["exception"]["values"]
    ] == [("ValueError", "boom", True)]
