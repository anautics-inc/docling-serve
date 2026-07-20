"""Ray-scoped legacy Office integration without module mutation."""

from __future__ import annotations

import asyncio
from typing import Any

from ray import serve

from docling_jobkit.orchestrators.ray import serve_deployment as upstream
from docling_jobkit.orchestrators.ray.models import (
    ConverterFailureResult,
    PassthroughTaskRequest,
)
from docling_jobkit.orchestrators.ray.orchestrator import RayOrchestrator

from docling_serve.execution.failure_mapping import map_public_failure
from docling_serve.execution.staged_docling_task import run_staged_task_async
from docling_serve.ingestion.canonical_task import (
    finalize_canonical_task,
    is_canonical_task,
    prepare_canonical_task,
)
from docling_serve.legacy_office import (
    LegacyOfficeError,
    LegacyOfficeSourceFetchError,
    build_converter_manager,
    check_legacy_office_capability,
)
from docling_serve.settings import docling_serve_settings
from docling_serve.upload_staging import (
    check_upload_staging_capability,
    persist_cleanup_state,
    staged_refs_for_task,
)

# Ray's @serve.deployment wrapper exposes these members dynamically; keep the
# untyped boundary at attribute lookup and type-check the owned subclass body.
_BaseConverterReplica: Any = getattr(
    upstream.DoclingProcessorConverterDeployment, "func_or_class"
)
_BaseCoordinatorReplica: Any = getattr(
    upstream.DoclingProcessorCoordinatorDeployment, "func_or_class"
)


@serve.deployment
class LegacyOfficeRayConverterDeployment(
    _BaseConverterReplica  # type: ignore[misc,valid-type]
):
    """Converter replica that changes behavior only for legacy failures."""

    def __init__(self, converter_manager_config: Any, config: Any) -> None:
        if docling_serve_settings.legacy_office_enabled:
            check_legacy_office_capability()
        if docling_serve_settings.upload_staging_mode == "required":
            check_upload_staging_capability(force=True)
        super().__init__(converter_manager_config, config)
        self.cm = build_converter_manager(converter_manager_config)
        import redis

        self._staging_redis = redis.Redis.from_url(config.redis_url)

    async def process_converter_request(self, request: Any) -> Any:
        if not isinstance(request, PassthroughTaskRequest):
            return await super().process_converter_request(request)
        original_task = request.task
        if not staged_refs_for_task(original_task):
            return await super().process_converter_request(request)

        parent_process = super().process_converter_request

        async def run_task(worker_task: Any) -> Any:
            return await parent_process(
                request.model_copy(update={"task": worker_task})
            )

        async def persist_cleanup(states: list[Any]) -> None:
            await asyncio.to_thread(
                persist_cleanup_state,
                self._staging_redis,
                task_id=original_task.task_id,
                states=states,
                ttl_seconds=self.config.results_ttl,
            )

        return await run_staged_task_async(
            original_task,
            run_task,
            on_cleanup=persist_cleanup,
        )

    async def _run_with_retry(
        self,
        task_label: str,
        func: Any,
        *,
        task: Any = None,
    ) -> Any:
        try:
            return await asyncio.to_thread(func)
        except (LegacyOfficeError, LegacyOfficeSourceFetchError) as first_legacy:
            last_error: BaseException = first_legacy
            max_retries = self.config.max_task_retries
            for attempt in range(max_retries + 1):
                error = first_legacy if attempt == 0 else last_error
                failure = map_public_failure(
                    error,
                    task_id=task.task_id if task is not None else str(task_label),
                )
                if not failure.retryable or attempt >= max_retries:
                    if task is not None:
                        return ConverterFailureResult(failure=failure)
                    raise error
                await asyncio.sleep(self.config.retry_delay)
                try:
                    return await asyncio.to_thread(func)
                except (LegacyOfficeError, LegacyOfficeSourceFetchError) as exc:
                    last_error = exc
                except Exception as nonlegacy:
                    return await self._delegate_nonlegacy_after_first_failure(
                        task_label,
                        func,
                        nonlegacy,
                        task=task,
                    )
        except Exception as first_nonlegacy:
            return await self._delegate_nonlegacy_after_first_failure(
                task_label,
                func,
                first_nonlegacy,
                task=task,
            )
        raise RuntimeError("unreachable legacy retry state")

    async def _delegate_nonlegacy_after_first_failure(
        self,
        task_label: str,
        func: Any,
        first_error: Exception,
        *,
        task: Any,
    ) -> Any:
        replayed = False

        def replay_first_then_continue() -> Any:
            nonlocal replayed
            if not replayed:
                replayed = True
                raise first_error
            return func()

        return await super()._run_with_retry(
            task_label,
            replay_first_then_continue,
            task=task,
        )


@serve.deployment
class CanonicalRayCoordinatorDeployment(
    _BaseCoordinatorReplica  # type: ignore[misc,valid-type]
):
    """Materialize and decorate canonical tasks before atomic Ray completion."""

    def __init__(
        self,
        converter_manager_config: Any,
        config: Any,
        redis_url: str,
        converter_handle: Any,
    ) -> None:
        super().__init__(
            converter_manager_config,
            config,
            redis_url,
            converter_handle,
        )
        import redis

        self._staging_redis = redis.Redis.from_url(redis_url)

    async def _process_task(self, task: Any, workdir: Any) -> Any:
        if not is_canonical_task(task):
            return await super()._process_task(task, workdir)
        original_task = task
        parent_process = super()._process_task

        async def run_task(worker_task: Any) -> Any:
            with prepare_canonical_task(worker_task) as canonical:
                result = await parent_process(
                    canonical.task if canonical is not None else worker_task,
                    workdir,
                )
                if canonical is None or isinstance(result, ConverterFailureResult):
                    return result
                return finalize_canonical_task(canonical, result)

        async def persist_cleanup(states: list[Any]) -> None:
            await asyncio.to_thread(
                persist_cleanup_state,
                self._staging_redis,
                task_id=original_task.task_id,
                states=states,
                ttl_seconds=self.config.results_ttl,
            )

        if not staged_refs_for_task(original_task):
            return await run_task(original_task)
        return await run_staged_task_async(
            original_task,
            run_task,
            on_cleanup=persist_cleanup,
        )


def create_legacy_deployment(
    converter_manager_config: Any,
    config: Any,
    redis_url: str,
    app_name: str = upstream.DEFAULT_SERVE_APP_NAME,
) -> Any:
    coordinator_target = config.coordinator_target_requests_per_replica
    coordinator_ongoing = config.coordinator_max_ongoing_requests_per_replica
    coordinator_cpus = config.coordinator_actor_num_cpus
    assert coordinator_target is not None
    assert coordinator_ongoing is not None
    assert coordinator_cpus is not None
    assert config.coordinator_min_actors is not None
    assert config.coordinator_max_actors is not None

    converter_options = upstream._build_deployment_options(
        name="converter",
        min_replicas=config.min_actors,
        max_replicas=config.max_actors,
        target_requests_per_replica=config.target_requests_per_replica,
        max_ongoing_requests=(
            config.max_ongoing_requests_per_replica
            or config.target_requests_per_replica
        ),
        num_cpus=config.converter_actor_num_cpus,
        memory_limit=config.converter_actor_memory_request,
        upscale_delay_s=config.upscale_delay_s,
        downscale_delay_s=config.downscale_delay_s,
        graceful_shutdown_wait_loop_s=config.graceful_shutdown_wait_loop_s,
        graceful_shutdown_timeout_s=config.graceful_shutdown_timeout_s,
        max_replicas_per_node=config.converter_max_replicas_per_node,
    )
    coordinator_options = upstream._build_deployment_options(
        name="coordinator",
        min_replicas=config.coordinator_min_actors,
        max_replicas=config.coordinator_max_actors,
        target_requests_per_replica=coordinator_target,
        max_ongoing_requests=coordinator_ongoing,
        num_cpus=coordinator_cpus,
        memory_limit=config.coordinator_actor_memory_request,
        upscale_delay_s=config.upscale_delay_s,
        downscale_delay_s=config.downscale_delay_s,
        graceful_shutdown_wait_loop_s=config.graceful_shutdown_wait_loop_s,
        graceful_shutdown_timeout_s=config.graceful_shutdown_timeout_s,
        max_replicas_per_node=config.coordinator_max_replicas_per_node,
    )
    converter = LegacyOfficeRayConverterDeployment.options(**converter_options).bind(
        converter_manager_config=converter_manager_config, config=config
    )
    coordinator_deployment: Any = CanonicalRayCoordinatorDeployment
    return coordinator_deployment.options(**coordinator_options).bind(
        converter_manager_config=converter_manager_config,
        config=config,
        redis_url=redis_url,
        converter_handle=converter,
    )


def deploy_legacy_processor(
    converter_manager_config: Any,
    config: Any,
    redis_url: str,
    app_name: str = upstream.DEFAULT_SERVE_APP_NAME,
) -> Any:
    deployment = create_legacy_deployment(
        converter_manager_config,
        config,
        redis_url,
        app_name,
    )
    return serve.run(deployment, name=app_name, route_prefix=f"/{app_name}")


class DoclingServeRayOrchestrator(RayOrchestrator):
    """Intercept only deployment construction and staged-input cleanup."""

    async def _run_ray_admin(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        if func is upstream.deploy_processor:
            func = deploy_legacy_processor
        return await super()._run_ray_admin(func, *args, **kwargs)

    async def task_status(self, task_id: str, wait: float = 0.0) -> Any:
        return await super().task_status(task_id, wait)
