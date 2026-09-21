import { useEffect, useRef, useState } from 'react'
import { api, STATUS_LABELS } from './api.js'

const SESSION_KEY = 'rf-assistant-session'
const THEME_KEY = 'rf-assistant-theme'

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

function CopyButton({ getText }) {
  const [copied, setCopied] = useState(false)
  const timer = useRef(null)

  useEffect(() => () => clearTimeout(timer.current), [])

  async function copy() {
    const text = getText()
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      // Clipboard API unavailable (permissions / non-secure context).
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      try { document.execCommand('copy') } finally { ta.remove() }
    }
    setCopied(true)
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setCopied(false), 1600)
  }

  return (
    <button className="copy-btn" onClick={copy} title="Copy to clipboard">
      {copied ? 'Copied' : 'Copy'}
    </button>
  )
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
  // Copy the user-visible output only — never intent/debug metadata.
  const copyText = answered
    ? r.answer_text || ''
    : [r.message, ...(r.candidate_hints || [])].filter(Boolean).join('\n')
  return (
    <div className="msg assistant">
      <div className="msg-head">
        {statusBadge(r.status)}
        {r.row_id != null && (
          <span className="source">Source: catalogue row {r.row_id}</span>
        )}
        <span className="spacer" />
        <CopyButton getText={() => copyText} />
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
  const [theme, setTheme] = useState(() =>
    document.documentElement.dataset.theme || 'dark')
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

  function toggleTheme() {
    const next = theme === 'dark' ? 'light' : 'dark'
    setTheme(next)
    document.documentElement.dataset.theme = next
    localStorage.setItem(THEME_KEY, next)
  }

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

  const themeBtn = (
    <button className="ghost-btn" onClick={toggleTheme}>
      {theme === 'dark' ? 'Light mode' : 'Dark mode'}
    </button>
  )

  return (
    <div className="layout">
      <header className="mobilebar">
        <span className="mobile-title">RF/SystemVue Assistant</span>
        {themeBtn}
        <button className="ghost-btn" onClick={reset}>New chat</button>
      </header>

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

        <div className="sidebar-actions">
          {themeBtn}
          <button className="reset" onClick={reset}>New chat / reset</button>
        </div>
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
          <textarea
            value={input}
            rows={1}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              // Enter sends; Shift+Enter inserts a newline (composed
              // input, e.g. IME, is never hijacked).
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
