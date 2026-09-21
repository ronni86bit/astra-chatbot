# Office Review Items — Unresolved Business Semantics

These are catalogue/business questions the deterministic engine and the NL
parser deliberately DO NOT answer by guessing. The system behaves
conservatively (clarification or verbatim catalogue semantics) until an
office subject-matter expert confirms a rule. Once confirmed, encode the
rule in `src/rf_catalogue/nlp/vocabulary.py` (request variants) or
`src/rf_catalogue/query_intent.py` (qualifier semantics) with a regression
test.

| # | Item | Candidates / question | Current behaviour |
|---|------|----------------------|-------------------|
| 1 | **Worst vs Minimum** — does "lowest gain" mean the `Minimum` request (smallest measured value) or the `Worst` request (worst-case run)? | `Minimum` vs `Worst` | "lowest"/"smallest" → NEEDS_CLARIFICATION listing both |
| 2 | **Best vs Maximum** — does "highest gain" mean `Maximum` or `Best`? | `Maximum` vs `Best` | "highest"/"largest" → NEEDS_CLARIFICATION listing both |
| 3 | **Desired Channel Power vs Channel Power** (TNP) — what physically distinguishes the "desired" channel power measurement from the plain channel power measurement? | `DESIRED_CHANNEL_POWER` vs `CHANNEL_POWER` | Both preserved as distinct controlled qualifiers; wording is never merged |
| 4 | **Channel Noise Power semantics** (NDCP) — confirm "channel noise power" is the intended reference quantity for noise-plus-distortion differences | `CHANNEL_NOISE_POWER` | Preserved verbatim; used as the reference qualifier |
| 5 | **output-to-input semantics** (OP1DB / IPSAT) — should "output-to-input" be modelled as a reference qualifier or an explicit direction pair? | `OUTPUT_TO_INPUT` | Preserved verbatim as a qualifier |
| 6 | **CP subtraction operand ordering** — "Show channel power minus desired channel power": confirm canonical minuend/subtrahend ordering | qualifier = `DESIRED_CHANNEL_POWER` | Subtrahend captured as the reference; wording preserved |
| 7 | **"Where is …" scope convention** — catalogue-wide threshold/closest/range questions ("Where is X above 0.5 dB?") carry scope `All nodes` only as a workbook column, never in the question text. Confirm the rule: interrogative "Where" ⇒ `All nodes` when the metric/request family offers {All nodes, Final node}. | `All nodes` vs `Final node` | Without the rule the system asks which scope is meant |
| 8 | **Noise-reference family metric** — audit correction: the "noise plus distortion power above channel noise power" family belongs to metric **NDCP** (not CNDR as once assumed). Confirm. | NDCP | Parser maps the phrase to NDCP (audited grammar) |
| 9 | **Final-node semantics when context is part-specific** — if a user asks about a part and then says "and the final node?", confirm how part-scope context should interact with `Final node` | n/a | Context never overrides an explicit part scope |
| 10 | **Bare-frequency question ⇒ which REQUEST?** — "What is Cascaded Gain at 1500 MHz for the final node?" matches TWO valid catalogue records: `Particular frequency` (row 161) and `Individual frequency final value` (row 789). Which does the office intend when a user asks a bare frequency question? (Discovered in live acceptance testing M05/M06/M15/M24.) | `Particular frequency` vs `Individual frequency final value` | Either valid record is accepted by the acceptance runner (documented alternative outcome); a conservative build may clarify |

Source registries in code:
- `rf_catalogue.query_intent.OFFICE_REVIEW_ITEMS` (qualifier semantics)
- `rf_catalogue.nlp.vocabulary.REQUEST_VARIANTS_UNDER_REVIEW` (request variants)
