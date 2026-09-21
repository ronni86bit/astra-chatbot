"""HTTP API for the RF/SystemVue Question Assistant.

FastAPI application wrapping ``RfCatalogueService`` with per-session
``ConversationContext`` so conversational follow-ups and clarification
replies work across HTTP requests exactly as they do in the terminal
chatbot.

Run (development):

    python -m rf_catalogue.api                    # 127.0.0.1:8000
    uvicorn rf_catalogue.api:app --reload         # equivalent

The service is built once at startup (workbook load takes a few
seconds). All trust rules from the terminal chatbot carry over
unchanged:

- Answers are ALWAYS the verbatim catalogue row content, cited by
  ``row_id``; the LLM only interprets the question.
- Ambiguous or missing information produces NEEDS_CLARIFICATION.
- Secrets (API keys) are never logged, returned, or echoed.

Environment variables (all optional):

    CORS_ORIGINS    comma-separated allowed origins for browser access
                    (default: local Vite/dev origins)
    API_HOST        bind host for ``python -m rf_catalogue.api``
    API_PORT        bind port for ``python -m rf_catalogue.api``
    SESSION_TTL     seconds before an idle session is dropped (default 7200)
"""

from __future__ import annotations

import os
import secrets as _secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from rf_catalogue.nlp.context import ConversationContext
from rf_catalogue.service import AnswerResult, AnswerStatus, RfCatalogueService

#: Built frontend (created by `npm run build` in frontend/). When present it
#: is served by the API itself, so `python -m rf_catalogue.api` alone is a
#: complete single-process deployment. Production setups may instead serve
#: the dist via nginx (deploy/nginx.conf) and run the API behind it.
_FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

#: Hard cap on a single chat message (catalogue questions are short).
MAX_MESSAGE_CHARS = 1000

#: Default CORS origins: the Vite dev server and common local ports.
DEFAULT_CORS_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)

#: Representative catalogue question row IDs (same set as /examples in
#: the terminal chatbot).
_EXAMPLE_ROW_IDS = (1, 10, 2850, 3227, 178, 4772)

_NO_SERVICE_MESSAGE = (
    "The LLM interpretation layer is not available, so questions cannot "
    "be answered right now. Configure your provider (LLM_PROVIDER and "
    "the matching API key, e.g. GROQ_API_KEY) in the backend .env "
    "(see .env.example) and restart the server."
)


class ChatRequest(BaseModel):
    """One chat turn.

    Canonical fields are ``message`` + ``session_id``; the task-style
    aliases ``question`` + ``conversation_id`` are accepted for contract
    compliance (POST /api/query). At least one message field is required.
    """

    message: str | None = Field(default=None, min_length=1,
                                max_length=MAX_MESSAGE_CHARS)
    session_id: str | None = Field(default=None, min_length=8, max_length=64)
    question: str | None = Field(default=None, min_length=1,
                                 max_length=MAX_MESSAGE_CHARS)
    conversation_id: str | None = Field(default=None, min_length=8,
                                        max_length=64)

    @model_validator(mode="after")
    def _resolve_aliases(self) -> "ChatRequest":
        if self.message is None and self.question is not None:
            self.message = self.question
        if self.message is None:
            raise ValueError("Provide a non-empty 'message' (or 'question').")
        if self.session_id is None and self.conversation_id is not None:
            self.session_id = self.conversation_id
        return self


class ResetRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)


class _Session:
    """One browser conversation: its context plus a lock (threadpool)."""

    __slots__ = ("context", "last_seen", "lock")

    def __init__(self) -> None:
        self.context = ConversationContext()
        self.last_seen = time.monotonic()
        self.lock = threading.Lock()


class SessionStore:
    """In-memory session store with TTL eviction and a size cap.

    Deliberately simple: an internal office tool with a single backend
    process. No persistence is needed - a restart simply starts fresh
    conversations, which is also the safest failure mode for context
    inheritance.
    """

    def __init__(self, ttl_seconds: int = 7200, max_sessions: int = 500):
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self._sessions: dict[str, _Session] = {}
        self._guard = threading.Lock()

    def get_or_create(self, session_id: str | None) -> tuple[str, _Session]:
        with self._guard:
            self._evict_expired()
            if session_id and session_id in self._sessions:
                session = self._sessions[session_id]
            else:
                session_id = _secrets.token_urlsafe(24)
                session = _Session()
                self._sessions[session_id] = session
                if len(self._sessions) > self.max_sessions:
                    self._evict_oldest()
            session.last_seen = time.monotonic()
            return session_id, session

    def reset(self, session_id: str) -> bool:
        with self._guard:
            session = self._sessions.get(session_id)
            if session is None:
                return False
            session.context.reset()
            return True

    def _evict_expired(self) -> None:
        cutoff = time.monotonic() - self.ttl_seconds
        stale = [sid for sid, s in self._sessions.items()
                 if s.last_seen < cutoff]
        for sid in stale:
            del self._sessions[sid]

    def _evict_oldest(self) -> None:
        oldest = min(self._sessions, key=lambda sid: self._sessions[sid].last_seen)
        del self._sessions[oldest]


def serialize_candidates(candidates: list) -> list[dict[str, Any]]:
    """Clarification candidate intents -> chips with a short reply label.

    Candidates differ in exactly one dimension; the chip label is that
    dimension's value, so clicking (or retyping) it is exactly the
    documented clarification-reply flow that resumes the pending draft.
    """
    if not candidates:
        return []
    dims = ("metric", "request", "scope", "frequency_selection", "unit")
    varying = None
    for dim in dims:
        if len({getattr(c, dim) for c in candidates}) > 1:
            varying = dim
            break
    return [
        {
            "label": getattr(c, varying) if varying else c.intent_key(),
            "intent_key": c.intent_key(),
        }
        for c in candidates
    ]


def serialize_result(result: AnswerResult) -> dict[str, Any]:
    """AnswerResult -> JSON-safe dict (never includes secrets).

    Mirrors the terminal renderer: verbatim answer text, row provenance,
    resolved intent, and clarification candidates.
    """
    intent = None
    if result.intent is not None:
        params_key = result.intent.params.key()
        intent = {
            "metric": result.intent.metric,
            "request": result.intent.request,
            "scope": result.intent.scope,
            "frequency_selection": result.intent.frequency_selection,
            "unit": result.intent.unit,
            "params": None if params_key[0] == "none" else list(params_key),
        }
    return {
        "status": result.status.value,
        "message": result.message,
        "answer_text": result.answer_text,
        "question_text": result.question_text,
        "row_id": result.row_id,
        "metric": result.metric,
        "request": result.request,
        "scope": result.scope,
        "unit": result.unit,
        "frequency_selection": result.frequency_selection,
        "answer_type": result.answer_type,
        "intent": intent,
        "missing_fields": result.missing_fields,
        "candidate_hints": result.candidate_hints,
        "candidates": serialize_candidates(result.candidates),
    }


def _llm_info(service: RfCatalogueService | None) -> dict[str, Any]:
    """Provider/model info without secrets (mirrors terminal /status)."""
    if service is None:
        return {"provider": None, "model": None, "live_mode": False}
    client = getattr(service.parser, "llm", None)
    model = getattr(client, "model", None)
    provider = getattr(client, "provider", None)
    return {
        "provider": provider,
        "model": model,
        "live_mode": bool(model),
    }


def _handle_command(command: str, store: SessionStore,
                    session: _Session) -> dict[str, Any] | None:
    """Terminal-parity slash commands. Returns a result dict or None."""
    cmd = command.strip().lower()
    if cmd in ("/reset", "/clear"):
        session.context.reset()
        return {
            "status": AnswerStatus.ANSWERED.value,
            "message": "Conversation context cleared.",
        }
    if cmd in ("/help", "/examples", "/status", "/debug", "/quit", "/exit", "/q"):
        return {
            "status": AnswerStatus.UNSUPPORTED.value,
            "message": (
                f"{cmd} is a terminal-only command. The web interface "
                "provides examples, status and reset as buttons."
            ),
        }
    return {
        "status": AnswerStatus.UNSUPPORTED.value,
        "message": f"Unknown command: {command.strip()} (try asking a "
                   "catalogue question instead).",
    }


def create_app(
    service: RfCatalogueService | None = None,
    cors_origins: list[str] | None = None,
    session_ttl: int | None = None,
) -> FastAPI:
    """App factory. Tests inject a FakeLLMClient-backed service; production
    builds the real service once at startup."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if service is None:
            try:
                app.state.service = RfCatalogueService()
            except RuntimeError:
                # Degraded mode: API stays up for status/health/examples;
                # chat returns explicit guidance (mirrors terminal).
                app.state.service = None
            except Exception:
                raise
        else:
            app.state.service = service
        yield

    app = FastAPI(
        title="RF/SystemVue Question Assistant API",
        version="1.0.0",
        description=(
            "Answers natural-language questions with the exact stored "
            "catalogue answer (row-cited). The LLM only interprets "
            "questions; it never generates answers."
        ),
        lifespan=lifespan,
    )

    if cors_origins is None:
        raw = os.environ.get("CORS_ORIGINS", "")
        cors_origins = ([o.strip() for o in raw.split(",") if o.strip()]
                        if raw else list(DEFAULT_CORS_ORIGINS))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    ttl = session_ttl or int(os.environ.get("SESSION_TTL", "7200"))
    app.state.store = SessionStore(ttl_seconds=ttl)

    @app.get("/api/health")
    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/status")
    def status() -> dict:
        service_ref = app.state.service
        info = {"catalogue_loaded": service_ref is not None}
        if service_ref is not None:
            engine = service_ref.engine
            info.update({
                "catalogue_rows": engine.total_rows(),
                "unique_lookup_keys": engine.unique_lookup_keys(),
                "intent_keys": engine.intent_unique_keys(),
                "intent_duplicate_keys": engine.intent_duplicate_keys(),
            })
        info.update(_llm_info(service_ref))
        return info

    @app.get("/api/examples")
    def examples() -> dict:
        service_ref = app.state.service
        questions = [
            "What is the best Mismatch Loss in the final node?",
            "Where is Mismatch Loss above 0.5 dB?",
        ]
        if service_ref is not None:
            by_id = {r.row_id: r for r in service_ref.engine._catalogue}
            fetched = [by_id[rid].question for rid in _EXAMPLE_ROW_IDS
                       if rid in by_id]
            if fetched:
                questions = fetched
        return {"examples": questions}

    @app.post("/api/chat")
    @app.post("/api/query")
    def chat(request: ChatRequest) -> dict:
        session_id, session = app.state.store.get_or_create(
            request.session_id)
        message = request.message.strip()

        # Terminal-parity slash commands (e.g. "/reset" typed in the box).
        if message.startswith("/"):
            outcome = _handle_command(message, app.state.store, session)
            return {"session_id": session_id, "result": outcome}

        service_ref = app.state.service
        if service_ref is None:
            return {
                "session_id": session_id,
                "result": {
                    "status": AnswerStatus.ERROR.value,
                    "message": _NO_SERVICE_MESSAGE,
                },
            }

        with session.lock:
            result = service_ref.answer_question(message, session.context)
        return {
            "session_id": session_id,
            "result": serialize_result(result),
        }

    @app.post("/api/reset")
    def reset(request: ResetRequest) -> dict:
        ok = app.state.store.reset(request.session_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Unknown session.")
        return {"session_id": request.session_id, "reset": True}

    # -- Static frontend (single-process deployment) ----------------------
    # Registered LAST so it never shadows /api routes or /docs. With the
    # dist present, the API serves the React app and the SPA fallback.
    if _FRONTEND_DIST.is_dir():
        assets = _FRONTEND_DIST / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str) -> FileResponse:
            candidate = (_FRONTEND_DIST / full_path).resolve()
            if (full_path
                    and str(candidate).startswith(str(_FRONTEND_DIST))
                    and candidate.is_file()):
                return FileResponse(candidate)
            return FileResponse(_FRONTEND_DIST / "index.html")

    return app


def main() -> int:
    """Run the API with uvicorn (development entry point)."""
    import uvicorn

    host = os.environ.get("API_HOST", "127.0.0.1")
    port = int(os.environ.get("API_PORT", "8000"))
    uvicorn.run("rf_catalogue.api:create_app_factory", factory=True,
                host=host, port=port)


def create_app_factory() -> FastAPI:
    """Uvicorn factory target - builds the app with the real service."""
    return create_app()


if __name__ == "__main__":
    raise SystemExit(main())
