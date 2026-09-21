# Agents

This project uses a deterministic catalogue engine as its trusted execution
layer. The LLM (milestone 2+) may interpret questions, but all answers come
from the catalogue. Agents should not attempt to integrate neural components
into the engine core.

If new capabilities are needed, extend the engine's dimension coverage or add
new question templates to the catalogue - do not replace the lookup logic with
a model.

## Layout & commands

- `src/rf_catalogue/` — Python package. `engine.py` (deterministic lookup,
  the ONLY answer source), `query_intent.py` (validated query model),
  `nlp/` (normalizer, parser, vocabulary, LLM adapter, conversation
  context), `service.py` (facade), `chatbot.py` (terminal),
  `api.py` (FastAPI web backend).
- `frontend/` — React 18 + Vite chat UI. Talks to the backend ONLY via
  `/api/*`; it never renders content that is not a verbatim backend field.
  `npm run build` emits `frontend/dist`, which `api.py` serves directly
  (single-process mode).
- `tests/` — offline pytest suite (no network, no key). All LLM turns are
  scripted with `FakeLLMClient` from `rf_catalogue.nlp.llm_client`; the web
  API is tested with FastAPI `TestClient` the same way.
- `deploy/` — Dockerfiles + nginx. `docker-compose.yml` runs the stack.

Commands:
- Offline tests: `python -m pytest` (never requires network/API key).
- Backend: `python -m rf_catalogue.api` (port 8000).
- Frontend dev: `cd frontend && npm run dev` (Vite on 5173, proxies /api).
- Frontend build: `cd frontend && npm run build`.
- Full stack: `docker compose up --build` (frontend on 8080).
- Live LLM eval (separate from pytest): `python -m rf_catalogue.evaluation`.

## Safety / Trust Rules (Milestone 2)

1. **Numerical answers must originate from the deterministic catalogue
   engine.** The LLM may interpret questions but may not invent, calculate,
   or alter answers.
2. **Never silently resolve ambiguous queries.** Ambiguity produces
   `NEEDS_CLARIFICATION` with candidate interpretations.
3. **Never invent catalogue dimensions.** All dimension values must come from
   the controlled vocabulary built from the audited workbook; aliases may
   only resolve to canonical catalogue values.
4. **Never fabricate raw RF measurements.** The ANSWER text is the verbatim
   stored row content.
5. **Every answer must be traceable to a catalogue row ID.** `ANSWERED`
   results must carry `row_id`.
6. **Unsupported questions must produce `NOT_FOUND` or
   `NEEDS_CLARIFICATION`** — never a guessed or default answer.
7. **New parser capabilities require regression tests**, including gold-set
   evaluation items with audited row IDs as expectations.
8. **Invalid structured LLM output must fail validation** (Pydantic +
   controlled-vocabulary checks). No free-form JSON, no prose parsing, no
   regex extraction of model output.

## Permanent Project Rules (Milestone 3 — terminal chatbot)

9. **Status semantics are fixed.** `NEEDS_CLARIFICATION` = ambiguous or
   missing information; `NOT_FOUND` = specific request for something that
   does not exist; `UNSUPPORTED` = request type outside catalogue
   capabilities; `PARSE_ERROR` = malformed/invalid LLM structured output;
   `EXACT_MATCH`/`AMBIGUOUS` are the deterministic lookup outcomes. Do not
   collapse one status into another.
10. **Catalogue REQUEST values are authoritative.** Worst / Minimum / Best /
    Maximum are never merged. Natural-language variants ("lowest",
    "highest") clarify with candidates until an office-confirmed business
    rule exists (see `OFFICE_REVIEW.md`).
11. **Metric display-name precedence:** exact canonical token > exact
    unambiguous (longest-match) alias > LLM interpretation. Bare hypernyms
    ("gain") clarify between GAIN and CGAIN. Never use raw substring
    matching for metrics.
12. **Scope and unit are never invented.** Guessed values are vetoed;
    a missing dimension is filled ONLY when the audited catalogue uniquely
    determines it (documented, tested inference) — otherwise clarification.
13. **REQUEST vs FREQUENCY_SELECTION are separate dimensions.** Frequency
    wording must never become a REQUEST value.
14. **The structured schema must remain provider-strict-compatible**
    (`anyOf`, not `oneOf`; OpenAI strict structured output rejects `oneOf`
    with HTTP 400).
15. **Live LLM evaluation stays separate from pytest.** The offline suite
    never requires network access or an API key. Live accuracy claims
    require an actually-completed live run; otherwise label results as
    NOT YET MEASURED.
16. **Secrets are never logged.** Debug artifacts must be sanitized; the
    API key lives only in the environment / `.env` (git-ignored).

## Web application rules (Milestone 4)

17. **The web API is a thin, stateless wrapper around
    `RfCatalogueService`.** Answers, statuses, and clarification logic
    live in the service/parser/engine — never in `api.py` or the frontend.
    Status semantics from rule 9 apply to HTTP responses unchanged.
18. **Conversation continuity is server-side.** Sessions carry a
    `ConversationContext` keyed by an opaque `session_id` (in-memory,
    TTL+size-capped store). The frontend stores only the session id and
    renders backend results verbatim; do not move context logic client-side.
19. **Secrets never reach the browser.** `/api/status` exposes
    provider/model names only. No key material is logged, serialized, or
    embedded in responses.
20. **Provider errors are not model failures.** Timeouts, rate limits,
    auth and credit errors surface as friendly `ERROR` results and are
    excluded from accuracy denominators; they are never retried
    blindly and never converted into answers.
21. **No RAG/vector/embedding architecture** unless an explicit future
    requirement replaces this rule — the deterministic lookup is the
    trusted execution layer.
22. **A stored answer of "No valid matching records were found." is a
    valid ANSWERED payload** (the catalogue row exists); do not convert
    it to NOT_FOUND.
