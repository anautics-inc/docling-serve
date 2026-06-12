<p align="center">
  <a href="https://github.com/docling-project/docling-serve">
    <img loading="lazy" alt="Docling" src="https://github.com/docling-project/docling-serve/raw/main/docs/assets/docling-serve-pic.png" width="30%"/>
  </a>
</p>

# Docling Serve

Running [Docling](https://github.com/docling-project/docling) as an API service.

> [!IMPORTANT]
> **This is the Captify fork** (`anautics-inc/docling-serve`). It extends
> upstream docling-serve with the Captify extraction subsystem and a
> hardened security/attribution model. See
> [Captify additions](#captify-additions) below before working in this repo.

## Captify additions

This fork is deployed as a private internal service behind the
Cognito-gated captify gateway (captify-pytology). On top of upstream
docling-serve it adds:

### Extraction subsystem

- **Typed extractors** (`docling_serve/extractors/`): a registry-dispatched
  extractor chain per document family — PPTX (native geometry via
  python-pptx), Access databases (mdbtools), XFA forms, schematics
  (vector + raster KiCad reconstruction, net tracing, DC simulation), and a
  generic Docling-backed extractor. Selected per request via the `profile`
  form field (`auto` / `schematic` / `document` / …).
- **Enhancers** (`docling_serve/extractors/enhancers/`): opt-in enrichment
  passes — `image_context` (vision descriptions of embedded images) and
  `knowledge_graph` (entity/relation extraction via
  [docling-graph](https://github.com/docling-project/docling-graph)).
- **Deep extraction bundles** (`docling_serve/deep_document/`,
  `docling_serve/extraction/`): `extraction=deep` uploads publish one
  standard S3 bundle per document (`document.json` / `document.md` /
  `document.html` / `media/` / `extraction.json`) plus a live
  `progress.json`, so notebooks can render an editable digital document
  while the pipeline chunks the markdown for OpenSearch / Neo4j.
- **Graph endpoint** (`POST /v1/graph/extract`): stateless knowledge-graph
  extraction from converted text. The ontology layer downstream owns
  persistence (Neo4j/OpenSearch) — docling-serve never writes to a graph
  store directly.
- **Legacy Office pre-conversion**: `.doc`/`.xls`/`.ppt` uploads are
  converted with headless LibreOffice before the normal chain runs.

See [docs/extraction-connectors-extractors-enhancers.md](./docs/extraction-connectors-extractors-enhancers.md)
and [docs/deep-document-object-contract.md](./docs/deep-document-object-contract.md).

### Security & model-call routing

- **All model calls route through the LiteLLM proxy** — vision passes and
  knowledge-graph extraction authenticate with a service-scoped LiteLLM
  virtual key. The service holds **no** Bedrock/IAM model credentials, and
  its AWS access is S3-only with an explicit destination-bucket allow-list.
- **Service auth is enforced**: callers must present the shared
  `X-Api-Key` (`DOCLING_SERVE_API_KEY`). Unset = unauthenticated; never run
  that way.
- **Per-user spend attribution**: the gateway forwards
  `x-captify-tenant-id` / `x-captify-actor-id` / `x-request-id`; the
  service binds them to the extraction task and forwards them on every
  model call, so LiteLLM spend logs attribute usage to the originating
  user and tenant even though the proxy key is service-scoped.

Full contract: [docs/captify-security-and-attribution.md](./docs/captify-security-and-attribution.md).

### Working in this repo

- `AGENTS.md` is the agent/developer briefing (verification loop, removed
  prototypes, restart procedure).
- Configuration lives in `.env` (documented inline) with the annotated
  template in `.env.example`.
- Run the service via pm2: `pm2 restart docling-serve` — single worker on
  purpose (in-memory task store).
- Verification loop for extraction changes:
  `uv run pytest tests/test_extraction_pipeline.py tests/test_identity_attribution.py tests/test_deep_document_export.py tests/test_env_parsing.py tests/test_config_file_loading.py tests/test_deep_document_options.py tests/test_deep_document_docling_adapter.py`

---

📚 [Docling Serve documentation](./docs/README.md)

- Learning how to [configure the webserver](./docs/configuration.md)
- Get to know all [runtime options](./docs/usage.md) of the API
- Explore useful [deployment examples](./docs/deployment.md)
- And more

> [!NOTE]
> **Migration to the `v1` API.** Docling Serve now has a stable v1 API. Read more on the [migration to v1](./docs/v1_migration.md).

## Getting started

Install the `docling-serve` package and run the server.

```bash
# Using the python package
pip install "docling-serve[ui]"
docling-serve run --enable-ui

# Using container images, e.g. with Podman
podman run -p 5001:5001 -e DOCLING_SERVE_ENABLE_UI=1 quay.io/docling-project/docling-serve
```

The server is available at

- API <http://127.0.0.1:5001>
- API documentation <http://127.0.0.1:5001/docs>
- UI playground <http://127.0.0.1:5001/ui>

![API documentation](img/fastapi-ui.png)

Try it out with a simple conversion:

```bash
curl -X 'POST' \
  'http://localhost:5001/v1/convert/source' \
  -H 'accept: application/json' \
  -H 'Content-Type: application/json' \
  -d '{
    "sources": [{"kind": "http", "url": "https://arxiv.org/pdf/2501.17887"}]
  }'
```

### Container Images

The following container images are available for running **Docling Serve** with different hardware and PyTorch configurations:

#### 📦 Distributed Images

| Image | Description | Architectures | Size |
|-------|-------------|----------------|------|
| [`ghcr.io/docling-project/docling-serve`](https://github.com/docling-project/docling-serve/pkgs/container/docling-serve) <br> [`quay.io/docling-project/docling-serve`](https://quay.io/repository/docling-project/docling-serve) | Base image with all packages installed from the official PyPI index. | `linux/amd64`, `linux/arm64` | 4.4 GB (arm64) <br> 8.7 GB (amd64) |
| [`ghcr.io/docling-project/docling-serve-cpu`](https://github.com/docling-project/docling-serve/pkgs/container/docling-serve-cpu) <br> [`quay.io/docling-project/docling-serve-cpu`](https://quay.io/repository/docling-project/docling-serve-cpu) | CPU-only variant, using `torch` from the PyTorch CPU index. | `linux/amd64`, `linux/arm64` | 4.4 GB |
| [`ghcr.io/docling-project/docling-serve-cu128`](https://github.com/docling-project/docling-serve/pkgs/container/docling-serve-cu128) <br> [`quay.io/docling-project/docling-serve-cu128`](https://quay.io/repository/docling-project/docling-serve-cu128) | CUDA 12.8 build with `torch` from the cu128 index. | `linux/amd64` | 11.4 GB |
| [`ghcr.io/docling-project/docling-serve-cu130`](https://github.com/docling-project/docling-serve/pkgs/container/docling-serve-cu130) <br> [`quay.io/docling-project/docling-serve-cu130`](https://quay.io/repository/docling-project/docling-serve-cu130) | CUDA 13.0 build with `torch` from the cu130 index. | `linux/amd64`, `linux/arm64` | TBD |

> [!IMPORTANT]
> **CUDA Image Tagging Policy**
>
> CUDA-specific images (`-cu128`, `-cu130`) follow PyTorch's CUDA version support lifecycle and are tagged differently from base images:
>
> - **Base images** (`docling-serve`, `docling-serve-cpu`): Tagged with `latest` and `main` for convenience
> - **CUDA images** (`docling-serve-cu*`): **Only tagged with explicit versions** (e.g., `1.12.0`) and `main`
>
> **Why?** CUDA versions are deprecated over time as PyTorch adds support for newer CUDA releases. To avoid accidentally pulling deprecated CUDA versions, CUDA images intentionally exclude the `latest` tag. Always use explicit version tags like:
>
> ```bash
> # ✅ Recommended: Explicit version
> docker pull quay.io/docling-project/docling-serve-cu130:1.12.0
>
> # ❌ Not available for CUDA images
> docker pull quay.io/docling-project/docling-serve-cu130:latest
> ```

#### 🚫 Not Distributed

An image for AMD ROCm 6.3 (`docling-serve-rocm`) is supported but **not published** due to its large size.

To build it locally:

```bash
git clone --branch main git@github.com:docling-project/docling-serve.git
cd docling-serve/
make docling-serve-rocm-image
```

For deployment using Docker Compose, see [docs/deployment.md](docs/deployment.md).

Coming soon: `docling-serve-slim` images will reduce the size by skipping the model weights download.

### Demonstration UI

An easy to use UI is available at the `/ui` endpoint.

![Input controllers in the UI](img/ui-input.png)

![Output visualization in the UI](img/ui-output.png)

## Get help and support

Please feel free to connect with us using the [discussion section](https://github.com/docling-project/docling/discussions).

## Contributing

Please read [Contributing to Docling Serve](https://github.com/docling-project/docling-serve/blob/main/CONTRIBUTING.md) for details.

## References

If you use Docling in your projects, please consider citing the following:

```bib
@techreport{Docling,
  author = {Docling Contributors},
  month = {1},
  title = {Docling: An Efficient Open-Source Toolkit for AI-driven Document Conversion},
  url = {https://arxiv.org/abs/2501.17887},
  eprint = {2501.17887},
  doi = {10.48550/arXiv.2501.17887},
  version = {2.0.0},
  year = {2025}
}
```

## License

The Docling Serve codebase is under MIT license.

## IBM ❤️ Open Source AI

Docling has been brought to you by IBM.
