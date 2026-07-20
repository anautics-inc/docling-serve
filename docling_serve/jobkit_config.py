"""Shared Docling Jobkit configuration builders.

Both the API service and worker CLI use these builders so deployment modes cannot
silently drift as settings are added.
"""

from __future__ import annotations

from typing import Any

from docling_serve.settings import AsyncEngine, docling_serve_settings
from docling_serve.storage import get_scratch


def current_async_engine() -> AsyncEngine:
    return docling_serve_settings.eng_kind


def traces_enabled() -> bool:
    return docling_serve_settings.otel_enable_traces


def build_converter_manager_config() -> Any:
    from docling_jobkit.convert.manager import DoclingConverterManagerConfig

    settings = docling_serve_settings
    return DoclingConverterManagerConfig(
        artifacts_path=settings.artifacts_path,
        options_cache_size=settings.options_cache_size,
        enable_remote_services=settings.enable_remote_services,
        allow_external_plugins=settings.allow_external_plugins,
        max_num_pages=settings.max_num_pages,
        max_file_size=settings.max_file_size,
        queue_max_size=settings.queue_max_size,
        ocr_batch_size=settings.ocr_batch_size,
        layout_batch_size=settings.layout_batch_size,
        table_batch_size=settings.table_batch_size,
        batch_polling_interval_seconds=settings.batch_polling_interval_seconds,
        default_vlm_preset=settings.default_vlm_preset,
        allowed_vlm_presets=settings.allowed_vlm_presets,
        custom_vlm_presets=settings.custom_vlm_presets,
        allowed_vlm_engines=settings.allowed_vlm_engines,
        allow_custom_vlm_config=settings.allow_custom_vlm_config,
        default_picture_description_preset=settings.default_picture_description_preset,
        allowed_picture_description_presets=settings.allowed_picture_description_presets,
        custom_picture_description_presets=settings.custom_picture_description_presets,
        allowed_picture_description_engines=settings.allowed_picture_description_engines,
        allow_custom_picture_description_config=settings.allow_custom_picture_description_config,
        default_code_formula_preset=settings.default_code_formula_preset,
        allowed_code_formula_presets=settings.allowed_code_formula_presets,
        custom_code_formula_presets=settings.custom_code_formula_presets,
        allowed_code_formula_engines=settings.allowed_code_formula_engines,
        allow_custom_code_formula_config=settings.allow_custom_code_formula_config,
        default_picture_classification_preset=settings.default_picture_classification_preset,
        allowed_picture_classification_presets=settings.allowed_picture_classification_presets,
        custom_picture_classification_presets=settings.custom_picture_classification_presets,
        allow_custom_picture_classification_config=settings.allow_custom_picture_classification_config,
        default_table_structure_kind=settings.default_table_structure_kind,
        allowed_table_structure_kinds=settings.allowed_table_structure_kinds,
        default_table_structure_preset=settings.default_table_structure_preset,
        allowed_table_structure_presets=settings.allowed_table_structure_presets,
        custom_table_structure_presets=settings.custom_table_structure_presets,
        allow_custom_table_structure_config=settings.allow_custom_table_structure_config,
        default_layout_kind=settings.default_layout_kind,
        allowed_layout_kinds=settings.allowed_layout_kinds,
        default_layout_preset=settings.default_layout_preset,
        allowed_layout_presets=settings.allowed_layout_presets,
        custom_layout_presets=settings.custom_layout_presets,
        allow_custom_layout_config=settings.allow_custom_layout_config,
        default_ocr_preset=settings.default_ocr_preset,
        default_ocr_kind=settings.default_ocr_kind,
        allowed_ocr_presets=settings.allowed_ocr_presets,
        custom_ocr_presets=settings.custom_ocr_presets,
        allowed_ocr_kinds=settings.allowed_ocr_kinds,
        allow_custom_ocr_config=settings.allow_custom_ocr_config,
    )


def build_s3_presigned_config() -> Any | None:
    """Build managed result storage configuration when it is enabled."""

    settings = docling_serve_settings
    if not settings.artifact_storage_enabled:
        return None

    from docling.datamodel.service.sources import S3Coordinates
    from docling_jobkit.config.target_config import S3PresignedConfig

    return S3PresignedConfig(
        s3_coords=S3Coordinates(
            endpoint=settings.artifact_storage_endpoint,
            verify_ssl=settings.artifact_storage_verify_ssl,
            access_key=settings.artifact_storage_access_key,
            secret_key=settings.artifact_storage_secret_key,
            bucket=settings.artifact_storage_bucket,
            key_prefix=settings.artifact_storage_key_prefix,
        ),
        url_expiration=settings.artifact_storage_presign_ttl_seconds,
    )


def build_local_orchestrator_config() -> Any:
    from docling_jobkit.orchestrators.local.orchestrator import (
        LocalOrchestratorConfig,
    )

    settings = docling_serve_settings
    return LocalOrchestratorConfig(
        num_workers=settings.eng_loc_num_workers,
        shared_models=settings.eng_loc_share_models,
        scratch_dir=get_scratch(),
        result_removal_delay=settings.result_removal_delay,
        s3_presigned_config=build_s3_presigned_config(),
    )


def build_rq_orchestrator_config() -> Any:
    from docling_jobkit.orchestrators.rq.orchestrator import RQOrchestratorConfig

    settings = docling_serve_settings
    return RQOrchestratorConfig(
        redis_url=settings.eng_rq_redis_url,
        queue_name=settings.eng_rq_queue_name,
        results_prefix=settings.eng_rq_results_prefix,
        sub_channel=settings.eng_rq_sub_channel,
        scratch_dir=get_scratch(),
        debug_error_details=settings.debug_error_details,
        results_ttl=settings.eng_rq_results_ttl,
        failure_ttl=settings.eng_rq_failure_ttl,
        redis_max_connections=settings.eng_rq_redis_max_connections,
        redis_socket_timeout=settings.eng_rq_redis_socket_timeout,
        redis_socket_connect_timeout=settings.eng_rq_redis_socket_connect_timeout,
        redis_gate_concurrency=settings.eng_rq_redis_gate_concurrency,
        redis_gate_reserved_connections=settings.eng_rq_redis_gate_reserved_connections,
        redis_gate_wait_timeout=settings.eng_rq_redis_gate_wait_timeout,
        redis_gate_status_poll_wait_timeout=settings.eng_rq_redis_gate_status_poll_wait_timeout,
        zombie_reaper_interval=settings.eng_rq_zombie_reaper_interval,
        zombie_reaper_max_age=settings.eng_rq_zombie_reaper_max_age,
        result_removal_delay=settings.result_removal_delay,
        s3_presigned_config=build_s3_presigned_config(),
    )


def build_ray_orchestrator_config() -> Any:
    from docling_jobkit.orchestrators.ray.config import RayOrchestratorConfig

    settings = docling_serve_settings
    max_page_slice_parallelism = (
        settings.eng_ray_max_page_slice_parallelism
        if settings.eng_ray_max_page_slice_parallelism is not None
        else settings.eng_ray_max_concurrent_tasks
    )
    return RayOrchestratorConfig(
        s3_presigned_config=build_s3_presigned_config(),
        redis_url=settings.eng_ray_redis_url,
        redis_max_connections=settings.eng_ray_redis_max_connections,
        redis_socket_timeout=settings.eng_ray_redis_socket_timeout,
        redis_socket_connect_timeout=settings.eng_ray_redis_socket_connect_timeout,
        redis_gate_concurrency=settings.eng_ray_redis_gate_concurrency,
        redis_gate_reserved_connections=settings.eng_ray_redis_gate_reserved_connections,
        redis_gate_wait_timeout=settings.eng_ray_redis_gate_wait_timeout,
        redis_gate_status_poll_wait_timeout=settings.eng_ray_redis_gate_status_poll_wait_timeout,
        results_ttl=settings.eng_ray_results_ttl,
        results_prefix=settings.eng_ray_results_prefix,
        result_removal_delay=settings.result_removal_delay,
        sub_channel=settings.eng_ray_sub_channel,
        dispatcher_interval=settings.eng_ray_dispatcher_interval,
        supervisor_poll_interval=settings.eng_ray_supervisor_poll_interval,
        max_concurrent_tasks=settings.eng_ray_max_concurrent_tasks,
        max_queued_tasks=settings.eng_ray_max_queued_tasks,
        enable_queue_limit_rejection=settings.eng_ray_enable_queue_limit_rejection,
        max_documents=settings.eng_ray_max_documents,
        enable_document_limits=settings.eng_ray_enable_document_limits,
        ray_address=(
            None
            if settings.eng_ray_address in {"auto", "local"}
            else settings.eng_ray_address
        ),
        ray_namespace=settings.eng_ray_namespace,
        ray_runtime_env=dict(settings.eng_ray_runtime_env or {}),
        enable_mtls=settings.eng_ray_enable_mtls,
        ray_cluster_name=settings.eng_ray_cluster_name,
        min_actors=settings.eng_ray_min_actors,
        max_actors=settings.eng_ray_max_actors,
        target_requests_per_replica=settings.eng_ray_target_requests_per_replica,
        max_ongoing_requests_per_replica=settings.eng_ray_max_ongoing_requests_per_replica,
        converter_max_replicas_per_node=settings.eng_ray_converter_max_replicas_per_node,
        upscale_delay_s=settings.eng_ray_upscale_delay_s,
        downscale_delay_s=settings.eng_ray_downscale_delay_s,
        graceful_shutdown_wait_loop_s=settings.eng_ray_graceful_shutdown_wait_loop_s,
        graceful_shutdown_timeout_s=settings.eng_ray_graceful_shutdown_timeout_s,
        converter_actor_num_cpus=settings.eng_ray_converter_actor_num_cpus,
        enable_pdf_page_slice_fanout=settings.eng_ray_enable_pdf_page_slice_fanout,
        max_page_slice_size=settings.eng_ray_max_page_slice_size,
        max_page_slice_parallelism=max_page_slice_parallelism,
        coordinator_min_actors=settings.eng_ray_coordinator_min_actors,
        coordinator_max_actors=settings.eng_ray_coordinator_max_actors,
        coordinator_target_requests_per_replica=settings.eng_ray_coordinator_target_requests_per_replica,
        coordinator_max_ongoing_requests_per_replica=settings.eng_ray_coordinator_max_ongoing_requests_per_replica,
        coordinator_max_replicas_per_node=settings.eng_ray_coordinator_max_replicas_per_node,
        coordinator_actor_num_cpus=settings.eng_ray_coordinator_actor_num_cpus,
        coordinator_actor_memory_request=settings.eng_ray_coordinator_actor_memory_request,
        max_task_retries=settings.eng_ray_max_task_retries,
        retry_delay=settings.eng_ray_retry_delay,
        max_document_retries=settings.eng_ray_max_document_retries,
        dispatcher_max_restarts=settings.eng_ray_dispatcher_max_restarts,
        dispatcher_max_task_retries=settings.eng_ray_dispatcher_max_task_retries,
        task_timeout=settings.eng_ray_task_timeout,
        document_timeout=settings.eng_ray_document_timeout,
        redis_operation_timeout=settings.eng_ray_redis_operation_timeout,
        dispatcher_rpc_timeout=settings.eng_ray_dispatcher_rpc_timeout,
        liveness_fail_after=settings.eng_ray_liveness_fail_after,
        enable_heartbeat=settings.eng_ray_enable_heartbeat,
        converter_actor_memory_request=settings.eng_ray_converter_actor_memory_request,
        dispatcher_num_cpus=settings.eng_ray_dispatcher_num_cpus,
        dispatcher_memory_request=settings.eng_ray_dispatcher_memory_request,
        ray_object_store_memory=settings.eng_ray_object_store_memory,
        enable_oom_protection=settings.eng_ray_enable_oom_protection,
        memory_warning_threshold=settings.eng_ray_memory_warning_threshold,
        scratch_dir=settings.eng_ray_scratch_dir or get_scratch(),
        log_level=settings.eng_ray_log_level,
        debug_error_details=settings.debug_error_details,
    )
