import { useCallback, useEffect, useRef, useState } from 'react'
import Manuals from './Manuals.jsx'
import { API, WS } from '../api.js'


// Escalation reasons carry different operational meanings, so they are worth
// distinguishing at a glance in the queue. A spike in any one of them points
// at a different underlying problem — see docs/06-observability.md.
const REASON_META = {
  safety: { tone: 'bad', label: 'Safety' },
  restricted_part: { tone: 'bad', label: 'Technician only' },
  customer_request: { tone: 'info', label: 'Asked for a human' },
  no_coverage: { tone: 'warn', label: 'No manual' },
  low_retrieval_confidence: { tone: 'warn', label: 'Low confidence' },
  step_budget_exhausted: { tone: 'warn', label: 'Out of steps' },
  no_progress: { tone: 'warn', label: 'Going in circles' },
  tool_failures: { tone: 'bad', label: 'Tool failures' },
  customer_frustration: { tone: 'warn', label: 'Frustrated' },
  high_value_order: { tone: 'info', label: 'Needs approval' },
  order_failed: { tone: 'bad', label: 'Order failed' },
  no_part_match: { tone: 'warn', label: 'No part match' },
}

const currency = (cents) => `$${((cents || 0) / 100).toFixed(2)}`

export default function App() {
  const [queue, setQueue] = useState([])
  const [selected, setSelected] = useState(null)
  const [packet, setPacket] = useState(null)
  const [metrics, setMetrics] = useState(null)
  const [filter, setFilter] = useState('queued')
  const [view, setView] = useState('handoffs')
  const ws = useRef(null)

  const loadQueue = useCallback(async (status) => {
    const res = await fetch(`${API}/api/handoffs?status=${status}`)
    const data = await res.json()
    setQueue(data.handoffs || [])
  }, [])

  const loadMetrics = useCallback(async () => {
    const res = await fetch(`${API}/api/metrics`)
    setMetrics(await res.json())
  }, [])

  useEffect(() => { loadQueue(filter) }, [filter, loadQueue])
  useEffect(() => {
    loadMetrics()
    const t = setInterval(loadMetrics, 15000)
    return () => clearInterval(t)
  }, [loadMetrics])

  useEffect(() => {
    const socket = new WebSocket(`${WS}/ws/console`)
    socket.onmessage = () => loadQueue(filter)
    ws.current = socket
    return () => socket.close()
  }, [filter, loadQueue])

  const open = async (handoff) => {
    setSelected(handoff)
    const res = await fetch(`${API}/api/handoffs/${handoff.id}`)
    const data = await res.json()
    setPacket(data.packet)
  }

  const claim = async () => {
    await fetch(`${API}/api/handoffs/${selected.id}/claim`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent_name: 'You' }),
    })
    loadQueue(filter)
  }

  const resolve = async () => {
    await fetch(`${API}/api/handoffs/${selected.id}/resolve`, { method: 'POST' })
    setSelected(null); setPacket(null); loadQueue(filter)
  }

  return (
    <div className="shell">
      <header>
        <div className="brand">
          <span className="lockup">
            <svg className="mark" width="20" height="20" viewBox="0 0 24 24" aria-hidden="true">
              <rect x="1.5" y="8" width="4" height="8" rx="1.4" fill="currentColor" />
              <rect x="18.5" y="8" width="4" height="8" rx="1.4" fill="currentColor" />
              <rect x="6.5" y="10.6" width="11" height="2.8" rx="1.4" fill="currentColor" />
            </svg>
            <strong>FitForge <em>Agent Console</em></strong>
          </span>
          <nav className="viewnav">
            <button className={view === 'handoffs' ? 'vtab active' : 'vtab'}
                    onClick={() => setView('handoffs')}>Handoffs</button>
            <button className={view === 'manuals' ? 'vtab active' : 'vtab'}
                    onClick={() => setView('manuals')}>Manuals</button>
          </nav>
          <a className="crosslink" href="/">← Customer chat</a>
          <a className="crosslink" href="/docs/">Docs</a>
        </div>
        {metrics && (
          <div className="stats">
            <Stat label="Containment"
                  value={metrics.containment_rate != null
                    ? `${(metrics.containment_rate * 100).toFixed(0)}%` : '—'} />
            <Stat label="Sessions" value={metrics.sessions?.total ?? 0} />
            <Stat label="Issues resolved" value={metrics.issues?.resolved ?? 0} />
            <Stat label="Unbacked models" value={metrics.coverage?.unbacked ?? 0}
                  tone={metrics.coverage?.unbacked > 0 ? 'warn' : undefined} />
            <Stat label="Avg LLM latency"
                  value={`${Math.round(metrics.llm?.avg_latency_ms || 0)}ms`} />
          </div>
        )}
      </header>

      {view === 'manuals' && (
        <div className="body single">
          <main><Manuals onCoverageChanged={loadMetrics} /></main>
        </div>
      )}

      {view === 'handoffs' && (
      <div className="body">
        <aside>
          <div className="tabs">
            {['queued', 'claimed', 'all'].map((s) => (
              <button key={s}
                      className={filter === s ? 'tab active' : 'tab'}
                      onClick={() => setFilter(s)}>
                {s}
              </button>
            ))}
          </div>

          {queue.length === 0 && <p className="muted small pad">Nothing waiting.</p>}
          {queue.map((h) => {
            const meta = REASON_META[h.reason] || { tone: 'info', label: h.reason }
            return (
              <div key={h.id}
                   className={`qitem ${selected?.id === h.id ? 'sel' : ''}`}
                   onClick={() => open(h)}>
                <div className="qhead">
                  <span className={`badge ${meta.tone}`}>{meta.label}</span>
                  <span className="muted small">{h.issue_count} issue(s)</span>
                </div>
                <div className="qname">{h.customer_name || 'Unidentified customer'}</div>
                <div className="muted small clamp">{h.summary}</div>
                <div className="muted tiny">{new Date(h.created_at).toLocaleString()}</div>
              </div>
            )
          })}
        </aside>

        <main>
          {!packet && (
            <div className="empty">
              <h2>Select a handoff</h2>
              <p className="muted">
                Every handoff carries the full diagnostic history for each issue
                thread, so you never need to ask the customer to start again.
              </p>
            </div>
          )}

          {packet && (
            <div className="packet">
              <div className="pkthead">
                <div>
                  <h1>{packet.customer?.name || 'Unidentified customer'}</h1>
                  <div className="muted small">
                    {packet.customer?.email} · {packet.customer?.phone}
                  </div>
                </div>
                <div className="actions">
                  {selected?.status === 'queued' && <button onClick={claim}>Claim</button>}
                  <button className="ghost" onClick={resolve}>Mark resolved</button>
                </div>
              </div>

              <Section title="Why this reached you">
                <div className="reason">
                  <span className={`badge ${(REASON_META[packet.escalation?.reason] || {}).tone || 'info'}`}>
                    {(REASON_META[packet.escalation?.reason] || {}).label || packet.escalation?.reason}
                  </span>
                  <span className="muted small">{packet.escalation?.detail}</span>
                </div>
              </Section>

              {packet.summary && (
                <Section title="Summary">
                  <p>{packet.summary.text}</p>
                  <p className="next"><strong>Suggested next action:</strong> {packet.summary.next_action}</p>
                  {!packet.summary.generated && (
                    <p className="tiny muted">
                      Generated from structured state — the summariser was unavailable.
                    </p>
                  )}
                </Section>
              )}

              <Section title={`Machines (${packet.verified_models?.length || 0})`}>
                {packet.verified_models?.map((m) => (
                  <div key={m.model_id} className="row">
                    <div>
                      <strong>{m.name}</strong>
                      <div className="muted tiny">{m.model_id}</div>
                    </div>
                    <div className="muted small">
                      {m.method.replace(/_/g, ' ')} · {(m.confidence * 100).toFixed(0)}%
                      {m.purchased_at && <> · bought {m.purchased_at}</>}
                    </div>
                  </div>
                ))}
              </Section>

              <Section title={`Issue threads (${packet.issues?.length || 0})`}>
                {packet.issues?.map((issue) => (
                  <div key={issue.seq} className="thread">
                    <div className="thead">
                      <strong>#{issue.seq} {issue.title}</strong>
                      <span className={`badge ${issue.status === 'resolved' ? 'ok' : 'warn'}`}>
                        {issue.status.replace(/_/g, ' ')}
                      </span>
                    </div>
                    <div className="muted small">
                      {issue.model_id} · {issue.steps_used} steps
                    </div>
                    {issue.symptom && <p className="symptom">{issue.symptom}</p>}

                    {issue.steps_taken?.length > 0 && (
                      <ol className="steps">
                        {issue.steps_taken.map((step) => (
                          <li key={step.n}>
                            <div className="asked">{step.asked}</div>
                            {step.customer_said && (
                              <div className="said">“{step.customer_said}”</div>
                            )}
                            {step.concluded && (
                              <div className="concluded">→ {step.concluded}</div>
                            )}
                          </li>
                        ))}
                      </ol>
                    )}

                    {issue.ruled_out?.length > 0 && (
                      <div className="ruled">
                        <strong>Ruled out:</strong> {issue.ruled_out.join('; ')}
                      </div>
                    )}
                    {issue.citations?.length > 0 && (
                      <div className="cites">
                        {issue.citations.map((c, i) => (
                          <span key={i} className="cite">
                            {c.section}{c.pages ? ` · ${c.pages}` : ''}
                            {c.confidence < 0.9 && ' ⚠'}
                          </span>
                        ))}
                      </div>
                    )}
                    {issue.candidate_part && (
                      <div className="muted small">Part identified: {issue.candidate_part}</div>
                    )}
                    {issue.resolution && <div className="resolution">{issue.resolution}</div>}
                  </div>
                ))}
              </Section>

              {packet.quotes?.length > 0 && (
                <Section title="Quotes">
                  {packet.quotes.map((q) => (
                    <div key={q.id} className="row">
                      <div>
                        <strong>{q.part_number}</strong>
                        <div className="muted tiny">{q.reason}</div>
                      </div>
                      <div>
                        {q.covered ? <span className="badge ok">Under warranty</span>
                                   : <span>{currency(q.total_cents)}</span>}
                        <span className="muted small"> · {q.status}</span>
                      </div>
                    </div>
                  ))}
                </Section>
              )}

              {packet.orders?.length > 0 && (
                <Section title="Orders placed">
                  {packet.orders.map((o) => (
                    <div key={o.id} className="row">
                      <div>
                        <strong>{o.id}</strong>
                        <div className="muted tiny">{o.part_number}</div>
                      </div>
                      <div className="muted small">
                        {o.status} · ETA {o.eta_days}d · {o.covered ? 'warranty' : currency(o.total_cents)}
                      </div>
                    </div>
                  ))}
                </Section>
              )}

              <Section title={`Transcript (${packet.transcript?.length || 0})`} collapsed>
                <div className="transcript">
                  {packet.transcript?.map((m, i) => (
                    <div key={i} className={`tline ${m.role}`}>
                      <span className="who">{m.role}</span>
                      <span>{m.content}</span>
                    </div>
                  ))}
                </div>
              </Section>
            </div>
          )}
        </main>
      </div>
      )}
    </div>
  )
}

function Stat({ label, value, tone }) {
  return (
    <div className="stat">
      <div className={`sval ${tone || ''}`}>{value}</div>
      <div className="slabel">{label}</div>
    </div>
  )
}

function Section({ title, children, collapsed = false }) {
  const [open, setOpen] = useState(!collapsed)
  return (
    <section>
      <h3 onClick={() => setOpen(!open)}>
        <span className={`caret ${open ? 'open' : ''}`}>▸</span> {title}
      </h3>
      {open && <div className="scontent">{children}</div>}
    </section>
  )
}
