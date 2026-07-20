"""RQ adapter that keeps serialized task arguments out of job descriptions."""

from __future__ import annotations

from typing import Any

import redis
from rq import Queue

from docling_jobkit.orchestrators.rq.orchestrator import (
    RQOrchestrator,
    RQOrchestratorConfig,
)


class RedactedTaskQueue(Queue):
    def enqueue(self, f: Any, *args: Any, **kwargs: Any) -> Any:
        task_id = str(kwargs.get("job_id") or "pending")
        kwargs.setdefault("description", f"docling task {task_id}")
        return super().enqueue(f, *args, **kwargs)


class DoclingServeRQOrchestrator(RQOrchestrator):
    @staticmethod
    def make_rq_queue(
        config: RQOrchestratorConfig,
    ) -> tuple[redis.Redis, Queue]:
        pool = redis.ConnectionPool.from_url(
            config.redis_url,
            max_connections=config.redis_max_connections,
            socket_timeout=config.redis_socket_timeout,
            socket_connect_timeout=config.redis_socket_connect_timeout,
        )
        connection = redis.Redis(connection_pool=pool)
        queue = RedactedTaskQueue(
            config.queue_name,
            connection=connection,
            default_timeout=14400,
            result_ttl=config.results_ttl,
            failure_ttl=config.failure_ttl,
        )
        return connection, queue
