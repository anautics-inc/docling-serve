from __future__ import annotations

import enum
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Union

import yaml
from pydantic import (
    AliasChoices,
    Field,
    PositiveFloat,
    PositiveInt,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from typing_extensions import Self

if TYPE_CHECKING:
    from docling_serve.settings_views import (
        ArtifactSettings,
        AutoRoutingSettings,
        EngineAdapterSettings,
        GraphSettings,
        LegacyOfficeSettings,
        StagingSettings,
    )

_log = logging.getLogger(__name__)


class UvicornSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="UVICORN_", env_file=".env", extra="allow"
    )

    host: str = "0.0.0.0"
    port: int = 5001
    reload: bool = False
    root_path: str = ""
    proxy_headers: bool = True
    timeout_keep_alive: int = 60
    ssl_certfile: Path | None = None
    ssl_keyfile: Path | None = None
    ssl_keyfile_password: str | None = None
    workers: Union[int, None] = None


class LogLevel(str, enum.Enum):
    WARNING = "WARNING"
    INFO = "INFO"
    DEBUG = "DEBUG"


class LogFormat(str, enum.Enum):
    TEXT = "text"
    JSON = "json"


class AsyncEngine(str, enum.Enum):
    LOCAL = "local"
    RQ = "rq"
    RAY = "ray"


class YamlConfigSettingsSource(PydanticBaseSettingsSource):
    """
    A settings source that loads configuration from a YAML or JSON file.
    The file path is specified via the DOCLING_SERVE_CONFIG_FILE environment variable.
    """

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        # Not used in this implementation
        return None, "", False

    def __call__(self) -> dict[str, Any]:
        """Load configuration from YAML or JSON file if config_file is set."""
        import os

        # Check for config_file in environment variable
        config_path_str = os.environ.get("DOCLING_SERVE_CONFIG_FILE")
        if not config_path_str:
            return {}

        config_path = Path(config_path_str)
        if not config_path.exists():
            raise FileNotFoundError(
                f"Config file not found: {config_path}. Fix the environment variable DOCLING_SERVE_CONFIG_FILE or unset it."
            )

        try:
            with open(config_path) as f:
                if config_path.suffix in [".yaml", ".yml"]:
                    data = yaml.safe_load(f)
                elif config_path.suffix == ".json":
                    data = json.load(f)
                else:
                    raise ValueError(
                        f"Unsupported config file format: {config_path.suffix}. Only .yaml, .yml, and .json are supported."
                    )
            if not isinstance(data, dict):
                raise ValueError(
                    f"Config file must contain a dictionary/object, got {type(data).__name__}"
                )
            return data
        except Exception as err:
            _log.error(f"Error parsing the config file {config_path}")
            raise RuntimeError(f"Failed to parse config file {config_path}") from err

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}()"


class DoclingServeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DOCLING_SERVE_",
        env_prefix_target="all",
        env_file=".env",
        env_parse_none_str="",
        extra="allow",
    )

    # Config file support
    config_file: Path | None = None

    enable_ui: bool = False
    api_host: str = "localhost"
    deployment_mode: Literal["development", "production"] = "development"
    allow_insecure_development: bool = False
    log_level: LogLevel | None = None
    log_format: LogFormat = LogFormat.TEXT
    log_header_prefix: str = "X-Docling-Log-"
    artifacts_path: Path | None = None
    static_path: Path | None = None
    scratch_path: Path | None = None
    single_use_results: bool = True
    load_models_at_boot: bool = True
    options_cache_size: int = 2
    enable_remote_services: bool = False
    allow_external_plugins: bool = False
    allow_custom_vlm_config: bool = False
    allow_custom_picture_description_config: bool = False
    allow_custom_code_formula_config: bool = False
    allow_custom_table_structure_config: bool = False
    allow_custom_layout_config: bool = False
    allow_custom_picture_classification_config: bool = False
    allow_custom_ocr_config: bool = False
    show_version_info: bool = True
    enable_management_endpoints: bool = False
    debug_error_details: bool = False

    api_key: str = ""
    # Fails CLOSED by default: with no api_key configured, every request is
    # refused (503) instead of silently accepted. Set true to explicitly opt
    # back into the permissive "no api_key means no auth" behavior for a
    # deliberately unauthenticated dev/test instance — never for a deployment
    # reachable beyond localhost.
    allow_no_auth: bool = False
    # Authentication modes are deployment-wide and mutually exclusive. Captify
    # deployments set "assertion"; "api_key" remains available for generic
    # upstream clients, with no per-request fallback between the two.
    auth_mode: Literal["assertion", "api_key", "none"] = "api_key"
    allow_default_tenant: bool = False
    default_tenant_id: str = "default"
    assertion_issuer: str = "captify-pytology"
    assertion_audience: str = "docling-service"
    assertion_client_id: str = "captify-platform"
    assertion_algorithm: str = "RS256"
    assertion_public_key: str = ""
    assertion_kms_key_id: str = ""
    assertion_kms_region: str = ""
    assertion_redis_url: str = ""

    # === LiteLLM proxy (shared LLM transport) ===
    # Knowledge-graph extraction routes its model calls through the LiteLLM proxy,
    # which fronts Bedrock and owns credentials/guardrails/spend. When unset, graph
    # extraction is skipped and /v1/graph/extract returns an empty graph + note.
    litellm_base_url: str | None = None
    litellm_api_key: str | None = None

    # === Model-driven extraction (Bedrock via LiteLLM) ===
    # The schematic extractor (and other model-driven passes) may call a multimodal
    # model to *understand* a drawing rather than relying only on vector heuristics.
    # The model name is a LiteLLM proxy alias. Disabled by default -> geometry-only.
    bedrock_enabled: bool = False
    bedrock_vision_model: str = "bedrock-claude-sonnet-4-5"
    bedrock_max_tokens: int = 8192
    # The deployed vision alias uses adaptive thinking. Keep effort low so the
    # model emits extraction JSON instead of consuming the full output budget
    # on hidden reasoning. Set "none" for a non-thinking model alias.
    bedrock_reasoning_effort: str = "low"
    bedrock_temperature: float = 0.0
    bedrock_timeout_seconds: float = 120.0
    bedrock_max_retries: int = 3
    bedrock_max_pages: int = 8
    bedrock_render_dpi: int = 200
    # Figure callout hotspots: when a figure's tesseract callout recall falls
    # below this fraction, a Sonnet-4.5 (LiteLLM/Bedrock) vision pass fills the
    # missing callouts. Set the enable flag off to stay pure-OCR (no model).
    figure_hotspot_vision: bool = False
    figure_hotspot_vision_min_recall: float = 0.75
    # Hard cap on vision callout passes per document (bounds Bedrock spend/latency
    # on figure-dense parts manuals). 0 disables the cap.
    figure_hotspot_vision_max_calls: int = 40
    # Vision parts-TABLE reader for genuinely SCANNED docs: the text-OCR column
    # parser garbles scanned parts pages, so read the table off the rendered page
    # with the vision model instead. Bounded by a per-document page budget.
    vision_parts: bool = False
    vision_parts_max_pages: int = 40
    # Extract schematic-like TO figures into one nested captify.schematic.v1
    # bundle so engineers can edit and export them through the CAD surface.
    technical_order_schematic_figures: bool = False
    technical_order_schematic_max_pages: int = 8
    # Drawing digital twin: frontier-vision (Opus-class) tracing of each
    # called-out part's drawn geometry + assembly graph with reserved 3D slots
    # (captify.drawing-twin.v1). Foundation for the 2D->3D reconstruction path.
    technical_order_drawing_twin: bool = False
    technical_order_drawing_twin_model: str = "chat-opus-4-8"
    technical_order_drawing_twin_max_figures: int = 12

    # === Knowledge-graph extraction (docling-graph via LiteLLM) ===
    # /v1/graph/extract runs docling-graph's template-driven entity+relation
    # extraction (the AWS Comprehend NER replacement) over already-converted text.
    # graph_litellm_* are optional per-path overrides of the shared litellm_* above.
    graph_litellm_base_url: str | None = None
    graph_litellm_api_key: str | None = None
    graph_extraction_enabled: bool = False
    graph_litellm_model: str = "bedrock-claude-sonnet-4-6"
    graph_litellm_provider: str = "litellm_proxy"
    # Dotted import path to a Pydantic template class. None -> built-in generic template.
    graph_extraction_template: str | None = None
    graph_extraction_contract: str = "direct"
    graph_extraction_structured_output: bool = False
    graph_extraction_max_chars: int = 200_000
    graph_extraction_max_output_tokens: int = 32_000
    graph_extraction_context_limit: int = 200_000

    max_document_timeout: float = 3_600 * 24 * 7  # 7 days
    max_num_pages: int = sys.maxsize
    # Finite admission default: large enough for technical manuals while
    # preventing an unbounded multipart/remote-source read.
    max_file_size: PositiveInt = 1024 * 1024 * 1024
    max_sources_per_request: int = 3

    # Bounded automatic document routing. These are policy rather than parser
    # constants so deployments can tune recall without forking client code.
    auto_route_min_parts_signals: int = Field(default=2, ge=1, le=5)
    auto_route_max_pdf_streams: PositiveInt = 200
    auto_route_max_stream_output_bytes: PositiveInt = 2_000_000
    auto_route_max_total_stream_output_bytes: PositiveInt = 8_000_000

    # Worker-side legacy binary Office (.doc/.ppt/.xls) preconversion. The executable
    # is optional: workers auto-discover an already-installed libreoffice/soffice
    # binary and raise a typed capability error when none is available.
    legacy_office_enabled: bool = True
    legacy_office_executable: Path | None = None
    legacy_office_approved_executable_roots: list[Path] = Field(
        default_factory=lambda: [
            Path("/usr/bin"),
            Path("/usr/libexec"),
            Path("/usr/lib64/libreoffice"),
            Path("/usr/lib/libreoffice"),
            Path("/opt/libreoffice"),
        ]
    )
    legacy_office_timeout_seconds: PositiveFloat = 120.0
    legacy_office_max_input_bytes: PositiveInt = 512 * 1024 * 1024
    legacy_office_max_output_bytes: PositiveInt = 512 * 1024 * 1024
    legacy_office_max_scratch_bytes: PositiveInt = 1024 * 1024 * 1024
    legacy_office_max_file_count: PositiveInt = 256
    legacy_office_fetch_timeout_seconds: PositiveFloat = 30.0
    legacy_office_max_redirects: int = Field(default=5, ge=0, le=10)

    # Image export policy
    allowed_image_export_modes: list[str] | None = None  # None = all modes allowed
    max_images_scale: float = 2.0

    # Artifact storage (required for PresignedUrlTarget)
    artifact_storage_enabled: bool = False
    artifact_storage_endpoint: str = ""
    artifact_storage_verify_ssl: bool = True
    artifact_storage_bucket: str = ""
    artifact_storage_access_key: str = ""
    artifact_storage_secret_key: str = ""
    artifact_storage_key_prefix: str = "converted/"
    artifact_storage_presign_ttl_seconds: int = 3600
    upload_staging_mode: Literal["required", "disabled"] = "disabled"
    upload_staging_bucket: str = ""
    upload_staging_region: str = ""
    upload_staging_endpoint: str = ""
    upload_staging_verify_ssl: bool = True
    upload_staging_key_prefix: str = "docling-staging/v1/"
    upload_staging_retention_days: PositiveInt = 1
    upload_staging_cleanup_retention_days: int = Field(default=7, ge=1, le=30)
    upload_staging_dead_letter_retention_days: int = Field(default=30, ge=1, le=90)
    upload_staging_claim_retention_days: int = Field(default=1, ge=1, le=7)
    upload_staging_claim_lease_seconds: float = Field(default=60.0, ge=5.0, le=900.0)
    upload_staging_max_file_size: PositiveInt = 1024 * 1024 * 1024
    upload_staging_kms_key_id: str = ""
    upload_staging_io_timeout_seconds: PositiveFloat = 30.0
    upload_staging_probe_cache_seconds: PositiveFloat = 30.0
    upload_staging_cleanup_retries: int = Field(default=3, ge=0, le=10)
    upload_staging_reconcile_interval_seconds: PositiveFloat = 30.0
    upload_staging_reconcile_batch_size: PositiveInt = 32

    # Threading pipeline
    queue_max_size: int | None = None
    ocr_batch_size: int | None = None
    layout_batch_size: int | None = None
    table_batch_size: int | None = None
    batch_polling_interval_seconds: float | None = None

    sync_poll_interval: int = 2  # seconds
    max_sync_wait: int = 120  # 2 minutes

    cors_origins: list[str] = []
    cors_methods: list[str] = ["GET", "POST"]
    cors_headers: list[str] = [
        "Authorization",
        "Content-Type",
        "X-API-Key",
        "X-Tenant-Id",
        "X-Document-Id",
        "X-Captify-Identity-Assertion",
    ]

    eng_kind: AsyncEngine = AsyncEngine.LOCAL
    result_removal_delay: int = 300  # seconds until result is removed after fetch
    # Local engine
    eng_loc_num_workers: int = 2
    eng_loc_share_models: bool = False
    # RQ engine
    eng_rq_redis_url: str = ""
    eng_rq_queue_name: str = "convert"
    eng_rq_results_prefix: str = "docling:results"
    eng_rq_sub_channel: str = "docling:updates"
    eng_rq_results_ttl: int = 3_600 * 4  # 4 hours default
    eng_rq_failure_ttl: int = 3_600 * 4  # 4 hours default
    eng_rq_redis_max_connections: int = 50
    eng_rq_redis_socket_timeout: float | None = None  # Socket timeout in seconds
    eng_rq_redis_socket_connect_timeout: float | None = (
        None  # Socket connect timeout in seconds
    )
    eng_rq_redis_gate_concurrency: int | None = None
    eng_rq_redis_gate_reserved_connections: int = 10
    eng_rq_redis_gate_wait_timeout: float = 0.25
    eng_rq_redis_gate_status_poll_wait_timeout: float = 5.0
    eng_rq_zombie_reaper_interval: float = 300.0
    eng_rq_zombie_reaper_max_age: float = 3600.0
    # Fair Ray engine
    # Redis Configuration
    eng_ray_redis_url: str = ""
    eng_ray_redis_max_connections: int = 50
    eng_ray_redis_socket_timeout: float | None = None
    eng_ray_redis_socket_connect_timeout: float | None = None
    eng_ray_redis_gate_concurrency: int | None = None
    eng_ray_redis_gate_reserved_connections: int = 10
    eng_ray_redis_gate_wait_timeout: float = 0.25
    eng_ray_redis_gate_status_poll_wait_timeout: float = 5.0

    # Result Storage
    eng_ray_results_ttl: int = 3_600 * 4  # 4 hours
    eng_ray_results_prefix: str = "docling:ray:results"

    # Pub/Sub
    eng_ray_sub_channel: str = "docling:ray:updates"

    # Fair Dispatcher
    eng_ray_dispatcher_interval: float = 30.0
    eng_ray_supervisor_poll_interval: float = 5.0

    # Per-User Dispatcher Limits
    eng_ray_max_concurrent_tasks: int = 5
    eng_ray_max_queued_tasks: int | None = None
    eng_ray_enable_queue_limit_rejection: bool = False
    eng_ray_max_documents: int | None = None
    eng_ray_enable_document_limits: bool = False

    # Ray Configuration
    eng_ray_address: str = ""  # Required - must be set explicitly
    eng_ray_namespace: str = "docling"
    eng_ray_runtime_env: dict | None = None

    # Ray mTLS Configuration
    eng_ray_enable_mtls: bool = False
    eng_ray_cluster_name: str | None = None

    # Ray Serve Autoscaling
    eng_ray_min_actors: int = 1
    eng_ray_max_actors: int = 10
    eng_ray_target_requests_per_replica: PositiveFloat = 1.0
    # Hard cap on concurrent in-flight requests per replica.
    # None -> follow eng_ray_target_requests_per_replica.
    eng_ray_max_ongoing_requests_per_replica: int | None = None
    # Hard cap on converter Serve replicas per Ray node. None -> no cap.
    eng_ray_converter_max_replicas_per_node: int | None = None
    eng_ray_upscale_delay_s: float = 30.0
    eng_ray_downscale_delay_s: float = 600.0
    # None -> use Ray Serve defaults.
    eng_ray_graceful_shutdown_wait_loop_s: float | None = None
    eng_ray_graceful_shutdown_timeout_s: float | None = None
    eng_ray_converter_actor_num_cpus: float = Field(
        1.0,
        validation_alias=AliasChoices(
            "eng_ray_converter_actor_num_cpus",
            "eng_ray_num_cpus_per_actor",
        ),
    )
    eng_ray_enable_pdf_page_slice_fanout: bool = False
    eng_ray_max_page_slice_size: int = 32
    # Unset means "default to eng_ray_max_concurrent_tasks" at runtime.
    # Explicit values override that default but fan-out should never be unbounded.
    eng_ray_max_page_slice_parallelism: int | None = None
    eng_ray_coordinator_min_actors: int | None = None
    eng_ray_coordinator_max_actors: int | None = None
    eng_ray_coordinator_target_requests_per_replica: PositiveFloat | None = None
    eng_ray_coordinator_max_ongoing_requests_per_replica: int = 8
    # Hard cap on coordinator Serve replicas per Ray node. None -> no cap.
    eng_ray_coordinator_max_replicas_per_node: int | None = None
    eng_ray_coordinator_actor_num_cpus: float = 0.25
    eng_ray_coordinator_actor_memory_request: str | None = None

    # Fault Tolerance & Retry
    eng_ray_max_task_retries: int = 3
    eng_ray_retry_delay: float = 5.0
    eng_ray_max_document_retries: int = 2

    # Ray Actor Configuration
    eng_ray_dispatcher_max_restarts: int = -1
    eng_ray_dispatcher_max_task_retries: int = 3

    # Timeouts
    eng_ray_task_timeout: float | None = 3600.0
    eng_ray_document_timeout: float | None = 300.0
    eng_ray_redis_operation_timeout: float = 30.0
    eng_ray_dispatcher_rpc_timeout: float = 5.0
    eng_ray_liveness_fail_after: float = 90.0

    # Health Checks
    eng_ray_enable_heartbeat: bool = True

    # Resource Management & Memory Monitoring
    eng_ray_converter_actor_memory_request: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "eng_ray_converter_actor_memory_request",
            "eng_ray_memory_limit_per_actor",
        ),
    )
    eng_ray_dispatcher_num_cpus: float = 0.25
    eng_ray_dispatcher_memory_request: str | None = None
    eng_ray_object_store_memory: str | None = None
    eng_ray_enable_oom_protection: bool = True
    eng_ray_memory_warning_threshold: float = 0.9

    # Scratch Directory
    eng_ray_scratch_dir: Path | None = None

    # Logging
    eng_ray_log_level: str = "INFO"

    # Tenant ID Header
    eng_ray_tenant_id_header: str = "X-Tenant-Id"

    # OpenTelemetry settings
    otel_enable_metrics: bool = True
    otel_enable_traces: bool = False
    otel_enable_prometheus: bool = True
    otel_enable_otlp_metrics: bool = False
    otel_service_name: str = "docling-serve"

    # Metrics
    metrics_port: int | None = None

    # === DoclingConverterManagerConfig Parameters ===
    # TODO: Don't overwrite the default of docling-jobkit. This requires first some restructure in jobkit.

    # VLM Pipeline Control
    default_vlm_preset: str = "granite_docling"
    allowed_vlm_presets: list[str] | None = None
    custom_vlm_presets: dict[str, Any] = Field(default_factory=dict)
    allowed_vlm_engines: list[str] | None = None

    # Picture Description Control
    default_picture_description_preset: str = "smolvlm"
    allowed_picture_description_presets: list[str] | None = None
    custom_picture_description_presets: dict[str, Any] = Field(default_factory=dict)
    allowed_picture_description_engines: list[str] | None = None

    # Code/Formula Control
    default_code_formula_preset: str = "default"
    allowed_code_formula_presets: list[str] | None = None
    custom_code_formula_presets: dict[str, Any] = Field(default_factory=dict)
    allowed_code_formula_engines: list[str] | None = None

    # Picture Classification Control
    default_picture_classification_preset: str = "document_figure_classifier_v2"
    allowed_picture_classification_presets: list[str] | None = None
    custom_picture_classification_presets: dict[str, Any] = Field(default_factory=dict)

    # Table Structure Control
    default_table_structure_kind: str = "docling_tableformer"
    allowed_table_structure_kinds: list[str] | None = None
    default_table_structure_preset: str = "tableformer_v1_accurate"
    allowed_table_structure_presets: list[str] | None = None
    custom_table_structure_presets: dict[str, Any] = Field(default_factory=dict)

    # Layout Control
    default_layout_kind: str = "docling_layout_default"
    allowed_layout_kinds: list[str] | None = None
    default_layout_preset: str = "docling_layout_default"
    allowed_layout_presets: list[str] | None = None
    custom_layout_presets: dict[str, Any] = Field(default_factory=dict)

    # OCR Control
    default_ocr_preset: str = "auto"
    default_ocr_kind: str = "auto"
    allowed_ocr_presets: list[str] | None = None
    custom_ocr_presets: dict[str, Any] = Field(default_factory=dict)
    allowed_ocr_kinds: list[str] | None = None

    # Target Control
    allowed_target_types: list[str] | None = None

    @property
    def staging(self) -> StagingSettings:
        from docling_serve.settings_views import staging_settings

        return staging_settings(self)

    @property
    def legacy_office(self) -> LegacyOfficeSettings:
        from docling_serve.settings_views import legacy_office_settings

        return legacy_office_settings(self)

    @property
    def graph(self) -> GraphSettings:
        from docling_serve.settings_views import graph_settings

        return graph_settings(self)

    @property
    def auto_routing(self) -> AutoRoutingSettings:
        from docling_serve.settings_views import auto_routing_settings

        return auto_routing_settings(self)

    @property
    def artifacts(self) -> ArtifactSettings:
        from docling_serve.settings_views import artifact_settings

        return artifact_settings(self)

    @property
    def engine_adapters(self) -> EngineAdapterSettings:
        from docling_serve.settings_views import engine_adapter_settings

        return engine_adapter_settings(self)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """
        Customize settings sources to include YAML/JSON config file support.
        Priority order: init > env > dotenv > yaml_config > file_secret
        """
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    @field_validator(
        "custom_vlm_presets",
        "custom_picture_description_presets",
        "custom_code_formula_presets",
        "custom_picture_classification_presets",
        "custom_table_structure_presets",
        "custom_layout_presets",
        "custom_ocr_presets",
        mode="before",
    )
    @classmethod
    def parse_dict_from_json(cls, v: Any) -> dict[str, Any]:
        """Parse dict parameters from JSON-serialized ENV variables."""
        if v is None or v == "":
            return {}
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, dict):
                    return parsed
                return {}
            except json.JSONDecodeError:
                return {}
        return {}

    @field_validator(
        "allowed_vlm_presets",
        "allowed_vlm_engines",
        "allowed_picture_description_presets",
        "allowed_picture_description_engines",
        "allowed_code_formula_presets",
        "allowed_code_formula_engines",
        "allowed_picture_classification_presets",
        "allowed_table_structure_kinds",
        "allowed_table_structure_presets",
        "allowed_layout_kinds",
        "allowed_layout_presets",
        "allowed_ocr_presets",
        "allowed_ocr_kinds",
        "allowed_target_types",
        "allowed_image_export_modes",
        mode="before",
    )
    @classmethod
    def parse_list_from_json_or_csv(cls, v: Any) -> list[str] | None:
        """Parse list parameters from JSON arrays or comma-separated strings."""
        if v is None or v == "":
            return None
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            # Try JSON first
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [str(item) for item in parsed]
            except json.JSONDecodeError:
                pass
            # Fall back to comma-separated
            items = [item.strip() for item in v.split(",") if item.strip()]
            return items if items else None
        return None

    @field_validator("log_level", mode="before")
    @classmethod
    def validate_log_level(cls, v: str | None) -> str | None:
        """Validate and normalize log level to uppercase for case-insensitive support."""
        if v is None:
            return v
        if isinstance(v, str):
            return v.upper()
        return v

    @field_validator("legacy_office_executable")
    @classmethod
    def validate_legacy_office_executable(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("legacy_office_executable must be an absolute path")
        return value

    @field_validator("legacy_office_approved_executable_roots")
    @classmethod
    def validate_legacy_office_roots(cls, value: list[Path]) -> list[Path]:
        if not value or any(not root.is_absolute() for root in value):
            raise ValueError(
                "legacy_office_approved_executable_roots must contain absolute paths"
            )
        return value

    @model_validator(mode="before")
    @classmethod
    def warn_deprecated_ray_settings(cls, data: Any) -> Any:
        if isinstance(data, dict):
            deprecated_keys = {
                "eng_ray_num_cpus_per_actor": "eng_ray_converter_actor_num_cpus",
                "eng_ray_memory_limit_per_actor": "eng_ray_converter_actor_memory_request",
            }
            for old_key, new_key in deprecated_keys.items():
                if old_key in data:
                    _log.warning("%s is deprecated; use %s instead.", old_key, new_key)

        return data

    def _validate_production_policy(self) -> None:
        if self.deployment_mode != "production":
            return
        if self.auth_mode == "none":
            raise ValueError("Production deployments cannot use auth_mode=none")
        if "*" in self.cors_origins:
            raise ValueError(
                "Production deployments require an explicit CORS origin allowlist"
            )
        if self.allow_default_tenant:
            raise ValueError("Production deployments cannot assign a default tenant")
        if self.allow_insecure_development:
            raise ValueError(
                "Production deployments cannot enable insecure development exceptions"
            )

    def _validate_remote_model_policy(self) -> None:
        remote_model_features = {
            "bedrock_enabled": self.bedrock_enabled,
            "figure_hotspot_vision": self.figure_hotspot_vision,
            "vision_parts": self.vision_parts,
            "technical_order_drawing_twin": self.technical_order_drawing_twin,
            "graph_extraction_enabled": self.graph_extraction_enabled,
        }
        if any(remote_model_features.values()) and not (
            self.litellm_base_url and self.litellm_api_key
        ):
            enabled = ", ".join(
                name for name, value in remote_model_features.items() if value
            )
            raise ValueError(
                "Remote model features require litellm_base_url and litellm_api_key: "
                + enabled
            )

    def _validate_upload_staging_policy(self) -> None:
        if self.upload_staging_mode != "required":
            return
        required_staging = {
            "upload_staging_bucket": self.upload_staging_bucket,
            "upload_staging_region": self.upload_staging_region,
        }
        missing = [name for name, value in required_staging.items() if not value]
        if missing:
            raise ValueError(
                "Required upload staging is missing configuration: "
                + ", ".join(missing)
            )
        if not self.upload_staging_verify_ssl:
            raise ValueError("Required upload staging must verify TLS certificates")
        if self.upload_staging_endpoint and not self.upload_staging_endpoint.startswith(
            "https://"
        ):
            raise ValueError("Required upload staging endpoint must use https")
        if self.upload_staging_key_prefix != "docling-staging/v1/":
            raise ValueError(
                "Required upload staging must use fixed prefix 'docling-staging/v1/'"
            )

    def _validate_async_engine_policy(self) -> None:
        if self.eng_kind == AsyncEngine.RQ and not self.eng_rq_redis_url:
            raise ValueError("RQ Redis url is required when using the RQ engine.")
        if self.eng_kind == AsyncEngine.RAY:
            if not self.eng_ray_redis_url:
                raise ValueError(
                    "Fair Ray Redis URL is required when using the RAY engine."
                )
            if not self.eng_ray_address:
                raise ValueError(
                    "Fair Ray address is required when using the RAY engine. "
                    "Use 'auto' or 'local' for local Ray, or provide a Ray cluster address."
                )

    @model_validator(mode="after")
    def engine_settings(self) -> Self:
        self._validate_production_policy()
        self._validate_remote_model_policy()
        self._validate_upload_staging_policy()
        self._validate_async_engine_policy()
        return self


uvicorn_settings = UvicornSettings()
docling_serve_settings = DoclingServeSettings()
