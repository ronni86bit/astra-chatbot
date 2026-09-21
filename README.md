# RF/SystemVue Question Assistant

An internal office tool that answers natural-language questions about the
SystemVue RF catalogue (`SystemVue_RF_Unique_Question_Assistant_V63.xlsx`)
through a web chat interface (React frontend + FastAPI backend) and a
terminal chatbot, returning the **exact answer stored in the workbook**
with row-level provenance.

Accuracy, traceability, conservative behaviour and reproducibility take
priority over conversational cleverness. The LLM only interprets the
question; the deterministic catalogue engine is the sole source of answers.

## 1. Project purpose

Engineers ask questions like *"What is the worst Cascaded Gain in the final
node?"* or *"Where is Mismatch Loss above 0.5 dB?"* and get the exact
precomputed catalogue answer, traceable to a specific row ID. Ambiguous
questions are clarified, never guessed.

## 2. Dataset facts (verified by audit)

- 1 worksheet; 4,789 worksheet rows incl. metadata/header rows
- 4,785 candidate data rows → **4,768 non-placeholder records**
  (17 placeholder/header-like rows excluded by the configurable policy)
- 4,768 unique IDs; 0 unindexable records
- 18 metrics · 35 requests · 92 scopes · 6 units · 21 frequency selections
- The workbook is a **QUESTION-ANSWER catalogue**: the ANSWER column holds
  precomputed, formatted results (not raw measurements)
- Base 5-field key `(metric, request, scope, frequency_selection, unit)`:
  4,431 unique keys; **80 duplicate keys covering 417 rows** — resolved by
  the request-aware `params` dimension (see §9)

## 3. Architecture

```
BROWSER (React chat UI, frontend/)          TERMINAL (chatbot.py)
  └──────────┬──────────────┘
             ▼
  FASTAPI WEB API (api.py)  ── per-session ConversationContext
             ▼
  STAGE A deterministic normalization (whitespace, GHz→MHz, casing,
    exact aliases, audited phrase grammar, unsupported-value detection)
             ▼
  STAGE B LLM structured interpretation (provider-agnostic adapter,
    JSON-schema constrained to catalogue vocabulary)
             ▼
  QUERYINTENT VALIDATION (Pydantic + vocabulary + request/param rules)
             ▼
  INTENT/CATALOGUE RESOLUTION (guess veto, catalogue-unique inference)
             ▼
  DETERMINISTIC CATALOGUE ENGINE (lookup_by_intent)
             ▼
  EXACT STORED ANSWER (verbatim) → row-cited response
```

No RAG, no vector database, no semantic retrieval.
The LLM never invents, calculates, modifies or paraphrases answers.

## 4. Setup

```bash
pip install -e .[llm,api]     # backend: openai SDK + FastAPI/uvicorn
cd frontend && npm install    # frontend (Node 18+)
```

Python ≥ 3.10. The workbook must remain in the project root (read-only).

## 5. Environment variables

Copy `.env.example` to **`.env` in the project root**. Current development
configuration:

```
GROQ_API_KEY=gsk_...
LLM_PROVIDER=groq
LLM_MODEL=openai/gpt-oss-120b
```

- Real environment variables always take precedence over `.env` values.
- `LLM_PROVIDER` selects the endpoint; each provider uses its **own** key
  variable and never falls back to another provider's key:
  | provider | key variable | base URL |
  |---|---|---|
  | `groq` (current dev default) | `GROQ_API_KEY` | `https://api.groq.com/openai/v1` |
  | `openrouter` | `OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1` |
  | `openai` | `OPENAI_API_KEY` | `https://api.openai.com/v1` |
- `LLM_MODEL` overrides the provider default model
  (groq: `openai/gpt-oss-120b`, openrouter: `openai/gpt-4o-mini`,
  openai: `gpt-4o-mini`); `LLM_BASE_URL` overrides the endpoint for any
  OpenAI-compatible service.
- Web API options: `CORS_ORIGINS` (comma-separated browser origins),
  `API_HOST`/`API_PORT` (bind address, default `127.0.0.1:8000`),
  `SESSION_TTL` (idle seconds before a web session is dropped).
- `.env` is git-ignored; keys are never printed, logged, or returned by
  the API.

## 6. Running the web application (development)

```bash
python -m rf_catalogue.api            # backend on http://127.0.0.1:8000
cd frontend && npm run dev            # Vite dev server on http://localhost:5173
```

Open http://localhost:5173 — the dev server proxies `/api` to the backend.
If `frontend/dist` exists (after `npm run build`), the API also serves the
built frontend directly at http://127.0.0.1:8000 (single-process mode).

Web endpoints: `POST /api/chat` (message + optional session_id),
`POST /api/reset`, `GET /api/status`, `GET /api/examples`,
`GET /api/health`. Sessions are server-side: follow-up answers
("CGAIN") and clarification chips work across requests; a session id is
issued on the first message and stored in the browser.

## 6b. Running the terminal chatbot

```bash
python -m rf_catalogue.chatbot
```

Commands: `/help` `/status` `/debug` `/examples` `/reset` `/quit`.
Successful answers render the stored ANSWER verbatim plus
`Source: Catalogue row <ID>` and the resolved intent. Clarifications list
the candidate interpretations; NOT_FOUND/UNSUPPORTED/PARSE_ERROR are
explicit. Ctrl+C exits cleanly.

## 7. Running tests (offline; no network, no API key)

```bash
python -m pytest
```

All LLM interactions are scripted (`FakeLLMClient`), including the web API
tests (FastAPI `TestClient`). Live evaluation is completely separate (§8).

## 8. Running live evaluation

```bash
python -m rf_catalogue.evaluation --subset smoke   # 23 audited anchors
python -m rf_catalogue.evaluation --subset core    # 483 items
python -m rf_catalogue.evaluation --subset full    # complete 417-row coverage
```

- Requires a configured key; completely separate from pytest.
- Provider/budget (HTTP 402) failures are excluded from accuracy
  denominators and reported separately.
- Cost discipline: smoke → targeted regression → core → full, only after
  fixes are stable.

## 9. QueryIntent schema

Canonical query = 5 audited base dimensions **+ structured params**:

```
metric · request · scope · frequency_selection · unit  +  params
```

`params` is request-aware and structured (never free-form):

| request | params |
|---|---|
| Threshold Above / Below | `ThresholdParams(threshold)` |
| Closest value | `ClosestTargetParams(closest_target)` |
| Range | `RangeParams(range_min, range_max)` (min < max) |
| Power difference / Individual frequency power difference | `ReferenceQualifierParams(reference_qualifier)` — controlled enum |
| all other requests | `NoParams` |

Validation: required params present, irrelevant params rejected, strict
numerics (no bool/str/NaN/inf), enum-controlled qualifiers, catalogue-valid
metric/request/scope/unit/frequency, request/param compatibility.
See `src/rf_catalogue/query_intent.py` and `src/rf_catalogue/nlp/schemas.py`.

**Conversation context policy:** ALL clarification paths (missing
dimensions, candidate options, ambiguous request wording, missing
parameters) store a pending draft, so a short clarification reply
("CGAIN", "All nodes", "Minimum") resumes the query deterministically
without a new LLM call — in the terminal and on the web. Before the
merged intent is validated, the catalogue-unique inference fills any
remaining null dimensions the catalogue uniquely determines (otherwise
the assistant asks again). A new, grammatically complete question is
always a NEW intent: omitted dimensions are never silently inherited —
explicit elliptical follow-ups ("What about the best?") inherit
metric/scope/unit/frequency only. Request and params are never
inherited. `/reset` (or the web Reset button) clears everything.

## 10. Status semantics

| status | meaning |
|---|---|
| `ANSWERED` (EXACT_MATCH) | exactly one catalogue record resolved; answer is the verbatim stored row |
| `NEEDS_CLARIFICATION` (AMBIGUOUS) | multiple plausible interpretations or missing required information; candidates are listed |
| `NOT_FOUND` | request understood and specific, but the entity/value/combination does not exist |
| `UNSUPPORTED` | request type outside catalogue capabilities (plotting, export, predictions) |
| `PARSE_ERROR` | LLM structured output failed validation |

## 11. Deployment

Production layout (Docker Compose, two containers behind nginx):

```bash
docker compose up --build       # frontend on http://localhost:8080
```

- `deploy/frontend.Dockerfile` — builds the React app, serves `dist/`
  with nginx; `/api` is reverse-proxied to the backend (same origin, no
  CORS in production).
- `deploy/backend.Dockerfile` — installs the package (extras
  `llm,api`), bakes in the read-only workbook, runs uvicorn.
- Secrets are injected at runtime from `.env` via compose `environment`;
  never baked into images.
- Single-process alternative (no Docker): `python -m rf_catalogue.api`
  after `cd frontend && npm run build` serves UI + API on one port.
- Health check: `GET /api/health`. Session store is in-memory (TTL,
  size-capped); a backend restart starts fresh conversations, which is
  the safest failure mode for context inheritance.

## 12. Known limitations

- The raw 5-field key is intentionally non-unique (80 keys / 417 rows);
  `params` resolves them. See the documented xfail in
  `tests/test_ambiguities.py`.
- LLM-based parsing inherits model variability; temperature is 0 and output
  is schema-constrained, but accuracy must be measured, not assumed
  (see §8).
- Live runs require provider credits; budget errors are reported and
  excluded from accuracy denominators.
- Web sessions are in-memory per backend process; do not run multiple
  API replicas without moving session state to shared storage.

## 13. Office-review items

See **`OFFICE_REVIEW.md`**: Worst vs Minimum, Best vs Maximum, Desired
Channel Power vs Channel Power, Channel Noise Power semantics,
output-to-input semantics, CP subtraction ordering, "Where is …" ⇒ All-nodes
convention, final-node context with part scopes. The system behaves
conservatively until these are confirmed.

## 14. Changing LLM provider/model

Set `LLM_PROVIDER` plus the matching key variable (`LLM_PROVIDER=groq` +
`GROQ_API_KEY`, `LLM_PROVIDER=openrouter` + `OPENROUTER_API_KEY`,
`LLM_PROVIDER=openai` + `OPENAI_API_KEY`), or use `LLM_BASE_URL` +
`LLM_API_KEY` for any other OpenAI-compatible endpoint. `LLM_MODEL` always
overrides the default model. The adapter
(`rf_catalogue/nlp/llm_client.py`) is the only provider-specific code.
Structured output uses native `json_schema` (strict) with a `json_object`
fallback. Do not change models without evidence from §8 that parser/schema/
prompt are no longer the limiting factor.

## 15. Adding a new query family

1. Extend the workbook (source of truth) and re-run `python audit_keys.py`.
2. If it needs new parameters: extend `QueryIntent` params models + request
   mapping in `query_intent.py` (request-aware, structured).
3. Add display-name aliases in `nlp/vocabulary.py` (exact phrases only).
4. Extend the audited phrase grammar only if the workbook uses fixed
   wording for it.
5. Regenerate the gold set (`python evaluation/build_gold_set.py`) and add
   offline regression tests; then re-run live evaluation per §8.

## 16. Accuracy / evaluation methodology

Gold items are real workbook rows (verbatim QUESTION, audited row ID),
curated paraphrases, and clarification/NOT_FOUND/UNSUPPORTED cases — all
expectations verified against the deterministic engine at build time.
`expected row ID == resolved row ID` is the authoritative criterion.
Reported metrics: QueryIntent exact match, exact row resolution, per-dimension
(metric/request/scope/unit/frequency) accuracy, parameter extraction,
clarification accuracy + precision, NOT_FOUND accuracy, UNSUPPORTED accuracy.
Provider/budget errors are excluded from denominators. Accuracy claims are
only made from completed live runs; otherwise results are **NOT YET
MEASURED**.

Latest live results are in `evaluation/last_live_report.json`
(regenerated by every core run).
