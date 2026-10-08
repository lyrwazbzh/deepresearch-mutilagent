from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any

import httpx
from sqlalchemy import func, select

from src.agent.config import Settings
from src.agent.backends.persistence.database import create_session_factory
from src.agent.backends.persistence.models import (
    ConversationMessageRow,
    NodeRunRow,
    OutboxMessageRow,
    ResearchCheckpointRow,
    ResearchEventRow,
    ResearchTaskRow,
)


BASE_URL = os.getenv("PHASE10_ACCEPTANCE_URL", "http://127.0.0.1:8000")
ORIGIN = os.getenv("PHASE10_ACCEPTANCE_ORIGIN", "http://localhost:3000")
EMAIL = os.environ["PHASE10_ACCEPTANCE_EMAIL"]
PASSWORD = os.environ["PHASE10_ACCEPTANCE_PASSWORD"]
OTHER_EMAIL = os.environ["PHASE10_ACCEPTANCE_OTHER_EMAIL"]


def emit(section: str, **payload: Any) -> None:
    print(json.dumps({"section": section, **payload}, sort_keys=True), flush=True)


def login(email: str) -> tuple[str, str, dict[str, str]]:
    response = httpx.post(
        f"{BASE_URL}/api/v1/auth/login",
        json={"email": email, "password": PASSWORD, "tenant_id": "tenant_dev"},
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    refresh = response.cookies.get("dr_refresh")
    if not refresh:
        raise RuntimeError("Login did not issue the refresh cookie.")
    return str(body["access_token"]), refresh, dict(response.headers)


def auth_headers(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


def max_event_id(session_factory: Any, task_id: str) -> int:
    with session_factory() as session:
        return int(
            session.scalar(
                select(func.max(ResearchEventRow.event_id)).where(
                    ResearchEventRow.task_id == task_id
                )
            )
            or 0
        )


def read_sse_until(
    task_id: str,
    token: str,
    *,
    after_id: int,
    target_id: int,
    connected: Event | None = None,
) -> list[dict[str, Any]]:
    headers = auth_headers(token, **{"Last-Event-ID": str(after_id)})
    events: list[dict[str, Any]] = []
    timeout = httpx.Timeout(connect=10, read=90, write=10, pool=10)
    with httpx.stream(
        "GET",
        f"{BASE_URL}/api/v1/tasks/{task_id}/events",
        headers=headers,
        timeout=timeout,
    ) as response:
        response.raise_for_status()
        if connected is not None:
            connected.set()
        frame: dict[str, str] = {}
        for line in response.iter_lines():
            if line == "":
                if "id" in frame and "event" in frame:
                    event_id = int(frame["id"])
                    events.append(
                        {
                            "id": event_id,
                            "event": frame["event"],
                            "data": json.loads(frame.get("data", "{}")),
                        }
                    )
                    if event_id >= target_id:
                        return events
                frame = {}
                continue
            if line.startswith(":") or ":" not in line:
                continue
            name, value = line.split(":", 1)
            frame[name] = value.lstrip()
    raise RuntimeError("SSE stream ended before the expected durable event.")


def create_task(token: str, key: str, query: str) -> dict[str, Any]:
    response = httpx.post(
        f"{BASE_URL}/api/v1/tasks",
        headers=auth_headers(token, **{"Idempotency-Key": key}),
        json={
            "query": query,
            "options": {"max_depth": 1, "max_revision_rounds": 1},
        },
        timeout=30,
    )
    response.raise_for_status()
    return dict(response.json())


def command(token: str, task_id: str, action: str, key: str) -> httpx.Response:
    return httpx.post(
        f"{BASE_URL}/api/v1/tasks/{task_id}/{action}",
        headers=auth_headers(token, **{"Idempotency-Key": key}),
        timeout=30,
    )


def wait_terminal(token: str, task_id: str, timeout: float = 120) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = httpx.get(
            f"{BASE_URL}/api/v1/tasks/{task_id}",
            headers=auth_headers(token),
            timeout=30,
        )
        response.raise_for_status()
        last = dict(response.json())
        if last["task_status"] in {"completed", "cancelled", "failed"}:
            return last
        time.sleep(0.25)
    raise TimeoutError(f"Task did not reach a terminal state: {last}")


def main() -> None:
    settings = Settings.from_env()
    sessions = create_session_factory(settings.database_url)
    token, refresh, login_headers = login(EMAIL)
    other_token, _other_refresh, _other_headers = login(OTHER_EMAIL)
    cookie_header = login_headers.get("set-cookie", "")
    emit(
        "auth.login",
        access=True,
        refresh_not_in_body=True,
        cookie_http_only="HttpOnly" in cookie_header,
        cookie_secure="Secure" in cookie_header,
        cookie_path="Path=/api/v1/auth" in cookie_header,
    )

    def rotate() -> tuple[int, str | None]:
        response = httpx.post(
            f"{BASE_URL}/api/v1/auth/refresh",
            headers={"Origin": ORIGIN, "Cookie": f"dr_refresh={refresh}"},
            timeout=30,
        )
        return response.status_code, response.cookies.get("dr_refresh")

    with ThreadPoolExecutor(max_workers=2) as executor:
        rotations = list(executor.map(lambda _index: rotate(), range(2)))
    statuses = sorted(status for status, _cookie in rotations)
    if statuses != [200, 401]:
        raise RuntimeError(f"Refresh CAS did not have exactly one winner: {statuses}")
    new_refresh = next(cookie for status, cookie in rotations if status == 200)
    assert new_refresh
    old_replay = httpx.post(
        f"{BASE_URL}/api/v1/auth/refresh",
        headers={"Origin": ORIGIN, "Cookie": f"dr_refresh={refresh}"},
        timeout=30,
    )
    emit(
        "auth.refresh",
        concurrent_statuses=statuses,
        old_refresh_replay=old_replay.status_code,
    )

    key = f"phase10-real-{time.time_ns()}"

    def create_concurrently(_index: int) -> tuple[int, dict[str, Any]]:
        response = httpx.post(
            f"{BASE_URL}/api/v1/tasks",
            headers=auth_headers(
                token, **{"Idempotency-Key": f"{key}-concurrent-create"}
            ),
            json={"query": "Concurrent HTTP idempotency acceptance."},
            timeout=30,
        )
        response.raise_for_status()
        return response.status_code, dict(response.json())

    with ThreadPoolExecutor(max_workers=2) as executor:
        concurrent_creates = list(executor.map(create_concurrently, range(2)))
    create_statuses = sorted(item[0] for item in concurrent_creates)
    create_task_ids = {str(item[1]["task_id"]) for item in concurrent_creates}
    create_message_ids = {str(item[1]["message_id"]) for item in concurrent_creates}
    if create_statuses != [200, 201] or len(create_task_ids) != 1 or len(create_message_ids) != 1:
        raise RuntimeError(
            "Concurrent CreateTask idempotency did not converge to one aggregate: "
            f"{create_statuses}, {create_task_ids}, {create_message_ids}"
        )
    concurrent_task_id = next(iter(create_task_ids))
    stop_concurrent = command(
        token, concurrent_task_id, "cancel", f"{key}-concurrent-cancel"
    )
    if stop_concurrent.status_code not in {202, 409}:
        stop_concurrent.raise_for_status()
    emit(
        "http.idempotency.concurrent",
        statuses=create_statuses,
        task_ids=sorted(create_task_ids),
        message_ids=sorted(create_message_ids),
    )

    created = create_task(
        token,
        key,
        "Answer in one short sentence: confirm the real Phase 10 HTTP runtime path.",
    )
    task_id = str(created["task_id"])
    replay = create_task(
        token,
        key,
        "Answer in one short sentence: confirm the real Phase 10 HTTP runtime path.",
    )
    if replay["task_id"] != task_id or replay["message_id"] != created["message_id"]:
        raise RuntimeError("CreateTask idempotent HTTP replay changed the persisted result.")
    unauthorized = httpx.get(
        f"{BASE_URL}/api/v1/tasks/{task_id}",
        headers=auth_headers(other_token),
        timeout=30,
    )
    if unauthorized.status_code != 404:
        raise RuntimeError(f"Cross-user Task read returned {unauthorized.status_code}.")
    terminal = wait_terminal(token, task_id)
    if terminal["task_status"] != "completed":
        raise RuntimeError(f"Real HTTP ResearchTask did not complete: {terminal}")
    latest = max_event_id(sessions, task_id)
    completion_events = read_sse_until(
        task_id, token, after_id=0, target_id=latest
    )
    with sessions() as session:
        task = session.get(ResearchTaskRow, task_id)
        facts = {
            "messages": session.scalar(
                select(func.count()).select_from(ConversationMessageRow).where(
                    ConversationMessageRow.task_id == task_id
                )
            ),
            "checkpoints": session.scalar(
                select(func.count()).select_from(ResearchCheckpointRow).where(
                    ResearchCheckpointRow.task_id == task_id
                )
            ),
            "node_runs": session.scalar(
                select(func.count()).select_from(NodeRunRow).where(
                    NodeRunRow.task_id == task_id
                )
            ),
            "events": session.scalar(
                select(func.count()).select_from(ResearchEventRow).where(
                    ResearchEventRow.task_id == task_id
                )
            ),
            "schedule_dispatched": session.scalar(
                select(OutboxMessageRow.dispatched_at).where(
                    OutboxMessageRow.aggregate_id == task_id,
                    OutboxMessageRow.kind == "research.schedule",
                )
            )
            is not None,
        }
        assert task is not None
        provider_revisions = sorted(
            {
                revision["primary"]["model_config_id"]
                for revision in task.model_binding_snapshot["bindings"].values()
            }
        )
    emit(
        "http.runtime.sse",
        task_id=task_id,
        terminal_status=terminal["task_status"],
        sse_event_ids=[item["id"] for item in completion_events],
        last_event_id=latest,
        cross_user_status=unauthorized.status_code,
        model_revisions=provider_revisions,
        **facts,
    )

    replay_task = create_task(token, f"{key}-replay", "Pause for SSE replay acceptance.")
    replay_task_id = str(replay_task["task_id"])
    paused = command(token, replay_task_id, "pause", f"{key}-pause")
    paused.raise_for_status()
    first_boundary = max_event_id(sessions, replay_task_id)
    first_delivery = read_sse_until(
        replay_task_id, token, after_id=0, target_id=first_boundary
    )
    first_seen = first_delivery[-1]["id"]
    cancelled = command(token, replay_task_id, "cancel", f"{key}-cancel")
    cancelled.raise_for_status()
    replay_boundary = max_event_id(sessions, replay_task_id)
    replayed_events = read_sse_until(
        replay_task_id,
        token,
        after_id=first_seen,
        target_id=replay_boundary,
    )
    emit(
        "sse.reconnect",
        task_id=replay_task_id,
        disconnected_after=first_seen,
        generated_while_disconnected=replay_boundary,
        replayed_ids=[item["id"] for item in replayed_events],
    )

    live_task = create_task(token, f"{key}-live", "Pause for SSE live acceptance.")
    live_task_id = str(live_task["task_id"])
    pause_live = command(token, live_task_id, "pause", f"{key}-live-pause")
    pause_live.raise_for_status()
    live_boundary = max_event_id(sessions, live_task_id)
    connected = Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            read_sse_until,
            live_task_id,
            token,
            after_id=live_boundary,
            target_id=live_boundary + 1,
            connected=connected,
        )
        if not connected.wait(10):
            raise TimeoutError("SSE live connection did not establish.")
        time.sleep(0.5)
        cancel_live = command(token, live_task_id, "cancel", f"{key}-live-cancel")
        cancel_live.raise_for_status()
        live_events = future.result(timeout=30)
    emit(
        "sse.live",
        task_id=live_task_id,
        after_id=live_boundary,
        live_ids=[item["id"] for item in live_events],
    )

    logout = httpx.post(
        f"{BASE_URL}/api/v1/auth/logout",
        headers={"Origin": ORIGIN, "Cookie": f"dr_refresh={new_refresh}"},
        timeout=30,
    )
    rejected_after_logout = httpx.post(
        f"{BASE_URL}/api/v1/auth/refresh",
        headers={"Origin": ORIGIN, "Cookie": f"dr_refresh={new_refresh}"},
        timeout=30,
    )
    emit(
        "auth.logout",
        logout_status=logout.status_code,
        refresh_after_logout=rejected_after_logout.status_code,
    )


if __name__ == "__main__":
    main()
