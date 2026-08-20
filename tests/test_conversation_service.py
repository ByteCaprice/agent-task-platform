from __future__ import annotations

from conftest import make_store

from domain import AgentSpec
from framework.registry import AgentRegistry
from interfaces.schemas import RunSubmitRequest
from orchestration import RunService


def _service(tmp_path):
    store = make_store(tmp_path / "runs.db")
    registry = AgentRegistry(
        [
            AgentSpec(name="echo", version="1.0.0", route_tags=["echo.test"], runtime={"type": "echo"}),
        ]
    )
    return RunService(store, registry), store


def test_submit_creates_conversation_and_links_run(tmp_path) -> None:
    service, store = _service(tmp_path)

    run, is_new = service.submit(
        RunSubmitRequest(
            route_tag="echo.test",
            request_id="key-1",
            external_id="case-1",
            caller="tester",
            task_type="review",
            source="example",
        )
    )

    assert is_new
    assert run.conversation_id is not None
    conversation = store.conversations.get(run.conversation_id)
    assert conversation.external_id == "case-1"
    assert conversation.source == "example"


def test_same_external_id_groups_multiple_runs_in_one_conversation(tmp_path) -> None:
    service, store = _service(tmp_path)

    first, _ = service.submit(
        RunSubmitRequest(
            route_tag="echo.test",
            request_id="key-1",
            external_id="case-1",
            caller="tester",
        )
    )
    # The same external id with a different request id creates a new Run in one conversation.
    second, is_new = service.submit(
        RunSubmitRequest(
            route_tag="echo.test",
            request_id="key-2",
            external_id="case-1",
            caller="tester",
        )
    )

    assert is_new
    assert second.run_id != first.run_id
    assert second.conversation_id == first.conversation_id
    assert store.conversations.get_by_external_id("tester", "case-1").conversation_id == first.conversation_id


def test_request_id_replays_same_run(tmp_path) -> None:
    service, _store = _service(tmp_path)

    first, is_new1 = service.submit(
        RunSubmitRequest(
            route_tag="echo.test",
            request_id="dup-key",
            external_id="case-2",
            caller="tester",
        )
    )
    replay, is_new2 = service.submit(
        RunSubmitRequest(
            route_tag="echo.test",
            request_id="dup-key",
            external_id="case-2",
            caller="tester",
        )
    )

    assert is_new1 is True
    assert is_new2 is False
    assert replay.run_id == first.run_id


def test_external_id_falls_back_to_request_id(tmp_path) -> None:
    service, store = _service(tmp_path)

    run, _ = service.submit(
        RunSubmitRequest(
            route_tag="echo.test",
            request_id="without-external-id",
            caller="tester",
        )
    )

    conversation = store.conversations.get(run.conversation_id)
    assert conversation.external_id == "without-external-id"
