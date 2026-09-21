from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from lib.dag_scheduler import DAGScheduler
from lib.message_hub import (
    AGENT_COMMAND_TOPIC,
    AGENT_MESSAGE_TOPIC,
    InMemoryMessageBus,
    build_envelope,
    route_envelope,
    RocketMQMessageBus,
    _Subscription,
)


def test_short_poll_keeps_one_pending_receive() -> None:
    calls = []

    def receive(*args):
        calls.append(args)
        time.sleep(0.3)
        return []

    async def scenario():
        bus = RocketMQMessageBus(None)
        sub = _Subscription("lead", AGENT_MESSAGE_TOPIC,
                            SimpleNamespace(receive=receive), None, True, 1)
        bus._subscriptions["lead"] = [sub]
        started = time.monotonic()
        for _ in range(5):
            assert await bus.receive("lead", timeout=0.01) == []
        assert time.monotonic() - started < 0.2
        assert len(calls) == 1
        await asyncio.sleep(0.35)
        assert await bus.receive("lead", timeout=0.01) == []
        assert len(calls) == 1

    asyncio.run(scenario())


def test_execution_context_must_be_complete() -> None:
    with pytest.raises(ValueError, match="provided together"):
        build_envelope(
            sender="runtime-1",
            target="lead",
            event_type="execution.completed",
            payload={},
            execution_id="execution-1",
        )


def test_task_available_uses_normal_topic_and_task_key() -> None:
    envelope = build_envelope(
        sender="scheduler",
        target=None,
        event_type="task_available",
        payload={"task_type": "code.implementation", "work_shard": 4},
        thread_id="thread-1",
        task_id="task-1",
    )

    topic, tag, key, lite_topic = route_envelope(envelope)

    assert topic == AGENT_COMMAND_TOPIC
    assert tag == "task_available"
    assert key == "task-1"
    assert lite_topic is None


def test_directed_control_uses_normal_topic() -> None:
    envelope = build_envelope(
        sender="lead",
        target="runtime-1",
        event_type="execution_cancel",
        payload={"reason": "cancel"},
    )

    topic, tag, key, lite_topic = route_envelope(envelope)

    assert topic == AGENT_COMMAND_TOPIC
    assert tag == "execution_cancel"
    assert key == "runtime-1"
    assert lite_topic is None


def test_normal_message_uses_agent_message_topic() -> None:
    envelope = build_envelope(
        sender="lead",
        target="runtime-1",
        event_type="message",
        payload={"text": "status"},
    )

    topic, tag, key, lite_topic = route_envelope(envelope)

    assert topic == AGENT_MESSAGE_TOPIC
    assert tag == "message"
    assert key == "runtime-1"
    assert lite_topic is None


def test_in_memory_bus_retries_then_acks_once() -> None:
    async def scenario() -> None:
        bus = InMemoryMessageBus()
        event_id = await bus.send(
            sender="lead",
            target="runtime-1",
            event_type="message",
            payload={"text": "hello"},
        )

        first_delivery = await bus.receive("runtime-1")
        assert [message.event_id for message in first_delivery] == [event_id]
        await bus.retry(event_id, "temporary failure")

        second_delivery = await bus.receive("runtime-1")
        assert [message.event_id for message in second_delivery] == [event_id]
        await bus.ack(event_id)

        assert event_id in bus.acked
        assert bus.retried == [(event_id, "temporary failure")]

    asyncio.run(scenario())


def test_task_shards_are_stable_and_bounded() -> None:
    scheduler = DAGScheduler(pool=None, work_shard_count=64)

    first = scheduler.work_shard_for("task-42", "code.review")
    second = scheduler.work_shard_for("task-42", "code.review")

    assert first == second
    assert 0 <= first < 64
