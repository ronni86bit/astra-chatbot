# PROJECT_CONTEXT — RF/SystemVue Question Assistant

Permanent architecture and decisions, so a future contributor (or coding
agent) can work on this project without replaying history. The current
repository and its tests are authoritative; historical conversation logs
are secondary context only.

## What this is

An internal office tool that answers natural-language questions about the
SystemVue RF Excel catalogue (`SystemVue_RF_Unique_Question_Assistant_V63.xlsx`,
4,768 audited Q&A rows). Users get the **exact stored answer** for a
question, cited by catalogue row ID. Ambiguity is surfaced, never guessed.

## Non-negotiable architecture

```
question → conversation context → Stage A deterministic normalization
→ Stage B LLM structured interpretation (JSON-schema constrained)
→ QueryIntent validation (Pydantic + controlled vocabulary)
→ intent resolution (guess veto + catalogue-unique inference)
→ deterministic catalogue lookup → verbatim stored ANSWER (row-cited)
```

- The catalogue is the ONLY source of answers. The LLM interprets; it
  never invents, computes, or alters answers.
- No RAG / vector search / embeddings / semantic retrieval / web search.
- Canonical intent = 5 audited base dimensions (metric, request, scope,
  frequency_selection, unit) **+ request-aware structured params**
  (`ThresholdParams`, `ClosestTargetParams`, `RangeParams`,
  `ReferenceQualifierParams`, `NoParams`). The raw 5-field key is
  intentionally non-unique (80 keys / 417 rows); `params` disambiguates.
  Do NOT revert to 5-field-only lookup.
- Status semantics are fixed and never collapsed: ANSWERED (EXACT_MATCH),
  NEEDS_CLARIFICATION (AMBIGUOUS), NOT_FOUND, UNSUPPORTED, PARSE_ERROR.
  A stored answer of "No valid matching records were found." (e.g. row
  2868) is a valid ANSWERED payload — do not convert it to NOT_FOUND.
- `anyOf` (never `oneOf`) in the LLM schema: OpenAI-strict structured
  output rejects `oneOf` with HTTP 400.
- Metric precedence: exact canonical token > longest unambiguous alias >
  LLM interpretation. Bare "gain" clarifies between GAIN and CGAIN.
- REQUEST values are authoritative: Worst/Minimum/Best/Maximum are never
  merged (see `OFFICE_REVIEW.md` for the open mappings).

## Conversational context policy (validated by tests)

- **Explicit elliptical follow-ups only** ("What about the best?", "And
  at 2600?", "Show the same for node 45.", short fragments): dimensions
  the new message leaves null are inherited from the last ANSWERED
  intent — including `request`; **params are never inherited** (if the
  inherited request needs a parameter, the assistant asks).
- A grammatically complete question is a NEW intent: nothing is silently
  inherited; missing dimensions are filled only when the catalogue
  uniquely determines them, otherwise the assistant clarifies.
- ALL clarification paths (missing dims, candidate lists, ambiguous
  request wording, missing params) store a pending draft in
  `ConversationContext`, so a short reply ("CGAIN", "Final node",
  "Minimum") resumes deterministically without a new LLM call.
- The guess veto nulls model values that are not literally in the user
  text — with one precision: a bare number that IS a catalogue frequency
  selection ("And at 2600?") counts as a literal frequency statement.
- `/reset` (terminal) and New chat (web) clear intent, pending draft,
  and context.

## Layers

| layer | file(s) | rule |
|---|---|---|
| Deterministic engine | `src/rf_catalogue/engine.py` | sole answer source; `lookup_by_intent` returns EXACT_MATCH/AMBIGUOUS/miss |
| Validated intent | `src/rf_catalogue/query_intent.py` | strict Pydantic; rejects bool-as-number, NaN/inf, bad ranges, unknown vocab |
| NLP | `src/rf_catalogue/nlp/*` | normalizer, parser (stages above), vocabulary, context, provider-agnostic LLM adapter (`llm_client.py` is the only provider-specific code), structured schema |
| Service facade | `src/rf_catalogue/service.py` | maps parse/lookup outcomes to the status envelope; provider errors → friendly ERROR messages (never model failures) |
| Terminal | `src/rf_catalogue/chatbot.py` | `/help /status /debug /examples /reset /quit`; kept as the diagnostic interface |
| Web API | `src/rf_catalogue/api.py` | thin wrapper: sessions carry `ConversationContext`; POST `/api/chat` (= `/api/query`, accepts `message`/`question`, `session_id`/`conversation_id`), GET `/api/status`, `/api/examples`, `/api/health` (= `/health`); serves `frontend/dist` when present (single-process mode) |
| Frontend | `frontend/` | React 18 + Vite + JSX. Renders backend fields verbatim (answer text, row ID, intent, clarification chips); copy button on every assistant output; dark/light theme persisted; Enter sends, Shift+Enter newlines; `VITE_API_BASE_URL` for split deployments. No business logic client-side |
| Deployment | `deploy/`, `docker-compose.yml` | backend container + nginx frontend container; runtime secret injection; nginx proxies `/api` (same origin, no CORS) |

## Testing & evaluation

- `python -m pytest` — fully offline (FakeLLMClient, FastAPI TestClient).
  Live LLM evaluation is a separate harness (`rf_catalogue.evaluation`,
  smoke/core/full subsets) and never runs inside pytest. Provider/budget
  failures are excluded from accuracy denominators; never fabricate
  accuracy numbers (label NOT YET MEASURED otherwise).
- New parser behavior REQUIRES regression tests, with audited row IDs as
  expectations where applicable (see `tests/test_milestone_final.py`).

## Known open items

- `OFFICE_REVIEW.md` business rules (Worst vs Minimum, Best vs Maximum,
  DCP vs CP, CNP semantics, output-to-input, CP ordering, all-nodes
  convention, NDCP, part-context × final-node) — conservative behavior
  until confirmed.
- Public GitHub repository contains the office workbook — see
  DEPLOYMENT.md §8 blocker note.
- Web sessions are in-memory per process (single-worker deployment).
