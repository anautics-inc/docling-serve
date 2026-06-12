import enum
import json
import logging
from pathlib import Path
from typing import Any, Union

import yaml
from pydantic import AnyUrl, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from typing_extensions import Self

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


class AsyncEngine(str, enum.Enum):
    LOCAL = "local"
    KFP = "kfp"
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
        env_file=".env",
        env_parse_none_str="",
        extra="allow",
    )

    # Config file support
    config_file: Path | None = None

    enable_ui: bool = False
    api_host: str = "localhost"
    log_level: LogLevel | None = None
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
    deep_document_s3_bucket: str = ""
    deep_document_s3_prefix_template: str = "documents/{tenant_id}/docling/{task_id}"
    deep_document_s3_region: str | None = None
    deep_document_service_env_file: str = ""
    # Buckets a caller may target via the ``deep_s3_bucket`` form field. The
    # server-configured ``deep_document_s3_bucket`` is always allowed implicitly.
    # When empty, request-supplied buckets that differ from the server default
    # are rejected (secure default) — this prevents a caller from making the
    # service write its processed output to an arbitrary bucket using the
    # service's own AWS credentials (confused-deputy).
    deep_document_s3_allowed_buckets: list[str] | None = None

    # === LiteLLM proxy (shared LLM transport) ===
    # All model calls — vision passes and knowledge-graph extraction — route
    # through the LiteLLM proxy, which fronts Bedrock and owns credentials,
    # guardrails, usage accounting, and model aliasing. When unset, the vision
    # provider falls back to the graph_litellm_* values below so one endpoint +
    # key serves both paths.
    litellm_base_url: str | None = None
    litellm_api_key: str | None = None

    # === Model-driven extraction (Bedrock via LiteLLM) ===
    # When enabled, profile-driven extractors (schematic, drawing) and the
    # image_context enhancer may call a multimodal model to *understand* a
    # document rather than relying on hard-coded rules. The model name must be
    # a LiteLLM proxy alias (or a ``bedrock/...`` wildcard route).
    bedrock_enabled: bool = False
    bedrock_vision_model: str = "bedrock-claude-sonnet-4-5"
    bedrock_max_tokens: int = 8192
    bedrock_temperature: float = 0.0
    bedrock_timeout_seconds: float = 120.0
    bedrock_max_retries: int = 3
    # Max source pages a model-driven extractor will send to the model per
    # document (cost/latency guard). Raster DPI for those page images.
    bedrock_max_pages: int = 8
    bedrock_render_dpi: int = 200
    # Max images a single enhancement pass (e.g. image_context) will send to the
    # model per document.
    enhancement_max_images: int = 40

    # === Extraction connectors ===
    # Allow-list of source connectors callers may request via the ``connector``
    # form field. ``file`` is always available. Empty -> all built-ins allowed.
    allowed_connectors: list[str] | None = None
    # Bytes ceiling per Access database / S3 object pulled by a connector.
    connector_max_object_bytes: int = 512 * 1024 * 1024

    # === Knowledge-graph extraction (docling-graph via LiteLLM) ===
    # The opt-in ``knowledge_graph`` enhancer runs docling-graph's template-driven
    # entity+relationship extraction (the AWS Comprehend NER replacement) and routes
    # the LLM call through the existing LiteLLM proxy, which fronts Bedrock. The
    # graph is emitted as a ``knowledge-graph.json`` sidecar; the ontology layer
    # downstream owns persistence (Neo4j/OpenSearch). docling-graph is an optional
    # dependency — the enhancer degrades gracefully when it is not installed.
    # base_url/api_key are graph-specific OVERRIDES; unset, the shared
    # litellm_base_url/litellm_api_key above are used.
    graph_litellm_base_url: str | None = None
    graph_litellm_api_key: str | None = None
    graph_litellm_model: str = "bedrock-claude-sonnet-4-6"
    # LiteLLM provider hint passed to docling-graph; ``litellm_proxy`` makes
    # LiteLLM forward ``model`` verbatim to the proxy at ``base_url``.
    graph_litellm_provider: str = "litellm_proxy"
    # Dotted import path to the Pydantic template class. None -> built-in generic
    # entity/relationship template (broad NER + RELATED_TO edges).
    graph_extraction_template: str | None = None
    # docling-graph extraction contract: "direct" | "staged" | "delta".
    graph_extraction_contract: str = "direct"
    # Schema-enforced response_format. Off by default for broad proxy/model support.
    graph_extraction_structured_output: bool = False
    # Upper bound on source characters fed to the graph extractor (cost guard).
    graph_extraction_max_chars: int = 200_000
    # LLM response budget for the extraction call. docling-graph cannot resolve
    # model metadata through a LiteLLM-proxy alias and would fall back to a 4092-token
    # cap — which truncates the JSON on document-scale extractions and fails the run.
    # Claude Sonnet on Bedrock supports >=64k output tokens; 32k is a safe budget.
    graph_extraction_max_output_tokens: int = 32_000
    # Model context window hint (input side). Same proxy-alias metadata gap as above
    # (the fallback is 32k); Claude Sonnet's real window is 200k tokens.
    graph_extraction_context_limit: int = 200_000

    api_key: str = ""

    max_document_timeout: float = 3_600 * 24 * 7  # 7 days
    # Finite ceilings (overridable) so a single oversized/zip-bomb document
    # cannot pin a worker indefinitely. Raise via env if a deployment needs it.
    max_num_pages: int = 10_000
    max_file_size: int = 1024 * 1024 * 1024  # 1 GiB

    # Threading pipeline
    queue_max_size: int | None = None
    ocr_batch_size: int | None = None
    layout_batch_size: int | None = None
    table_batch_size: int | None = None
    batch_polling_interval_seconds: float | None = None

    sync_poll_interval: int = 2  # seconds
    max_sync_wait: int = 120  # 2 minutes

    cors_origins: list[str] = ["*"]
    cors_methods: list[str] = ["*"]
    cors_headers: list[str] = ["*"]

    eng_kind: AsyncEngine = AsyncEngine.LOCAL
    result_removal_delay: int = 300  # seconds until result is removed after fetch
    # Local engine
    eng_loc_num_workers: int = 2
    eng_loc_share_models: bool = False
    # RQ engine
    eng_rq_redis_url: str = ""
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
    # KFP engine
    eng_kfp_endpoint: AnyUrl | None = None
    eng_kfp_token: str | None = None
    eng_kfp_ca_cert_path: str | None = None
    eng_kfp_self_callback_endpoint: str | None = None
    eng_kfp_self_callback_token_path: Path | None = None
    eng_kfp_self_callback_ca_cert_path: Path | None = None

    eng_kfp_experimental: bool = False

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
    eng_ray_dispatcher_interval: float = 2.0

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
    eng_ray_target_requests_per_replica: int = 1
    # Hard cap on concurrent in-flight requests per replica.
    # None -> follow eng_ray_target_requests_per_replica.
    eng_ray_max_ongoing_requests_per_replica: int | None = None
    eng_ray_upscale_delay_s: float = 30.0
    eng_ray_downscale_delay_s: float = 600.0
    # None -> use Ray Serve defaults.
    eng_ray_graceful_shutdown_wait_loop_s: float | None = None
    eng_ray_graceful_shutdown_timeout_s: float | None = None
    eng_ray_num_cpus_per_actor: float = 1.0

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
    eng_ray_memory_limit_per_actor: str | None = None
    eng_ray_object_store_memory: str | None = None
    eng_ray_enable_oom_protection: bool = True
    eng_ray_memory_warning_threshold: float = 0.9

    # Scratch Directory
    eng_ray_scratch_dir: Path | None = None

    # Logging
    eng_ray_log_level: str = "INFO"

    # Caller identity headers. The captify gateway (pytology) forwards the
    # authenticated user on every request; these names must match what
    # serve_client.py sends. The legacy "X-Tenant-Id" name was never sent by
    # any caller, so tenant always fell back to "default" — keep these aligned
    # with the gateway.
    eng_ray_tenant_id_header: str = "x-captify-tenant-id"
    actor_id_header: str = "x-captify-actor-id"
    request_id_header: str = "x-request-id"

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
        "allowed_connectors",
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

    @model_validator(mode="after")
    def engine_settings(self) -> Self:
        # Validate KFP engine settings
        if self.eng_kind == AsyncEngine.KFP:
            if self.eng_kfp_endpoint is None:
                raise ValueError("KFP endpoint is required when using the KFP engine.")

        if self.eng_kind == AsyncEngine.KFP:
            if not self.eng_kfp_experimental:
                raise ValueError(
                    "KFP is not yet working. To enable the development version, you must set DOCLING_SERVE_ENG_KFP_EXPERIMENTAL=true."
                )

        if self.eng_kind == AsyncEngine.RQ:
            if not self.eng_rq_redis_url:
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

        return self


uvicorn_settings = UvicornSettings()
docling_serve_settings = DoclingServeSettings()
