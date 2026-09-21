import { useEffect, useRef, useState } from 'react'
import { api, STATUS_LABELS } from './api.js'

const SESSION_KEY = 'rf-assistant-session'

let nextId = 1

function statusBadge(status) {
  const cls = {
    ANSWERED: 'badge ok',
    NEEDS_CLARIFICATION: 'badge clarify',
    NOT_FOUND: 'badge warn',
    UNSUPPORTED: 'badge warn',
    PARSE_ERROR: 'badge error',
    ERROR: 'badge error',
  }[status] || 'badge'
  return <span className={cls}>{STATUS_LABELS[status] || status}</span>
}

function IntentCard({ intent }) {
  if (!intent) return null
  const rows = [
    ['Metric', intent.metric],
    ['Request', intent.request],
    ['Scope', intent.scope],
    ['Frequency', intent.frequency_selection],
    ['Unit', intent.unit],
  ].filter(([, v]) => v)
  if (intent.params) rows.push(['Params', intent.params.join(' · ')])
  return (
    <div className="intent-card">
      <div className="intent-title">Resolved intent</div>
      {rows.map(([k, v]) => (
        <div key={k} className="intent-row">
          <span className="intent-key">{k}</span>
          <span className="intent-val">{v}</span>
        </div>
      ))}
    </div>
  )
}

function AssistantMessage({ msg, onCandidate }) {
  const r = msg.result || {}
  const answered = r.status === 'ANSWERED'
  return (
    <div className="msg assistant">
      <div className="msg-head">
        {statusBadge(r.status)}
        {r.row_id != null && (
          <span className="source">Source: catalogue row {r.row_id}</span>
        )}
      </div>

      {answered && r.answer_text && (
        <div className="answer-text">{r.answer_text}</div>
      )}
      {answered && r.question_text && (
        <div className="cat-question">Catalogue question: “{r.question_text}”</div>
      )}

      {!answered && r.message && <div className="msg-text">{r.message}</div>}

      {r.candidate_hints && r.candidate_hints.length > 0 && (
        <ul className="hints">
          {r.candidate_hints.map((h, i) => (
            <li key={i}>{h}</li>
          ))}
        </ul>
      )}

      {r.candidates && r.candidates.length > 0 && (
        <div className="chips">
          {r.candidates.map((c, i) => (
            <button key={i} className="chip" onClick={() => onCandidate(c.label)}>
              {c.label}
            </button>
          ))}
        </div>
      )}

      <IntentCard intent={r.intent} />
    </div>
  )
}

export default function App() {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [examples, setExamples] = useState([])
  const [status, setStatus] = useState(null)
  const sessionRef = useRef(null)
  const listRef = useRef(null)

  useEffect(() => {
    sessionRef.current = localStorage.getItem(SESSION_KEY)
    api('/api/examples').then((d) => setExamples(d.examples || [])).catch(() => {})
    api('/api/status').then(setStatus).catch(() => {})
  }, [])

  useEffect(() => {
    listRef.current?.scrollTo(0, listRef.current.scrollHeight)
  }, [messages, busy])

  async function send(text) {
    const message = text.trim()
    if (!message || busy) return
    setError(null)
    setMessages((m) => [...m, { id: nextId++, role: 'user', text: message }])
    setInput('')
    setBusy(true)
    try {
      const body = { message }
      if (sessionRef.current) body.session_id = sessionRef.current
      const data = await api('/api/chat', {
        method: 'POST',
        body: JSON.stringify(body),
      })
      sessionRef.current = data.session_id
      localStorage.setItem(SESSION_KEY, data.session_id)
      setMessages((m) => [...m, { id: nextId++, role: 'assistant', result: data.result }])
    } catch (e) {
      setError(`Request failed: ${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  async function reset() {
    try {
      if (sessionRef.current) {
        await api('/api/reset', {
          method: 'POST',
          body: JSON.stringify({ session_id: sessionRef.current }),
        })
      }
    } catch { /* session may already be gone; clear locally anyway */ }
    sessionRef.current = null
    localStorage.removeItem(SESSION_KEY)
    setMessages([])
    setError(null)
  }

  // Candidate chips carry their own full label; sending it verbatim is the
  // documented clarification-reply flow ("CGAIN" resumes the pending draft).
  const onCandidate = (label) => send(label)

  return (
    <div className="layout">
      <aside className="sidebar">
        <h1 className="brand">RF/SystemVue<br />Question Assistant</h1>
        <p className="tagline">
          Answers come verbatim from the SystemVue RF catalogue, cited by row.
          The assistant asks instead of guessing.
        </p>

        <h2>Examples</h2>
        <div className="examples">
          {examples.map((q, i) => (
            <button key={i} className="example" onClick={() => send(q)}>
              {q}
            </button>
          ))}
          {examples.length === 0 && <span className="muted">Loading…</span>}
        </div>

        <h2>Status</h2>
        <div className="status-box">
          {status ? (
            <>
              <div>Catalogue rows: <b>{status.catalogue_rows ?? '—'}</b></div>
              <div>Provider: <b>{status.provider ?? 'n/a'}</b></div>
              <div>Model: <b>{status.model ?? 'n/a'}</b></div>
            </>
          ) : (
            <span className="muted">Unavailable</span>
          )}
        </div>

        <button className="reset" onClick={reset}>Reset conversation</button>
      </aside>

      <main className="chat">
        <div className="messages" ref={listRef}>
          {messages.length === 0 && (
            <div className="empty">
              Ask a question about the RF catalogue, or pick an example.
            </div>
          )}
          {messages.map((m) =>
            m.role === 'user' ? (
              <div key={m.id} className="msg user">{m.text}</div>
            ) : (
              <AssistantMessage key={m.id} msg={m} onCandidate={onCandidate} />
            ),
          )}
          {busy && <div className="msg assistant typing">Thinking…</div>}
        </div>

        {error && <div className="error-banner">{error}</div>}

        <form
          className="composer"
          onSubmit={(e) => { e.preventDefault(); send(input) }}
        >
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              // Explicit Enter-to-send: do not rely on implicit form
              // submission, which some embedded browsers suppress.
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault()
                send(input)
              }
            }}
            placeholder="e.g. What is the worst Cascaded Gain in the final node?"
            disabled={busy}
            autoFocus
          />
          <button type="submit" disabled={busy || !input.trim()}>Send</button>
        </form>
      </main>
    </div>
  )
}
