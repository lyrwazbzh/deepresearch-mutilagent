from __future__ import annotations

import time

from src.agent.config import Settings
from src.agent.container import build_container
from src.agent.backends.events import EventRetentionCleaner, RedisEventBroker
from src.agent.backends.persistence.database import create_session_factory
from src.agent.workers.celery_app import celery_app
from src.agent.workers.outbox import CeleryJobPublisher, OutboxDispatcher
from src.agent.workers.recovery import RuntimeRecoveryScanner


def main() -> None:
    settings = Settings.from_env()
    sessions = create_session_factory(settings.database_url)
    broker = RedisEventBroker(settings.redis_url)
    container = build_container(
        settings, session_factory=sessions, event_broker=broker
    )
    dispatcher = OutboxDispatcher(
        sessions,
        CeleryJobPublisher(celery_app),
        broker,
    )
    recovery = RuntimeRecoveryScanner(sessions)
    event_retention = EventRetentionCleaner(sessions, settings.task_event_retention_days)
    last_recovery = 0.0
    last_event_retention = 0.0
    while True:
        current = time.monotonic()
        if current - last_recovery >= 5.0:
            recovery.scan_once()
            container.export_worker.recover_once()
            last_recovery = current
        if current - last_event_retention >= 3_600.0:
            event_retention.prune_once()
            last_event_retention = current
        dispatched = dispatcher.dispatch_once()
        if dispatched == 0:
            time.sleep(0.5)


if __name__ == "__main__":
    main()
