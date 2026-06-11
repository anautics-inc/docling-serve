# Extraction: Connectors, Extractors, and Enhancers

This document describes the pluggable ingestion architecture in `docling-serve`
and the standard, tool-agnostic output produced for engineering drawings and
schematics.

The pipeline separates three concerns so that adding a **source**, a **format**,
or an **enrichment** never requires touching the others:

```
            connector            extractor                enhancer(s)
  source ───────────────▶ item ──────────────▶ bundle ──────────────▶ enriched bundle
 (where)   IngestionItem  (how → default)   document.json + sidecars   (optional, opt-in)
```

- **Connector** — *where the data comes from*. Yields uniform `IngestionItem`s
  (file bytes, S3 objects, an Access database, an AWS-service result) and a
  `suggested_profile`.
- **Extractor** — *how one document becomes the standard bundle*. Every format
  has a **default extractor**; specialised ones (schematic, Access) register
  ahead of the generic Docling fallback.
- **Enhancer** — *optional enrichment applied on top of any extractor's default
  output*, requested per call (e.g. send each image to a vision agent and write
  the returned context back into the document).

---

## Connectors (`docling_serve/connectors/`)

A connector implements `discover(config) -> Iterator[IngestionItem]`. Built-ins:

| Connector | `connector` name | Purpose |
|-----------|------------------|---------|
| `FileConnector` | `file` | Already-uploaded bytes / local paths (always available). Drawings and schematics arrive here too — they are ordinary files routed by `profile`, not a separate connector. |
| `S3Connector` | `s3` | Every object under a `bucket`/`prefix` (lazy per-object download). |
| `AccessDbConnector` | `accessdb` (alias `access`) | An `.mdb`/`.accdb` file (local or S3), routed to `profile=access`. |
| `AwsServiceConnector` | `aws-service` (alias `aws`) | Data originating from an AWS service, dispatched by `config["service"]`. Ships `s3` and `textract` handlers; register more with `register_aws_handler(...)`. |

`IngestionItem` carries exactly one of `data` / `local_path` / `loader`, plus
`suggested_profile` and `source_refs` (provenance). The allow-list
`DOCLING_SERVE_ALLOWED_CONNECTORS` gates which connectors callers may request
(`file` is always allowed); `DOCLING_SERVE_CONNECTOR_MAX_OBJECT_BYTES` caps the
size of any single pulled object.

Add a new source by registering one `Connector` — nothing in the extractor or
assembly code changes.

---

## Extractors (`docling_serve/extractors/`)

An extractor implements `supports(ctx)` and `build(ctx) -> ExtractorResult`. The
registry (`select_extractor`) returns the first specialised extractor whose
`supports()` is true, otherwise the generic Docling fallback:

| Extractor | `name` | Selected when | Output beyond `document.json` |
|-----------|--------|---------------|-------------------------------|
| `AccessExtractor` | `extract_access` | `.mdb`/`.accdb` suffix or `profile=access` | per-table CSVs, `access-tables.json`, `access-schema.sql` |
| `SchematicExtractor` | `extract_schematic` | `profile=schematic\|drawing`, or `profile=auto` on a vector PDF | normalized SVG, KiCad `.kicad_sch`, `schematic-graph.json`, KiCad `.net` |
| `PptxExtractor` | `extract_ppt` | `.pptx` | (native parse, falls back to Docling) |
| `DoclingExtractor` | `extract_doc` | fallback | generic deep document |

Every extractor produces a validated **deep document** (`document.json`) so the
rest of the pipeline (NER, embeddings, OpenSearch) is unchanged regardless of
source format. Specialised artifacts are advertised in `extraction.json` under
`extractor`, `domain`, `artifacts`, and a domain block (e.g. `schematic`,
`database`).

### Model-driven, not template-driven

The schematic extractor does **not** hard-code a symbol library. It exports
clean geometry, rasterises each page, and asks a Bedrock vision model to read
the drawing and return components/pins/nets/title-block as strict JSON. Python
only orchestrates and normalises. When Bedrock is disabled or unreachable the
extractor still emits the SVG, the `.kicad_sch` geometry replay, and the raster
(the drawing stays openable) and records a note — it never fails the job.

**Connectivity is traced, not guessed.** The model also returns a bounding box
per component; `net_trace.py` then derives nets *deterministically* from the
page's vector line work — wire segments are clipped at component boxes (pin
attachments), joined end-to-line (straight joins and T-junctions connect,
X-crossings do not), and each resulting cluster touching ≥ 2 components is a
net. Net *names* are recovered from the best-overlapping model net, so power
nets split across per-branch symbols (GND, +5V) reunite by label. Traced nets
carry `"source": "geometry"` in `schematic-graph.json`; when tracing is not
possible (no bboxes, raster source) the model's own nets are kept.

---

## Enhancers (`docling_serve/extractors/enhancers/`)

Enhancers are **opt-in** passes that enrich any extractor's default output after
assets are attached. They run only when requested via the `enhancements` field
and never change default behaviour.

| Enhancer | name (aliases) | Effect |
|----------|----------------|--------|
| `ImageContextEnhancer` | `image_context` (`images`, `vision_context`, …) | Sends each image asset to the Bedrock vision model and writes the returned context onto `asset.context` and any referencing `element.context`, plus an `image-context.json` sidecar. |
| `GraphExtractionEnhancer` | `knowledge_graph` (`graph`, `kg`, `entities`, …) | Runs [docling-graph](https://github.com/docling-project/docling-graph) template-driven entity+relationship extraction over the bundle's `document.md` and emits a `knowledge-graph.json` sidecar (typed nodes + directed edges) plus a `document.knowledgeGraph` summary. **This is the AWS Comprehend NER replacement.** |

Example: on a PPT deck, request `enhancements=image_context` to extract the
slide images, run them through the agent for context, and save that context into
the output document.

### Knowledge-graph extraction (Comprehend replacement)

Generic NER (AWS Comprehend) returns flat, relationship-free spans and struggles
on technical domains. The `knowledge_graph` enhancer instead extracts a
*schema-validated* graph defined by a Pydantic **template**: `is_entity` models
become nodes, nested entity lists become typed edges. On a wiring note, for
example, it returns components (`R1` resistor 10k, `K1` relay, …), nets
(`+28V`, `GND`, `SIG_A`), and the connectivity between them — not just isolated
spans.

- **LLM routing**: the call goes through the existing **LiteLLM proxy** (which
  fronts Bedrock) via `provider_override=litellm_proxy` + `model_override` +
  `llm_overrides.connection{base_url, api_key}` — no model SDK is embedded.
  Configure with `DOCLING_SERVE_GRAPH_LITELLM_BASE_URL` / `_API_KEY` / `_MODEL`.
- **Templates**: the built-in `DocumentGraph` is a generic entity/relation
  fallback; set `DOCLING_SERVE_GRAPH_EXTRACTION_TEMPLATE` to a dotted path
  (e.g. a schematic or Access-table template) for domain-specific graphs. *The
  graph is only as good as the template* — prefer a specific one.
- **Declared dependency**: `docling-graph` is a first-class dependency in
  `pyproject.toml`/`uv.lock` (it ships in the image). The code still lazy-imports
  it and degrades gracefully (records a note, no changes) when the LiteLLM
  endpoint is unset — that guard is defensive, not an install opt-in.
- **Persistence stays the ontology**: this enhancer only *produces* the
  `knowledge-graph.json` artifact. The downstream ontology layer ingests it into
  Neo4j/OpenSearch — docling-serve never writes to a graph/search store directly.

Add a new enrichment by registering one `Enhancer`; extractors stay untouched.

#### Synchronous endpoint: `POST /v1/graph/extract`

The same extraction core is exposed as a stateless endpoint so a caller that
already has converted markdown (e.g. the pytology document worker, replacing its
Comprehend NER pass) can get a graph in one call without running a full bundle:

```jsonc
// request
{ "text": "<converted markdown>", "template": "pkg.mod.MyTemplate" }  // template optional
// response
{ "nodes": [...], "edges": [...], "labels": {...}, "edgeLabels": {...},
  "nodeCount": 12, "edgeCount": 7, "model": {...}, "template": "..." }
```

It reads the same `graph_litellm_*` settings and **degrades gracefully**: when
`docling-graph` is not installed or the LiteLLM endpoint is unconfigured it
returns an empty graph with a `note` (HTTP 200) rather than failing, so callers
treat "no graph" uniformly. Authentication matches the other `/v1` routes.

---

## Requesting a profile / connector / enhancements

The convert endpoints (`POST /v1/convert/file` and `/v1/convert/file/async`)
accept extra form fields. These take effect with deep extraction
(`extraction=deep`), which persists the source bytes and writes the expanded
bundle:

| Field | Example | Meaning |
|-------|---------|---------|
| `profile` | `schematic`, `access`, `auto`, `default` | Selects the extractor. |
| `enhancements` | `image_context,knowledge_graph` (comma-separated) | Opt-in enrichment passes. |

```bash
curl -F 'files=@main_schematic.pdf' \
     -F 'extraction=deep' \
     -F 'profile=schematic' \
     -F 'enhancements=image_context' \
     https://<host>/v1/convert/file
```

---

## Standard schematic output (`captify.schematic.v1`)

For a drawing/schematic the bundle's `schematic/` directory contains four
complementary representations so the result can be re-opened in other tools:

1. **Geometry — normalized SVG** (`schematic.svg` or `schematic-page-NNN.svg`).
   Loss-less vector export via `pdftocairo`; opens in any vector/CAD viewer.
2. **Geometry — KiCad schematic** (`schematic.kicad_sch` or
   `schematic-page-NNN.kicad_sch`). Deterministic replay of the SVG geometry —
   every line, shape, and text outline — as KiCad 8 graphical polylines, so the
   drawing opens directly in KiCad's schematic editor. No model involved
   (works even with Bedrock disabled).
3. **Structure — `schematic-graph.json`** (`captify.schematic.v1`). The
   model-derived, tool-agnostic graph of components, pins, nets/connections, and
   the title block. Schema:
   `docling_serve/deep_document/schemas/schematic-graph.schema.json`.
4. **Interchange — KiCad netlist** (`<name>.net`). A KiCad-style S-expression
   netlist generated from the graph — a widely importable EDA format.

`extraction.json` references all four under the `schematic` block, and
`document.json` carries a compact `schematic` summary (`componentCount`,
`netCount`, `modelUnderstood`, and the artifact paths).

### `captify.schematic.v1` shape (abridged)

```jsonc
{
  "schemaVersion": "1.0",
  "artifactKind": "captify.schematic.v1",
  "source": { "originalFileName": "main_schematic.pdf" },
  "model": { "provider": "bedrock", "modelId": "…", "understood": true },
  "pages": [
    { "pageNumber": 1, "svg": "schematic/schematic.svg",
      "raster": "media/schematic-page-001.png", "titleBlock": { "title": "…" } }
  ],
  "components": [
    { "id": "C0001", "refDes": "R1", "type": "resistor", "value": "10k",
      "page": 1, "pins": [ { "id": "C0001-p1", "number": "1", "name": null } ] }
  ],
  "nets": [
    { "id": "N001-0001", "name": "GND", "class": null,
      "nodes": [ { "component": "C0001", "pin": "2" } ] }
  ],
  "confidence": 0.9,
  "warnings": [],
  "notes": []
}
```

Component ids are stable within a document and net `nodes[].component` resolve to
those ids, so the graph and the netlist stay consistent.
