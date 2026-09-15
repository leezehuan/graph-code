"""Message bus, PostgreSQL outbox, and RocketMQ transport primitives.

PostgreSQL remains the source of truth. ``send`` only writes a durable outbox
record; ``OutboxDispatcher`` delivers those records to RocketMQ asynchronously.
The legacy ``agent_messages`` table is intentionally not written here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

AGENT_COMMAND_TOPIC = "agent-command"
TASK_EVENT_TOPIC = "task-event"
PERMISSION_EVENT_TOPIC = "permission-event"
AGENT_MESSAGE_TOPIC = "agent-message"
TASK_READY_LITE_TOPIC_PREFIX = "ready"
RUNTIME_CONTROL_LITE_TOPIC_PREFIX = "runtime"
_V5_IMPORT_LOCK = threading.Lock()


def _v5_client_types() -> tuple[Any, Any, Any, Any, Any, Any]:
    """Load the V5 SDK lazily so outbox-only processes need no MQ client."""
    # Version 5.1.1 configures a file logger at import time under
    # ~/logs/rocketmq_python. Redirect only that import to a configurable,
    # writable root, then restore HOME before application code runs.
    with _V5_IMPORT_LOCK:
        original_home = os.environ.get("HOME")
        log_home = os.getenv(
            "ROCKETMQ_CLIENT_HOME",
            "/tmp/langcode-rocketmq-client",
        )
        if "rocketmq.v5.log.log_config" not in sys.modules:
            os.makedirs(log_home, exist_ok=True)
            os.environ["HOME"] = log_home
        try:
            from rocketmq.v5.client import ClientConfiguration, Credentials
            from rocketmq.v5.consumer import SimpleConsumer
            from rocketmq.v5.model import FilterExpression, Message
            from rocketmq.v5.producer import Producer
        finally:
            if original_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = original_home

    return (
        ClientConfiguration,
        Credentials,
        FilterExpression,
        SimpleConsumer,
        Producer,
        Message,
    )


def configured_task_ready_lite_topics() -> tuple[str, ...]:
    """Return the configured task-ready LiteTopics for a generic Runtime."""
    task_types = tuple(
        task_type.strip()
        for task_type in os.getenv("TASK_WORK_TASK_TYPES", "general").split(",")
        if task_type.strip()
    )
    shard_count = int(os.getenv("TASK_WORK_SHARD_COUNT", "64"))
    if shard_count < 1:
        raise ValueError("TASK_WORK_SHARD_COUNT must be >= 1")
    return tuple(
        task_ready_lite_topic(task_type, shard)
        for task_type in task_types
        for shard in range(shard_count)
    )


@dataclass(frozen=True)
class MessageEnvelope:
    """Versioned application-level message carried by RocketMQ."""

    event_id: str
    event_type: str
    sender: str
    target: str | None
    payload: dict[str, Any]
    thread_id: str | None = None
    task_id: str | None = None
    execution_id: str | None = None
    attempt: int | None = None
    request_id: str | None = None
    correlation_id: str | None = None
    created_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "thread_id": self.thread_id,
            "task_id": self.task_id,
            "execution_id": self.execution_id,
            "attempt": self.attempt,
            "sender": self.sender,
            "target": self.target,
            "request_id": self.request_id,
            "correlation_id": self.correlation_id,
            "payload": self.payload,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MessageEnvelope":
        return cls(
            event_id=str(data["event_id"]),
            event_type=str(data["event_type"]),
            sender=str(data["sender"]),
            target=data.get("target"),
            payload=dict(data.get("payload") or {}),
            thread_id=data.get("thread_id"),
            task_id=data.get("task_id"),
            execution_id=data.get("execution_id"),
            attempt=data.get("attempt"),
            request_id=data.get("request_id"),
            correlation_id=data.get("correlation_id"),
            created_at=data.get("created_at"),
        )


class MessageBus(Protocol):
    """Application message bus contract used by lead agents and runtimes."""

    async def send(
        self,
        *,
        sender: str,
        target: str | None,
        event_type: str,
        payload: dict[str, Any],
        thread_id: str | None = None,
        task_id: str | None = None,
        execution_id: str | None = None,
        attempt: int | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> str:
        """Persist a message for asynchronous delivery and return its event id."""

    async def receive(
        self,
        consumer: str,
        timeout: float = 5,
    ) -> list[MessageEnvelope]:
        """Receive one or more messages for a configured consumer."""

    async def ack(self, event_id: str) -> None:
        """Acknowledge a successfully handled message."""

    async def retry(self, event_id: str, reason: str) -> None:
        """Request redelivery of a failed message."""


def _validate_execution_context(
    execution_id: str | None,
    attempt: int | None,
) -> None:
    if (execution_id is None) != (attempt is None):
        raise ValueError("execution_id and attempt must be provided together")
    if attempt is not None and attempt < 1:
        raise ValueError("attempt must be >= 1")


def build_envelope(
    *,
    sender: str,
    target: str | None,
    event_type: str,
    payload: dict[str, Any],
    thread_id: str | None = None,
    task_id: str | None = None,
    execution_id: str | None = None,
    attempt: int | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
    event_id: str | None = None,
) -> MessageEnvelope:
    """Build and validate a transport-independent message envelope."""
    if not sender:
        raise ValueError("sender is required")
    if not event_type:
        raise ValueError("event_type is required")
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")
    _validate_execution_context(execution_id, attempt)
    return MessageEnvelope(
        event_id=event_id or str(uuid4()),
        event_type=event_type,
        sender=sender,
        target=target,
        payload=payload,
        thread_id=thread_id,
        task_id=task_id,
        execution_id=execution_id,
        attempt=attempt,
        request_id=request_id,
        correlation_id=correlation_id or str(uuid4()),
        created_at=datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
    )


def task_ready_lite_topic(task_type: str, work_shard: int) -> str:
    """Return the stable LiteTopic for one ready-task shard."""
    if not task_type:
        raise ValueError("task_available payload.task_type is required")
    if work_shard < 0:
        raise ValueError("task_available payload.work_shard must be >= 0")
    return f"{TASK_READY_LITE_TOPIC_PREFIX}.{task_type}.s{work_shard}"


def runtime_control_lite_topic(runtime_id: str) -> str:
    """Return the directed control LiteTopic for one Runtime."""
    if not runtime_id:
        raise ValueError("directed control messages require target runtime_id")
    return f"{RUNTIME_CONTROL_LITE_TOPIC_PREFIX}.{runtime_id}"


def route_envelope(
    envelope: MessageEnvelope,
) -> tuple[str, str, str | None, str | None]:
    """Choose the RocketMQ topic, tag, key, and optional LiteTopic."""
    event_type = envelope.event_type
    lite_topic: str | None = None

    if event_type == "task_available":
        topic = AGENT_COMMAND_TOPIC
        tag = "task_available"
        try:
            task_type = str(envelope.payload["task_type"])
            work_shard = int(envelope.payload["work_shard"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "task_available requires payload.task_type and payload.work_shard"
            ) from exc
        lite_topic = task_ready_lite_topic(task_type, work_shard)
    elif event_type in {"permission_response", "execution_cancel"}:
        topic = AGENT_COMMAND_TOPIC
        tag = event_type
        lite_topic = runtime_control_lite_topic(envelope.target or "")
    elif event_type == "runtime_wakeup":
        topic = AGENT_COMMAND_TOPIC
        tag = event_type
        lite_topic = runtime_control_lite_topic(envelope.target or "")
    elif event_type.startswith("permission."):
        topic = PERMISSION_EVENT_TOPIC
        tag = event_type
    elif event_type.startswith(("task.", "execution.", "card.")):
        topic = TASK_EVENT_TOPIC
        tag = event_type
    else:
        topic = AGENT_MESSAGE_TOPIC
        tag = event_type

    if lite_topic and lite_topic.startswith(f"{RUNTIME_CONTROL_LITE_TOPIC_PREFIX}."):
        message_key = envelope.target
    else:
        message_key = (
            envelope.task_id
            or envelope.execution_id
            or envelope.request_id
            or envelope.target
            or envelope.event_id
        )
    return topic, tag, message_key, lite_topic


async def enqueue_outbox_event(conn: Any, envelope: MessageEnvelope) -> str:
    """Insert a message into ``message_outbox`` inside the caller's transaction."""
    topic, tag, message_key, lite_topic = route_envelope(envelope)
    await conn.execute(
        """
        INSERT INTO message_outbox
            (event_id, topic, tag, message_key, lite_topic, envelope, status,
             next_retry_at)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb, 'pending', NOW())
        """,
        [
            envelope.event_id,
            topic,
            tag,
            message_key,
            lite_topic,
            json.dumps(envelope.as_dict()),
        ],
    )
    return envelope.event_id


def _row_to_dict(cursor: Any, row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    return dict(zip([desc.name for desc in cursor.description], row))


class PostgresOutboxMessageBus:
    """Message bus writer backed by PostgreSQL's transactional Outbox."""

    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    async def setup(self) -> None:
        """Schema is initialized by ``DAGScheduler.setup`` from ``schema.sql``."""

    async def send(
        self,
        *,
        sender: str,
        target: str | None,
        event_type: str,
        payload: dict[str, Any],
        thread_id: str | None = None,
        task_id: str | None = None,
        execution_id: str | None = None,
        attempt: int | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> str:
        envelope = build_envelope(
            sender=sender,
            target=target,
            event_type=event_type,
            payload=payload,
            thread_id=thread_id,
            task_id=task_id,
            execution_id=execution_id,
            attempt=attempt,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await enqueue_outbox_event(conn, envelope)
        return envelope.event_id

    async def receive(
        self,
        consumer: str,
        timeout: float = 5,
    ) -> list[MessageEnvelope]:
        raise RuntimeError(
            "PostgresOutboxMessageBus only writes the Outbox; use "
            "RocketMQMessageBus in processes that consume messages."
        )

    async def ack(self, event_id: str) -> None:
        raise RuntimeError("PostgresOutboxMessageBus cannot acknowledge messages")

    async def retry(self, event_id: str, reason: str) -> None:
        raise RuntimeError("PostgresOutboxMessageBus cannot retry messages")

    async def create_permission_request(
        self,
        *,
        request_id: str,
        agent_name: str,
        tool_name: str,
        command: str,
        thread_id: str | None = None,
        task_id: str | None = None,
        execution_id: str | None = None,
        attempt: int | None = None,
        correlation_id: str | None = None,
    ) -> str:
        """Audit a permission request and enqueue its event atomically."""
        _validate_execution_context(execution_id, attempt)
        envelope = build_envelope(
            sender=agent_name,
            target="lead",
            event_type="permission.requested",
            payload={
                "request_id": request_id,
                "tool": tool_name,
                "command": command,
            },
            thread_id=thread_id,
            task_id=task_id,
            execution_id=execution_id,
            attempt=attempt,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO permission_audit_log
                        (request_id, agent_name, tool_name, command, thread_id,
                         task_id, execution_id, attempt, event_id, correlation_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        request_id,
                        agent_name,
                        tool_name,
                        command,
                        thread_id,
                        task_id,
                        execution_id,
                        attempt,
                        envelope.event_id,
                        envelope.correlation_id,
                    ],
                )
                await enqueue_outbox_event(conn, envelope)
        return envelope.event_id

    async def decide_permission(
        self,
        *,
        request_id: str,
        decision: str,
        reason: str | None,
        decided_by: str,
        sender: str = "lead",
    ) -> str | None:
        """Persist a permission decision and send a directed response atomically."""
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")

        async with self.pool.connection() as conn:
            async with conn.transaction():
                cursor = await conn.execute(
                    """
                    SELECT request_id, agent_name, thread_id, task_id, execution_id,
                           attempt, correlation_id
                    FROM permission_audit_log
                    WHERE request_id = %s
                    FOR UPDATE
                    """,
                    [request_id],
                )
                request = _row_to_dict(cursor, await cursor.fetchone())
                if request is None:
                    return None

                envelope = build_envelope(
                    sender=sender,
                    target=request["agent_name"],
                    event_type="permission_response",
                    payload={
                        "request_id": request_id,
                        "decision": decision,
                        "reason": reason,
                    },
                    thread_id=request["thread_id"],
                    task_id=request["task_id"],
                    execution_id=(
                        str(request["execution_id"])
                        if request["execution_id"] is not None
                        else None
                    ),
                    attempt=request["attempt"],
                    request_id=request_id,
                    correlation_id=(
                        str(request["correlation_id"])
                        if request["correlation_id"] is not None
                        else None
                    ),
                )
                await conn.execute(
                    """
                    UPDATE permission_audit_log
                    SET decision = %s, reason = %s, decided_by = %s,
                        decided_at = NOW(), event_id = %s
                    WHERE request_id = %s
                    """,
                    [decision, reason, decided_by, envelope.event_id, request_id],
                )
                await enqueue_outbox_event(conn, envelope)
                return envelope.event_id

    async def get_pending_permissions(self) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT request_id, agent_name, tool_name, command, thread_id,
                       task_id, execution_id, attempt, created_at
                FROM permission_audit_log
                WHERE decision IS NULL
                ORDER BY created_at ASC
                """
            )
            rows = await cursor.fetchall()
            return [_row_to_dict(cursor, row) for row in rows]

    async def get_permission_request(self, request_id: str) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT request_id, agent_name, tool_name, command, thread_id,
                       task_id, execution_id, attempt, created_at
                FROM permission_audit_log
                WHERE request_id = %s
                """,
                [request_id],
            )
            return _row_to_dict(cursor, await cursor.fetchone())


@dataclass
class _Receipt:
    consumer: str
    envelope: MessageEnvelope
    broker_message: Any
    subscription: "_Subscription"


@dataclass
class _Subscription:
    consumer: str
    topic: str
    broker_consumer: Any
    target: str | None
    allow_broadcast: bool
    max_messages: int
    lite_topics: tuple[str, ...]


def _default_lite_topics(
    *,
    topic: str,
    tag_expression: str,
    target: str | None,
) -> tuple[str, ...]:
    if topic != AGENT_COMMAND_TOPIC:
        return ()
    if tag_expression == "task_available":
        return configured_task_ready_lite_topics()
    if target is not None:
        return (runtime_control_lite_topic(target),)
    return ()


def _enable_lite_simple_consumer(consumer: Any) -> None:
    """Enable Lite POP mode missing from the public 5.1.1 Python SDK surface."""
    from rocketmq.grpc_protocol import ClientType

    # The SDK defines ClientType.LITE_SIMPLE_CONSUMER but does not expose a
    # matching class. SimpleConsumer otherwise supplies exactly the POP
    # receive lifecycle we need.
    consumer._Client__client_type = ClientType.LITE_SIMPLE_CONSUMER


def _sync_lite_subscriptions(
    consumer: Any,
    lite_topics: tuple[str, ...],
) -> None:
    """Bind a Lite POP consumer to its configured LiteTopic collection."""
    from rocketmq.grpc_protocol import (
        LiteSubscriptionAction,
        SyncLiteSubscriptionRequest,
    )
    from rocketmq.v5.util import MessagingResultChecker

    request = SyncLiteSubscriptionRequest()
    request.action = LiteSubscriptionAction.COMPLETE_ADD
    request.topic.name = AGENT_COMMAND_TOPIC
    request.topic.resource_namespace = consumer.client_configuration.namespace
    request.group.name = consumer.consumer_group
    request.group.resource_namespace = consumer.client_configuration.namespace
    request.lite_topic_set.extend(lite_topics)
    response = consumer.rpc_client.sync_lite_subscription_async(
        consumer.client_configuration.rpc_endpoints,
        request,
        metadata=consumer._sign(),
        timeout=consumer.client_configuration.request_timeout,
    ).result()
    MessagingResultChecker.check(response.status)


def _ack_broker_message(subscription: _Subscription, message: Any) -> None:
    if not subscription.lite_topics:
        subscription.broker_consumer.ack(message)
        return

    from rocketmq.grpc_protocol import AckMessageEntry, AckMessageRequest
    from rocketmq.v5.util import MessagingResultChecker

    consumer = subscription.broker_consumer
    request = AckMessageRequest()
    request.group.name = consumer.consumer_group
    request.group.resource_namespace = consumer.client_configuration.namespace
    request.topic.name = message.topic
    request.topic.resource_namespace = consumer.client_configuration.namespace
    entry = AckMessageEntry()
    entry.message_id = message.message_id
    entry.receipt_handle = message.receipt_handle
    entry.lite_topic = message.lite_topic
    request.entries.append(entry)
    response = consumer.rpc_client.ack_message_async(
        message.endpoints,
        request,
        metadata=consumer._sign(),
        timeout=consumer.client_configuration.request_timeout,
    ).result()
    MessagingResultChecker.check(response.status)


def _change_broker_message_invisible_duration(
    subscription: _Subscription,
    message: Any,
    invisible_duration: int,
) -> None:
    if not subscription.lite_topics:
        subscription.broker_consumer.change_invisible_duration(
            message,
            invisible_duration,
        )
        return

    from rocketmq.grpc_protocol import ChangeInvisibleDurationRequest
    from rocketmq.v5.util import MessagingResultChecker

    consumer = subscription.broker_consumer
    request = ChangeInvisibleDurationRequest()
    request.group.name = consumer.consumer_group
    request.group.resource_namespace = consumer.client_configuration.namespace
    request.topic.name = message.topic
    request.topic.resource_namespace = consumer.client_configuration.namespace
    request.receipt_handle = message.receipt_handle
    request.invisible_duration.seconds = invisible_duration
    request.message_id = message.message_id
    request.lite_topic = message.lite_topic
    response = consumer.rpc_client.change_invisible_duration_async(
        message.endpoints,
        request,
        metadata=consumer._sign(),
        timeout=consumer.client_configuration.request_timeout,
    ).result()
    message.receipt_handle = response.receipt_handle
    MessagingResultChecker.check(response.status)


class RocketMQMessageBus(PostgresOutboxMessageBus):
    """RocketMQ V5 transport with PostgreSQL Outbox writes and POP receives.

    ``SimpleConsumer`` uses POP/shared pull. A Runtime fetches work only while
    idle, so a busy Runtime cannot hold an unclaimed ``task_available`` receipt
    that another idle Runtime should receive.
    """

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        nameserver_address: str | None = None,
        endpoints: str | None = None,
        producer_group: str | None = None,
        receipt_timeout: float = 300,
    ):
        super().__init__(pool)
        # RocketMQ V5 clients talk to the broker proxy's gRPC endpoint, not
        # the legacy NameServer endpoint. Keep nameserver_address accepted for
        # API compatibility, but do not use it as a V5 access point.
        del nameserver_address
        self.endpoints = endpoints or os.getenv(
            "ROCKETMQ_ENDPOINTS",
            os.getenv("ROCKETMQ_PROXY_GRPC_ADDR", "127.0.0.1:8081"),
        )
        self.producer_group = producer_group or os.getenv(
            "ROCKETMQ_PRODUCER_GROUP", "GID-langcode-outbox"
        )
        self.receipt_timeout = receipt_timeout
        self._producer: Any | None = None
        self._producer_lock = asyncio.Lock()
        self._receipts: dict[str, _Receipt] = {}
        self._subscriptions: dict[str, list[_Subscription]] = {}
        self._subscription_positions: dict[str, int] = {}

    async def setup(self) -> None:
        await self._ensure_producer()

    async def close(self) -> None:
        consumers = [
            subscription.broker_consumer
            for subscriptions in self._subscriptions.values()
            for subscription in subscriptions
        ]
        self._subscriptions.clear()
        self._subscription_positions.clear()
        self._receipts.clear()
        for consumer in consumers:
            try:
                await asyncio.to_thread(consumer.shutdown)
            except Exception:
                logger.exception("Failed to stop RocketMQ consumer")
        if self._producer is not None:
            producer = self._producer
            self._producer = None
            try:
                await asyncio.to_thread(producer.shutdown)
            except Exception:
                logger.exception("Failed to stop RocketMQ producer")

    async def subscribe(
        self,
        consumer: str,
        *,
        topic: str,
        tag_expression: str = "*",
        target: str | None = None,
        allow_broadcast: bool = True,
        group_id: str | None = None,
        max_messages: int = 16,
        lite_topics: tuple[str, ...] | None = None,
    ) -> None:
        """Configure a local V5 POP consumer and destination filter.

        ``allow_broadcast`` is appropriate for lifecycle event streams. It is
        disabled for directed Runtime control and normal agent inboxes.
        """
        if max_messages < 1:
            raise ValueError("max_messages must be >= 1")
        subscriptions = self._subscriptions.setdefault(consumer, [])
        if any(subscription.topic == topic for subscription in subscriptions):
            return

        try:
            (
                ClientConfiguration,
                Credentials,
                FilterExpression,
                SimpleConsumer,
                _,
                _,
            ) = _v5_client_types()
        except ImportError as exc:
            raise RuntimeError(
                "RocketMQ support requires the 'rocketmq-python-client' package. "
                "Install requirements.txt before starting a runtime."
            ) from exc

        broker_group = group_id or f"GID-langcode-{_safe_group_component(consumer)}"
        configuration = ClientConfiguration(
            self.endpoints,
            Credentials(
                os.getenv("ROCKETMQ_ACCESS_KEY", ""),
                os.getenv("ROCKETMQ_SECRET_KEY", ""),
            ),
            request_timeout=int(os.getenv("ROCKETMQ_REQUEST_TIMEOUT", "3")),
        )
        mq_consumer = SimpleConsumer(
            configuration,
            broker_group,
            {topic: FilterExpression(tag_expression)},
        await_duration=max(
            1,
            int(os.getenv("ROCKETMQ_POP_POLL_SECONDS", "1")),
        ),
        )
        resolved_lite_topics = lite_topics or _default_lite_topics(
            topic=topic,
            tag_expression=tag_expression,
            target=target,
        )
        if resolved_lite_topics:
            _enable_lite_simple_consumer(mq_consumer)
        await asyncio.to_thread(mq_consumer.startup)
        if resolved_lite_topics:
            await asyncio.to_thread(
                _sync_lite_subscriptions,
                mq_consumer,
                resolved_lite_topics,
            )
        subscriptions.append(
            _Subscription(
                consumer=consumer,
                topic=topic,
                broker_consumer=mq_consumer,
                target=target,
                allow_broadcast=allow_broadcast,
                max_messages=max_messages,
                lite_topics=resolved_lite_topics,
            )
        )

    async def receive(
        self,
        consumer: str,
        timeout: float = 5,
    ) -> list[MessageEnvelope]:
        subscriptions = self._subscriptions.get(consumer)
        if not subscriptions:
            raise RuntimeError(
                f"Consumer '{consumer}' is not subscribed. Call subscribe() first."
            )

        deadline = asyncio.get_running_loop().time() + max(0, timeout)
        while True:
            position = self._subscription_positions.get(consumer, 0)
            subscription = subscriptions[position % len(subscriptions)]
            self._subscription_positions[consumer] = (position + 1) % len(
                subscriptions
            )
            messages = await asyncio.to_thread(
                subscription.broker_consumer.receive,
                subscription.max_messages,
                max(1, int(self.receipt_timeout)),
            )
            envelopes: list[MessageEnvelope] = []
            for broker_message in messages:
                try:
                    body = broker_message.body
                    if isinstance(body, bytes):
                        body = body.decode("utf-8")
                    envelope = MessageEnvelope.from_dict(json.loads(body))
                except Exception:
                    logger.exception(
                        "Discarding malformed RocketMQ message",
                        extra={"consumer": consumer, "topic": subscription.topic},
                    )
                    await asyncio.to_thread(
                        _ack_broker_message,
                        subscription,
                        broker_message,
                    )
                    continue

                if (
                    subscription.target is not None
                    and envelope.target != subscription.target
                ):
                    if not (
                        subscription.allow_broadcast and envelope.target is None
                    ):
                        await asyncio.to_thread(
                            _ack_broker_message,
                            subscription,
                            broker_message,
                        )
                        continue

                receipt = _Receipt(
                    consumer=consumer,
                    envelope=envelope,
                    broker_message=broker_message,
                    subscription=subscription,
                )
                if await self._claim_delivery(consumer, envelope.event_id):
                    self._receipts[envelope.event_id] = receipt
                    envelopes.append(envelope)
                else:
                    await asyncio.to_thread(
                        _ack_broker_message,
                        subscription,
                        broker_message,
                    )
            if envelopes or asyncio.get_running_loop().time() >= deadline:
                return envelopes

    async def ack(self, event_id: str) -> None:
        receipt = self._get_receipt(event_id)
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE message_consumer_inbox
                    SET result = 'consumed', consumed_at = NOW()
                    WHERE consumer_name = %s AND event_id = %s
                    """,
                    [receipt.consumer, event_id],
                )
                await conn.execute(
                    """
                    INSERT INTO message_delivery_audit
                        (event_id, consumer_name, delivery_status, consumed_at)
                    VALUES (%s, %s, 'consumed', NOW())
                    """,
                    [event_id, receipt.consumer],
                )
        await asyncio.to_thread(
            _ack_broker_message,
            receipt.subscription,
            receipt.broker_message,
        )
        self._receipts.pop(event_id, None)

    async def retry(self, event_id: str, reason: str) -> None:
        receipt = self._get_receipt(event_id)
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    DELETE FROM message_consumer_inbox
                    WHERE consumer_name = %s AND event_id = %s
                      AND result = 'processing'
                    """,
                    [receipt.consumer, event_id],
                )
                await conn.execute(
                    """
                    INSERT INTO message_delivery_audit
                        (event_id, consumer_name, delivery_status, error_message)
                    VALUES (%s, %s, 'retry_requested', %s)
                    """,
                    [event_id, receipt.consumer, reason[:4000]],
                )
        retry_delay = max(
            1,
            int(os.getenv("ROCKETMQ_RETRY_INVISIBLE_SECONDS", "1")),
        )
        await asyncio.to_thread(
            _change_broker_message_invisible_duration,
            receipt.subscription,
            receipt.broker_message,
            retry_delay,
        )
        self._receipts.pop(event_id, None)

    async def publish_outbox_record(
        self,
        *,
        topic: str,
        tag: str,
        message_key: str | None,
        lite_topic: str | None,
        envelope: dict[str, Any],
    ) -> None:
        """Publish one already-committed Outbox record to RocketMQ."""
        await self._ensure_producer()
        try:
            _, _, _, _, _, Message = _v5_client_types()
        except ImportError as exc:
            raise RuntimeError("RocketMQ producer dependency is unavailable") from exc

        def publish() -> None:
            message = Message()
            message.topic = topic
            message.tag = tag
            if message_key:
                message.keys = message_key
            if lite_topic:
                message.lite_topic = lite_topic
            message.body = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
            self._producer.send(message)

        await asyncio.to_thread(publish)

    async def _ensure_producer(self) -> None:
        if self._producer is not None:
            return
        async with self._producer_lock:
            if self._producer is not None:
                return
            try:
                (
                    ClientConfiguration,
                    Credentials,
                    _,
                    _,
                    Producer,
                    _,
                ) = _v5_client_types()
            except ImportError as exc:
                raise RuntimeError(
                    "RocketMQ support requires the 'rocketmq-python-client' package. "
                    "Install requirements.txt before starting LangCode."
                ) from exc
            configuration = ClientConfiguration(
                self.endpoints,
                Credentials(
                    os.getenv("ROCKETMQ_ACCESS_KEY", ""),
                    os.getenv("ROCKETMQ_SECRET_KEY", ""),
                ),
                request_timeout=int(os.getenv("ROCKETMQ_REQUEST_TIMEOUT", "3")),
            )
            producer = Producer(
                configuration,
                topics={
                    AGENT_COMMAND_TOPIC,
                    TASK_EVENT_TOPIC,
                    PERMISSION_EVENT_TOPIC,
                    AGENT_MESSAGE_TOPIC,
                },
            )
            await asyncio.to_thread(producer.startup)
            self._producer = producer

    async def _claim_delivery(self, consumer: str, event_id: str) -> bool:
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cursor = await conn.execute(
                    """
                    INSERT INTO message_consumer_inbox (consumer_name, event_id, result)
                    VALUES (%s, %s, 'processing')
                    ON CONFLICT (consumer_name, event_id) DO NOTHING
                    RETURNING event_id
                    """,
                    [consumer, event_id],
                )
                claimed = await cursor.fetchone() is not None
                if claimed:
                    await conn.execute(
                        """
                        INSERT INTO message_delivery_audit
                            (event_id, consumer_name, delivery_status, delivered_at)
                        VALUES (%s, %s, 'processing', NOW())
                        """,
                        [event_id, consumer],
                    )
                return claimed

    def _get_receipt(self, event_id: str) -> _Receipt:
        receipt = self._receipts.get(event_id)
        if receipt is None:
            raise KeyError(f"No active receipt for event {event_id}")
        return receipt

class OutboxDispatcher:
    """Publishes pending PostgreSQL Outbox records with bounded retry backoff."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        transport: RocketMQMessageBus,
        *,
        batch_size: int = 50,
        max_retries: int | None = None,
        retry_base_seconds: float = 1,
    ):
        self.pool = pool
        self.transport = transport
        self.batch_size = batch_size
        self.max_retries = max_retries or int(
            os.getenv("MESSAGE_OUTBOX_MAX_RETRIES", "12")
        )
        self.retry_base_seconds = retry_base_seconds

    async def dispatch_once(self) -> int:
        """Publish one locked batch. Returns the number of examined records."""
        processed = 0
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cursor = await conn.execute(
                    """
                    SELECT event_id, topic, tag, message_key, lite_topic, envelope,
                           retry_count
                    FROM message_outbox
                    WHERE status = 'pending'
                      AND (next_retry_at IS NULL OR next_retry_at <= NOW())
                    ORDER BY created_at, event_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                    """,
                    [self.batch_size],
                )
                rows = await cursor.fetchall()
                for row in rows:
                    record = _row_to_dict(cursor, row)
                    processed += 1
                    try:
                        envelope = record["envelope"]
                        if isinstance(envelope, str):
                            envelope = json.loads(envelope)
                        await self.transport.publish_outbox_record(
                            topic=record["topic"],
                            tag=record["tag"],
                            message_key=record["message_key"],
                            lite_topic=record["lite_topic"],
                            envelope=envelope,
                        )
                        await conn.execute(
                            """
                            UPDATE message_outbox
                            SET status = 'published', published_at = NOW(),
                                last_error = NULL
                            WHERE event_id = %s
                            """,
                            [record["event_id"]],
                        )
                        await conn.execute(
                            """
                            INSERT INTO message_delivery_audit
                                (event_id, delivery_status, delivered_at, retry_count)
                            VALUES (%s, 'published', NOW(), %s)
                            """,
                            [record["event_id"], record["retry_count"]],
                        )
                    except Exception as exc:
                        retry_count = int(record["retry_count"]) + 1
                        terminal = retry_count >= self.max_retries
                        delay = self.retry_base_seconds * (2 ** min(retry_count, 10))
                        await conn.execute(
                            """
                            UPDATE message_outbox
                            SET status = %s, retry_count = %s,
                                next_retry_at = CASE
                                    WHEN %s THEN NULL
                                    ELSE NOW() + (%s * INTERVAL '1 second')
                                END,
                                last_error = %s
                            WHERE event_id = %s
                            """,
                            [
                                "failed" if terminal else "pending",
                                retry_count,
                                terminal,
                                delay,
                                str(exc)[:4000],
                                record["event_id"],
                            ],
                        )
                        await conn.execute(
                            """
                            INSERT INTO message_delivery_audit
                                (event_id, delivery_status, retry_count, error_message)
                            VALUES (%s, %s, %s, %s)
                            """,
                            [
                                record["event_id"],
                                "failed" if terminal else "retry_scheduled",
                                retry_count,
                                str(exc)[:4000],
                            ],
                        )
                        logger.exception(
                            "Outbox publish failed",
                            extra={"event_id": str(record["event_id"])},
                        )
        return processed

    async def run(self, *, interval: float = 0.5) -> None:
        while True:
            processed = await self.dispatch_once()
            if processed == 0:
                await asyncio.sleep(interval)


class InMemoryMessageBus:
    """Small MessageBus implementation for focused unit tests."""

    def __init__(self) -> None:
        self._messages: deque[MessageEnvelope] = deque()
        self._inflight: dict[str, MessageEnvelope] = {}
        self.acked: set[str] = set()
        self.retried: list[tuple[str, str]] = []

    async def send(
        self,
        *,
        sender: str,
        target: str | None,
        event_type: str,
        payload: dict[str, Any],
        thread_id: str | None = None,
        task_id: str | None = None,
        execution_id: str | None = None,
        attempt: int | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> str:
        envelope = build_envelope(
            sender=sender,
            target=target,
            event_type=event_type,
            payload=payload,
            thread_id=thread_id,
            task_id=task_id,
            execution_id=execution_id,
            attempt=attempt,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        self._messages.append(envelope)
        return envelope.event_id

    async def receive(
        self,
        consumer: str,
        timeout: float = 5,
    ) -> list[MessageEnvelope]:
        del timeout
        selected: list[MessageEnvelope] = []
        remaining: deque[MessageEnvelope] = deque()
        while self._messages:
            envelope = self._messages.popleft()
            if envelope.target in {None, consumer}:
                selected.append(envelope)
                self._inflight[envelope.event_id] = envelope
            else:
                remaining.append(envelope)
        self._messages = remaining
        return selected

    async def ack(self, event_id: str) -> None:
        self._inflight.pop(event_id, None)
        self.acked.add(event_id)

    async def retry(self, event_id: str, reason: str) -> None:
        envelope = self._inflight.pop(event_id)
        self.retried.append((event_id, reason))
        self._messages.appendleft(envelope)


def _safe_group_component(value: str) -> str:
    return "".join(
        char if char.isalnum() or char in {"-", "_"} else "-" for char in value
    )
