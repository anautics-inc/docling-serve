# Dependency compatibility policy

Dependency locks are refreshed as coherent families and verified across every
declared uv platform group. “Current” means the newest stable release that can
resolve and pass the relevant contract tests on that platform.

## Active upstream constraints

- `typer` remains on 0.24.2 because `docling-core 2.87.1` requires `<0.25`.
- `redis-py` remains on 7.4.1 because `docling-jobkit 2.1.0` requires `<8`.
- NumPy remains below 2.5 while this package supports Python 3.10 and 3.11;
  NumPy 2.5 requires Python 3.12.
- mypy remains on the latest 1.x release because mypy 2 currently reports
  incompatible upstream Docling Graph, Ray, Pydantic plugin, and optional
  extractor annotations. Re-evaluate after those packages publish compatible
  typing metadata.
- Accelerator wheels follow the newest matched Torch/torchvision pair published
  by each official index: ROCm 6.3 uses 2.9/0.24, CUDA 12.8 uses 2.11/0.26,
  and CPU, CUDA 12.6/13.0, and ROCm 7.2 use 2.12/0.27. The default PyPI group
  uses 2.13/0.28.
- Transformers remains platform-split through Docling's constraints; it is not
  forced to one version across macOS and Linux.

Review these exceptions whenever the corresponding upstream family changes.
Do not loosen a cap without resolving every uv group and running the format,
OCR, graph, schematic, orchestration, and container tests it can affect.

## Supply-chain inputs

- Container bases are pinned by multi-platform digest.
- Native source releases are pinned by immutable commit or SHA-256. ngspice
  uses the checksummed official release archive so its generated `configure`
  remains compatible with the pinned EL9 build toolchain.
- Deployment examples use explicit image versions and digests.
- The uv version is aligned across the container, lock, pre-commit, and CI.
- `Containerfile.sbom` pins its Dockerfile frontend, base images, uv, and
  CycloneDX BOM generator. It emits validated, reproducible CycloneDX 1.6
  build and runtime dependency inventories from the frozen lock.
