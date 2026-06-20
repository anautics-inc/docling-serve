# docling-serve Final-Review Gate

**This gate MUST pass before any `git commit`.** It mechanizes
`review/constitution.md`. Run every section against the staged diff (or the whole
tree on a first run). Each check is **PASS / FAIL / N/A**. Any FAIL is a blocker
unless the user explicitly waives it, with the waiver recorded in the commit/PR.

How to run: work top-to-bottom, record a verdict per item, then emit the
**Decision** block at the bottom.

---

## 0. Automated gates (run these commands)

```bash
# from repo root
uv run ruff format --check .
uv run ruff check docling_serve/
uv run mypy docling_serve/graph docling_serve/policy.py docling_serve/settings.py
uv run pytest tests/ -q -k "graph or policy or auth or config or health or env"
```

- [ ] **G1 — format:** `ruff format --check` clean.
- [ ] **G2 — lint:** `ruff check docling_serve/` clean. No `print()` in any module
      reachable from a request path (tools/CLI excepted).
- [ ] **G3 — types:** `mypy` clean on graph, policy, settings. No new `Any`-leaks.
- [ ] **G4 — tests green:** the targeted suite passes; no skips that hide the new
      surface.

---

## 1. docling-as-architecture (Constitution N1, N2, Art. I/II)

- [ ] **A1.1** No new parser/adapter for a format docling already supports
      (PDF/DOCX/XLSX/PPTX/HTML/CSV/MD/TXT/images). Gaps are plugins/serializers.
- [ ] **A1.2** No monkey-patching of docling / docling-jobkit internals
      (`process_*_results`, worker hooks, orchestrator internals). `grep` the diff.
- [ ] **A1.3** Captify delta stays minimal and within the agreed surface
      (`graph/*`, settings, graph endpoint, `policy.py`, Containerfile air-gap, CI,
      docs). Anything else is justified in the PR.
- [ ] **A1.4** `allow_external_plugins` not enabled except for a specific vetted
      plugin with rationale.
- [ ] **A1.5** Pipeline behavior expressed as options/policy, not per-format
      branching code. Production paths set `document_timeout`,
      `max_num_pages`/`max_file_size`, and bounded image scale.

## 2. Document management & S3 (Art. II)

- [ ] **A2.1** S3 output uses jobkit's native pre-signed target (no custom S3
      publisher); manifests are thin serializers.
- [ ] **A2.2** Models loaded from `DOCLING_SERVE_ARTIFACTS_PATH`; no runtime
      auto-download path introduced.
- [ ] **A2.3** Inputs validated/bounded (format allow-list, size, page count,
      SSRF-safe URL fetch) and rejected with 422 *before* enqueue.
- [ ] **A2.4** No real customer/CUI/large binary corpora added to git. Fixtures are
      small, synthetic, license-clean. (Check `tests/` additions in the diff.)

## 3. Indexing & chunking (Art. III) — *N/A if no chunking touched*

- [ ] **A3.1** Uses `HybridChunker`; no re-implemented fixed-size splitting.
- [ ] **A3.2** Chunker tokenizer matches the downstream embedding tokenizer and is
      baked offline.
- [ ] **A3.3** Embeds `contextualize()` output, not raw `chunk.text`.
- [ ] **A3.4** `repeat_table_header` / `merge_peers` preserved; custom serialization
      via `SerializerProvider`.
- [ ] **A3.5** Chunk IDs deterministic/stable for the same input.

## 4. Extraction / graph (Art. IV)

- [ ] **A4.1** Any request-supplied `template`/profile is resolved through the
      allow-list (`_allowed_templates`) before import. No path to arbitrary
      `importlib.import_module`.
- [ ] **A4.2** Output keeps the `{nodes, edges, labels, edgeLabels, ...}` contract;
      if changed, the `graph_entities` consumer is updated in lockstep.
- [ ] **A4.3** Failure paths return an empty graph + `note`; errors expose only
      `type(err).__name__`, never raw LLM/proxy text.
- [ ] **A4.4** Input truncated to `max_chars`; token budgets pinned; temp files
      removed in `finally`.
- [ ] **A4.5** New entity types/predicates follow the controlled vocabulary and
      stay consistent with the domain vocabulary docs.

## 5. Air-gapped / IL5 (Art. V)

- [ ] **A5.1** No new runtime model/tokenizer download. New model deps have a
      build-time bake step in the `Containerfile`.
- [ ] **A5.2** `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` / `HF_DATASETS_OFFLINE`
      remain pinned; entrypoints don't unset them.
- [ ] **A5.3** Dependencies via `uv sync --frozen` against `uv.lock`; no unpinned
      installs. Intentional hacks (FIPS bypass, privileged steps) documented.

## 6. Security & tenancy (Art. VI)

- [ ] **A6.1** New endpoints use `Depends(require_auth)`; auth fails closed (no
      silent open-by-default in a protected environment).
- [ ] **A6.2** No secrets in query params or logs. Keys are `SecretStr`.
- [ ] **A6.3** `tenant_id` treated as untrusted; used only for fairness/metrics/
      spend unless validated against the principal. Documented as non-isolation.
- [ ] **A6.4** URL/S3/template inputs allow-listed (SSRF / RCE guards intact).

## 7. Observability & errors (Art. VII)

- [ ] **A7.1** No identity/PII logged at INFO (tenant, filename, size, md5 ≤ DEBUG).
      No secrets logged.
- [ ] **A7.2** No `print()` in serving code; structured `logging` used.
- [ ] **A7.3** Config/secret loading does not swallow exceptions silently; failures
      are logged.
- [ ] **A7.4** Health/readiness reflects real model-load state.

## 8. Code quality, settings & tests (Art. VIII)

- [ ] **A8.1** New settings are statically typed + commented; no defensive
      `getattr(settings, "x", default)` for statically-defined fields.
- [ ] **A8.2** Operator-facing settings documented in `docs/configuration.md` +
      `.env.example`.
- [ ] **A8.3** No event-loop-blocking calls inside `async def`.
- [ ] **A8.4** Every Captify-added surface in this change has tests (graph
      allow-list reject, `graph_payload_from_text` happy/empty/error,
      `_graph_to_payload` shape, `/v1/graph/extract` auth + degraded note, policy,
      auth open/closed).
- [ ] **A8.5** No stale `.pyc` / retired-fork artifacts reintroduced.

## 9. Upgrade safety & change discipline (Art. IX)

- [ ] **A9.1** Depends only on public docling/jobkit contracts.
- [ ] **A9.2** Strategy/contract/offline-posture changes carry an ADR under
      `docs/adr/`.
- [ ] **A9.3** Conventional commit, scoped; out-of-surface edits justified.

---

## Decision

```
GATE: PASS | FAIL
Blockers (must be empty for PASS, or each explicitly waived by the user):
  - <id> <one-line reason>
Waivers (user-approved):
  - <id> <reason> <approver>
```

A commit may proceed only when **GATE: PASS** (all FAILs resolved or waived).
