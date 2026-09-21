"""Natural-language parsing layer for the RF catalogue (milestone 2).

Provider-agnostic LLM structured parsing constrained by the audited
catalogue vocabulary, with deterministic Stage-A normalization and
strict Pydantic validation into QueryIntent. No RAG, no vector search,
no answer generation by the LLM.
"""
