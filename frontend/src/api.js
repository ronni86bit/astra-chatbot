const BASE = import.meta.env.VITE_API_BASE_URL || ''

const STATUS_LABELS = {
  ANSWERED: 'Answered',
  NEEDS_CLARIFICATION: 'Needs clarification',
  NOT_FOUND: 'Not found',
  UNSUPPORTED: 'Unsupported',
  PARSE_ERROR: 'Parse error',
  ERROR: 'Error',
}

async function api(path, options = {}) {
  const resp = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`
    try {
      const body = await resp.json()
      if (body.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch { /* keep default detail */ }
    throw new Error(detail)
  }
  return resp.json()
}

export { api, STATUS_LABELS }
