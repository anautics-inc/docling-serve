"""Instrumented RQ worker with OpenTelemetry tracing support."""

import logging
from pathlib import Path

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from docling_jobkit.convert.manager import (
    DoclingConverterManagerConfig,
)
from docling_jobkit.orchestrators.rq.orchestrator import RQOrchestratorConfig
from docling_jobkit.orchestrators.rq.worker import CustomRQWorker

from docling_serve.legacy_office import (
    build_converter_manager,
    check_legacy_office_capability,
)
from docling_serve.rq_instrumentation import extract_trace_context
from docling_serve.settings import docling_serve_settings
from docling_serve.telemetry import (
    record_sanitized_exception,
    sanitize_telemetry_text,
)
from docling_serve.upload_staging import check_upload_staging_capability

logger = logging.getLogger(__name__)


class InstrumentedRQWorker(CustomRQWorker):
    """RQ Worker with OpenTelemetry tracing instrumentation."""

    def __init__(
        self,
        *args,
        orchestrator_config: RQOrchestratorConfig,
        cm_config: DoclingConverterManagerConfig,
        scratch_dir: Path,
        **kwargs,
    ):
        if docling_serve_settings.legacy_office_enabled:
            check_legacy_office_capability()
        if docling_serve_settings.upload_staging_mode == "required":
            check_upload_staging_capability(force=True)
        super().__init__(
            *args,
            orchestrator_config=orchestrator_config,
            cm_config=cm_config,
            scratch_dir=scratch_dir,
            **kwargs,
        )
        # CustomRQWorker constructs jobkit's base manager. Replace it at worker
        # boot so durable RQ jobs preconvert legacy Office inputs immediately
        # before Docling extraction.
        self.conversion_manager = build_converter_manager(cm_config)
        self.tracer = trace.get_tracer(__name__)

    def perform_job(self, job, queue):
        """
        Perform job with distributed tracing support.

        This extracts the trace context from the job metadata and creates
        a span that continues the trace from the API request.
        """
        # Extract parent trace context from job metadata
        parent_context = extract_trace_context(job)

        # Create span name from job function
        func_name = sanitize_telemetry_text(
            job.func_name if hasattr(job, "func_name") else "unknown",
            limit=96,
        )
        span_name = f"rq.job.{func_name}"

        # Start span with parent context
        with self.tracer.start_as_current_span(
            span_name,
            context=parent_context,
            kind=SpanKind.CONSUMER,
        ) as span:
            try:
                # Add job attributes to span
                span.set_attribute("rq.job.func_name", func_name)
                span.set_attribute("rq.queue.name", sanitize_telemetry_text(queue.name))

                if hasattr(job, "timeout") and job.timeout:
                    span.set_attribute("rq.job.timeout", job.timeout)

                # Add job kwargs info
                if hasattr(job, "kwargs") and job.kwargs:
                    # Add conversion manager before executing
                    job.kwargs["conversion_manager"] = self.conversion_manager
                    job.kwargs["orchestrator_config"] = self.orchestrator_config
                    job.kwargs["scratch_dir"] = self.scratch_dir

                    # Log task info if available
                    task_type = job.kwargs.get("task_type")
                    if task_type:
                        span.set_attribute("docling.task.type", str(task_type))

                    sources = job.kwargs.get("sources", [])
                    if sources:
                        span.set_attribute("docling.task.num_sources", len(sources))

                logger.info("Executing instrumented RQ job")

                # Execute the actual job
                result = super().perform_job(job, queue)

                # Mark span as successful
                span.set_status(Status(StatusCode.OK))
                logger.debug("Instrumented RQ job completed successfully")

                return result

            except Exception as e:
                # Record exception and mark span as failed
                logger.error(
                    "Instrumented RQ job failed: %s",
                    sanitize_telemetry_text(e),
                )
                record_sanitized_exception(span, e)
                raise
