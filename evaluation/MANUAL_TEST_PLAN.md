# Manual End-to-End Test Plan — RF/SystemVue Terminal Chatbot

Prerequisites:
- `pip install -e .[llm]`
- `.env` in the project root with a funded `OPENROUTER_API_KEY`
- `python -m rf_catalogue.chatbot`

Legend: each case lists the INPUT, the EXPECTED STATUS
(`ANSWERED` / `NEEDS_CLARIFICATION` / `NOT_FOUND` / `UNSUPPORTED` /
`PARSE_ERROR`), and the expected catalogue row ID where applicable
(`expected row ID == resolved row ID` is the success criterion).
Verify with `/debug` after each case where useful.

Start every session with `/reset`.

| # | Area | Input | Expected status | Expected row / outcome |
|---|------|-------|-----------------|------------------------|
| 1 | straightforward best | What is the best Mismatch Loss in the final node? | ANSWERED | row 1 |
| 2 | straightforward worst | What is the worst Cascaded Gain in the final node? | ANSWERED | row 10 |
| 3 | maximum | What is the Maximum Mismatch Loss in the final node? | ANSWERED | row 3 |
| 4 | minimum (canonical word) | What is the Minimum Cascaded Gain in the final node? | ANSWERED | row 12 (CGAIN Minimum) |
| 5 | exact frequency | What is the Cascaded Gain at 1500 MHz for the final node? | ANSWERED | CGAIN Individual frequency final value @1500 (789) — or NEEDS_CLARIFICATION listing the two valid requests. Live note (acceptance round): the assistant may also ANSWER with the Particular-frequency record (161) — both are valid catalogue records for this phrasing; the Particular-frequency vs Individual-frequency-final-value distinction is office-review item 10. |
| 6 | GHz input | What is the Desired Channel Power at 2.5 GHz in the final node? | ANSWERED or NEEDS_CLARIFICATION | Frequency must display as 2500 MHz (never 2.5 GHz). Row per DCP@2500 catalogue content (Particular frequency row = 191). |
| 7 | part query | Show Mismatch Loss for part ADL8124 across frequency. | ANSWERED | row 3227 (Part summary / ADL8124 / All Frequencies) |
| 8 | node query | Give me the node summary for mismatch loss at node 2. | ANSWERED | row 3276 (Node summary / scope 2) |
| 9 | threshold | Where is Mismatch Loss above 0.5 dB in All nodes? | ANSWERED | row 2853 (threshold 0.5) |
| 10 | threshold distinction | Where is Mismatch Loss above 10 dB in All nodes? | ANSWERED | row 2868 (threshold 10 — NOT 2853) |
| 11 | range | Where is Cascaded Compression between 0.1 and 0.5 dB20? | ANSWERED | row 3084 (All nodes) |
| 12 | closest value | Where is Cascaded Compression closest to 0.1 dB20? | ANSWERED | row 3071 (target 0.1) |
| 13 | TNP reference qualifier (desired) | Show total node power above desired channel power at individual frequency 1200 MHz? | ANSWERED | row 2421 |
| 14 | TNP reference qualifier (plain) | Show total node power above channel power at individual frequency 1200 MHz? | ANSWERED | row 2423 (different from #13) |
| 15 | ambiguous metric | What is the gain at 2500 MHz? | NEEDS_CLARIFICATION | Candidates must include GAIN and CGAIN; no answer is guessed. Reply "CGAIN" → the assistant should continue with CGAIN (pending clarification). |
| 16 | ambiguous request variant | Give me the lowest cascade gain at the final node. | NEEDS_CLARIFICATION | Candidates include Minimum and Worst; never silently one of them. |
| 17 | ambiguous scope | What is the best mismatch loss in the ADL8124 family? | NEEDS_CLARIFICATION | Candidates include ADL8124 and ADL8124_1. |
| 18 | missing threshold | Where is Mismatch Loss above the limit? | NEEDS_CLARIFICATION | Missing threshold requested. |
| 19 | unknown part | What is the best mismatch loss for part FOO123? | NOT_FOUND | Message names FOO123. |
| 20 | unknown node | What is the best CGAIN in node 99? | NOT_FOUND | Message names node 99. |
| 21 | unknown frequency | What is the best CGAIN at 3400 MHz? | NOT_FOUND | Message names 3400 MHz. |
| 22 | unsupported request | Plot the Cascaded Gain over time for the final node. | UNSUPPORTED | Plotting is outside the catalogue. |
| 23 | follow-up inheritance | (a) What is the worst CGAIN in the final node? → (b) What about the best? | both ANSWERED | (a) row 10; (b) inherits CGAIN/Final node/All Frequencies/dB, request Best; different row. |
| 24 | clarification follow-up | (a) case 15 → (b) reply: CGAIN | (b) proceeds with CGAIN | `/debug` shows metric resolved from clarification. |
| 25 | reset | After any answered question: /reset then /debug | context empty | "/debug" reports no question answered; a follow-up like "What about the best?" no longer resolves. |
| 26 | invalid command | /frobnicate | graceful | "Unknown command" message; no crash. |
| 27 | empty input | press Enter | graceful | New prompt, no crash. |
| 28 | unknown metric | What is the return loss in the final node? | NOT_FOUND | Return loss is not a catalogue metric. |
| 29 | wrong-unit combination | Show the total node power above channel power difference in dB. | NOT_FOUND | TNP exists only in dBm; explicit user unit is respected and reported. |
| 30 | missing key behaviour | Remove/clear the provider API key (e.g. GROQ_API_KEY), restart the chatbot, ask a question | graceful | Startup explains the LLM layer is unavailable; commands still work; question explains what to configure. Restore the key afterwards. |

Pass criteria: statuses match, row IDs match where applicable, answers are
verbatim catalogue content, provenance cites `Source row: <ID>`, and the
chatbot never crashes. Log any deviation with the exact input text.
