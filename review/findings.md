# docling-serve Review — Triaged Findings & Build Worklist

Source: self-review + adversarial ("codex") review against `review/constitution.md`
and `review/final-review.md`. Verified on disk. **Manager decision** column is
binding for the build pass. Severity: BLOCKER > HIGH > MEDIUM > LOW.

Legend — Decision: **FIX** (do now) · **GUARD** (additive guard, don't rewrite
upstream) · **DOCS** · **DEFER** (record as known issue, out of this pass).

| ID | Sev | Decision | file:line | Problem | Required action |
|----|-----|----------|-----------|---------|-----------------|
| F1 | BLOCKER | GUARD | `auth.py:38,52` + `settings.py:118` | Auth fails **open** when `api_key=""` (upstream behavior). | Add a settings startup validator: if `api_key==""` raise unless `DOCLING_SERVE_ALLOW_UNAUTHENTICATED=true` is explicitly set. Do **not** change `auth.py`'s comparison logic (upstream). |
| F4 | BLOCKER | FIX | `.gitlab-ci.yml:526,529,532` | `cat deploy.sh` prints the registry password + base64 env file into CI logs. | Delete the `echo "===== Rendered…"` + `cat deploy.sh` + closing `echo` lines (531-533). Do not print rendered secrets. |
| F3 | BLOCKER | FIX | `.gitlab-ci.yml:93-94,202-203` | `lint` and `unit-tests` jobs are `when: never`; no mypy job. | Replace both `rules: - when: never` with the repo's standard MR/branch rules (mirror another active job). Add a `typecheck` job running `uv run mypy docling_serve/graph docling_serve/policy.py docling_serve/settings.py`. |
| F5 | BLOCKER | FIX | `tests/` (absent) | No graph or auth tests. | Add `tests/test_graph_extraction.py` (allow-list reject, `graph_payload_from_text` empty/no-text/unconfigured, `_graph_to_payload` shape via fake DiGraph) and `tests/test_auth.py` (open when key unset+guard off, 401 when key set & wrong/missing, 200 when correct). No live LLM. |
| F11 | HIGH | FIX | `tests/trash/**` | Retired `deep_document/` + possible AFTO/CUI artifacts; breaks `pytest` collection. | Delete `tests/trash/` from the working tree; add it to `.gitignore`; add `[tool.pytest.ini_options] testpaths=["tests"]` + `norecursedirs=["tests/trash"]` (or `addopts="--ignore=tests/trash"`) so collection succeeds. |
| F9 | HIGH | FIX | `app.py` (all `[TENANT_ID]` lines) | Tenant identity + raw header echoed at INFO. | Downgrade every `[TENANT_ID]`/`received tenant_id`/upload-filename-size-md5 log to `_log.debug`; drop `header_value='…'`. |
| F6 | HIGH | FIX | `settings.py:255-257`, `extraction.py:54,81,87,139` | Captify LiteLLM keys are plain `str`. | Type `litellm_api_key`/`graph_litellm_api_key` (and `_GraphConfig.api_key`) as `SecretStr`; call `.get_secret_value()` only at the httpx boundary. |
| F8 | HIGH | FIX | `templates.py` (12 `model_config` dicts) | 12 mypy `typeddict-unknown-key` errors. | Add a `GraphModelConfig` TypedDict (extends `ConfigDict`) with `is_entity: bool`, `graph_id_fields: list[str]`; annotate each `model_config`. |
| F10 | HIGH | FIX | `extraction.py:77-98,217` | Defensive `getattr(settings, …)` on statically-defined fields + duplicated default literals. | Access settings attributes directly; delete inline default literals (settings is the single source). |
| F12 | HIGH | FIX | `graph/extraction.py`, `graph/models.py` | `ruff format --check` fails. | Run `uv run ruff format docling_serve/`. |
| F7 | HIGH | GUARD | `settings.py:120-122` | `max_document_timeout=7d`, `max_num_pages/max_file_size=sys.maxsize` (upstream). | Set conservative Captify defaults (`max_document_timeout=300`, `max_num_pages=5000`, `max_file_size=314572800` = 300MB) and document overrides in `.env.example`. Keep env-overridable. |
| F13 | MEDIUM | FIX | `graph/models.py:11` | `text` has no max length; multi-GB body buffered before truncation. | Add `Field(max_length=…)` sized to `graph_extraction_max_chars` ceiling (e.g. 5_000_000) so oversized input 422s early. |
| F14 | MEDIUM | FIX | `extraction.py:101-160` | No wall-clock timeout on the remote LLM call. | Add `graph_extraction_timeout_s: float = 120.0` setting; pass it through `ConnectionOverrides`/client timeout. |
| F18 | MEDIUM | FIX | `app.py:1148` / `extraction.py:119-121` | Caller `tenant_id` forwarded verbatim as outbound header. | Validate against `^[A-Za-z0-9_.-]{1,64}$` before forwarding; drop/replace with `default` if invalid. |
| F16 | MEDIUM | FIX | `settings.py:79-80` | YAML config loader swallows all exceptions → `{}`. | Log the failure at ERROR and re-raise (fail closed). |
| F19 | LOW | FIX | `extraction.py:256,274` | `v not in (None, "")` can raise on array-valued attrs. | Guard with `v is None or (isinstance(v, str) and v == "")` style check. |
| F17 | MEDIUM | DOCS | `docs/adr/0001-*.md` | FIPS self-test bypass not enumerated in the ADR. | Add a "FIPS self-test bypass (auditable hack)" section to ADR-0001. |
| F20 | LOW | FIX | `graph/models.py:12-16` | `template` field description advertises arbitrary import. | Reword to "must match a server allow-listed template/profile (see PROFILE_TEMPLATES)". |
| F2 | HIGH | FIX | `settings.py:118` | `api_key: str=""` secret-as-str on `extra="allow"` model. | Type as `Optional[SecretStr]` default `None`; update `auth.py` wiring (`require_auth = APIKeyAuth(api_key.get_secret_value() if api_key else "")`) and the F1 guard to read it. Keep behavior identical to upstream when a key IS set. |
| F15 | MEDIUM | FIX | `app.py:1191-1202` | Websocket key as `?api_key=` query param (secret in URL). | Accept the key via `X-Api-Key` header (and optionally keep the query param as a deprecated fallback for one release); on failure `await websocket.close(code=1008)` instead of raising `HTTPException`. |

## Build pass scope (binding) — ALL findings in scope

**Builder MUST implement ALL findings:** F1, F2, F3, F4, F5, F6, F7, F8, F9, F10,
F11, F12, F13, F14, F15, F16, F17, F18, F19, F20. Nothing is deferred.

**F1/F2 auth handling:** keep upstream's "key set ⇒ enforced" behavior byte-for-byte;
the only added behavior is failing closed when NO key is configured (unless
`DOCLING_SERVE_ALLOW_UNAUTHENTICATED=true`). `auth.py`'s comparison logic for a
configured key must remain unchanged; only adapt the constructor wiring for
`SecretStr`.

**Stay inside the Captify surface:** `docling_serve/graph/*`, `settings.py`,
`auth.py` (wiring only), the graph endpoint + websocket auth + `[TENANT_ID]`
logging in `app.py`, `.gitlab-ci.yml`, `pyproject.toml`, `.gitignore`,
`docs/adr/`, `.env.example`, `tests/`.

**Do NOT commit.** Leave the working tree dirty; the manager reviews then commits.

## Manager adversarial review & sign-off (2026-06-20)

All 20 findings implemented and independently verified by the manager:

- **Gates (independently re-run):** `ruff format --check` clean · `ruff check` clean ·
  `mypy docling_serve/graph docling_serve/policy.py docling_serve/settings.py` →
  *Success, no issues* · targeted `pytest` → **59 passed**, +7 new tests.
- **Two remaining red tests are PRE-EXISTING** (unchanged on HEAD, files not in our
  diff): `test_otel_filtering::test_filtered_paths_constant` and a session-teardown
  `Event loop is closed` in `test_service_policy`. Out of scope for this pass.
- **Reviewer-added fix:** `.env.example` now documents the new auth (fail-closed),
  resource-limit, and graph/litellm settings (the build pass missed this doc).
- **All `api_key` readers verified SecretStr-safe** (gradio truthiness, auth.py
  raw-string wiring, websocket `.get_secret_value()`).

### ⚠️ Deployment-affecting behavior change (PR call-out, not a code defect)
The fail-closed guard (F1/F2) means the container **refuses to boot** unless
`DOCLING_SERVE_API_KEY` (preferred) **or** `DOCLING_SERVE_ALLOW_UNAUTHENTICATED=true`
is set in the runtime env. The deploy `.env` MUST set one of these before rollout.

**Verdict: PASS — cleared to commit.** No merge; PR only.

## Round 2 — codex (real model) second-opinion review, triaged (2026-06-20)

Driven via `codex exec`. All 9 accepted as FIX (user: fix every item).

| ID | Sev | Decision | Action |
|----|-----|----------|--------|
| R1 | BLOCKER | FIX | Remove `?api_key=` from status websocket; header-only (breaking — note in PR). |
| R2 | BLOCKER | FIX | Set `LITELLM_LOCAL_MODEL_COST_MAP=True` in Containerfile offline block AND defensively in extraction.py before importing docling_graph (stops LiteLLM GitHub cost-map fetch in air-gap). |
| R3 | HIGH | FIX | Replace per-call ThreadPoolExecutor with a module-level bounded shared executor (capped orphan threads + backpressure). |
| R4 | HIGH | FIX | Move fail-closed auth check out of the import-time model_validator into a `validate_serving_auth_mode()` called from `create_app()`, so non-serving imports/CLI don't crash. |
| R5 | HIGH | FIX | Centralize tenant_id charset validation in `_get_tenant_id_from_header()` (drop invalid -> "default") so convert/chunk metadata is also sanitized, not just the graph path. |
| R6 | MEDIUM | FIX | If `DOCLING_SERVE_CONFIG_FILE` is set but missing, raise FileNotFoundError (keep unset -> {}). |
| R7 | MEDIUM | FIX | Use `hmac.compare_digest` for configured-key comparison (auth.py + websocket). |
| R8 | MEDIUM | FIX | Add a startup test asserting create_app() fails closed with no key and passes with the opt-in/key. |
| R9 | LOW | FIX | `.env.example`: the "run without auth" example must be `=true` (was wrongly `=false`). |

### Round 2 — manager adversarial review of codex's build (PASS)
Independently verified: ruff format/check + mypy clean; 19 directly-affected tests pass
incl. new fail-closed-startup (R8) and fail-loud-config (R6) tests. Verified R4 moved the
auth check to a `create_app()`-invoked method (uvicorn `factory=True`, so import/CLI paths
no longer crash), R5 sanitizes tenant ids for all endpoints, R1 is header-only, R7 uses
`hmac.compare_digest`, R2 sets `LITELLM_LOCAL_MODEL_COST_MAP` before the lazy docling_graph
import (+ Containerfile), R3 uses a bounded shared executor. Same 2 pre-existing unrelated
failures remain. **Verdict: PASS — cleared to commit.**

## Round 3 — pre-existing flake fix (+ regressions caught while verifying)

- **Otel flake (the noted item):** `/ready` is a real registered health endpoint, so
  `FILTERED_PATHS` correctly includes it — the *test* was stale. Updated
  `test_otel_filtering.py` to include `/ready` (constant + drop + exclude-match
  cases) and corrected the source docstrings. FIXED.
- **event_loop teardown error:** traced to `test_health_probes`' deprecated
  session-scoped `event_loop` fixture; migrated it to the pytest-asyncio
  `loop_scope="session"` pattern (other files' fixtures hardened to yield/close).
  Teardown error gone.
- **Regression caught (F2 fallout):** 11 integration-test `auth_headers` fixtures
  passed the now-`SecretStr` `api_key` straight into a header
  (`TypeError: Header value must be str`). Fixed with `.get_secret_value()`.
- **Regression caught (R1 fallout):** the websocket integration test used the
  removed `?api_key=` query param; switched to the `X-Api-Key` header.
- **CI scope:** the newly-enabled `unit-tests` job ran the whole `tests/` tree,
  which pulled in slow in-process model-conversion suites (timeout) and
  live-server suites (no server). Scoped the job to the fast deterministic unit
  set; live-server `test_1-*/test_2-*` are conftest-ignored unless
  `DOCLING_SERVE_RUN_INTEGRATION=1`.

## Round 4 — IP-aware auth (local installs don't require a key)

Per request: `DOCLING_SERVE_API_KEY` is now required only for **non-local** callers.

- `auth.is_private_client()` (ipaddress: loopback/RFC1918/link-local/ULA) gates on
  the socket peer only — `X-Forwarded-For` is NOT trusted (documented caveat: behind
  a private-network proxy, set `auth_allow_private_networks=false` + a key).
- `APIKeyAuth` exempts private clients (and `allow_unauthenticated`); public clients
  must present a valid key, and are rejected if none is configured.
- Websocket reuses the same `request_requires_key()` gate.
- `validate_serving_auth_mode()` no longer fails closed on a missing key (local
  installs boot); it raises only in the unusable fully-locked config (no key + no
  private bypass + not unauthenticated).
- New setting `auth_allow_private_networks: bool = True`. Docs updated
  (.env.example, constitution VI.1). 20 auth tests pass; fast unit set 94 passed.
