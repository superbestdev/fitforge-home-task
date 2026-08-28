import { useCallback, useEffect, useRef, useState } from 'react'
import { API, WS } from '../api.js'

// Demo cards the mock PSP recognises. A real integration would mount the PSP's
// hosted fields here instead; the card number would never reach this component.
const TEST_CARDS = [
  { label: 'Approves', number: '4242424242424242' },
  { label: 'Declines (insufficient funds)', number: '4000000000000002' },
  { label: 'Declines (expired card)', number: '4000000000000069' },
]

const STATUS_STYLE = {
  new: 'st-open', diagnosing: 'st-open', awaiting_customer: 'st-open',
  awaiting_part: 'st-wait', resolved: 'st-done',
  unresolvable: 'st-bad', escalated: 'st-bad',
}

// The step budget the backend enforces. Shown as a bar rather than a count,
// because "how close am I to being handed to a person" is the useful reading.
const STEP_BUDGET = 8

// Three openers that take genuinely different routes through the system — a
// symptom that starts the diagnostic loop, a console code that resolves from
// a lookup table without a model call, and a parts request that goes to the
// warranty check. One click lands somewhere worth seeing.
const QUICK_STARTS = [
  'My treadmill belt keeps slipping when I run',
  'The display is showing E2',
  'I need to order a replacement part',
]

/* ---------- icons: inline so nothing is fetched at runtime ---------------- */

const Mark = ({ size = 22 }) => (
  <svg className="mark" width={size} height={size} viewBox="0 0 24 24" fill="none"
       aria-hidden="true">
    <rect x="1.5" y="8" width="4" height="8" rx="1.4" fill="currentColor" />
    <rect x="18.5" y="8" width="4" height="8" rx="1.4" fill="currentColor" />
    <rect x="6.5" y="10.6" width="11" height="2.8" rx="1.4" fill="currentColor" />
  </svg>
)

const PageIcon = () => (
  <svg width="9" height="11" viewBox="0 0 9 11" fill="none" aria-hidden="true">
    <path d="M1 1.4A.9.9 0 0 1 1.9.5h3.3L8 3.3v6.3a.9.9 0 0 1-.9.9H1.9a.9.9 0 0 1-.9-.9z"
          stroke="currentColor" strokeWidth="1" />
  </svg>
)

const LockIcon = () => (
  <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
    <rect x="2.5" y="6" width="9" height="6.5" rx="1.6" stroke="currentColor" strokeWidth="1.3" />
    <path d="M4.7 6V4.3a2.3 2.3 0 0 1 4.6 0V6" stroke="currentColor" strokeWidth="1.3" />
  </svg>
)

const CheckIcon = () => (
  <svg width="13" height="13" viewBox="0 0 14 14" fill="none" aria-hidden="true">
    <path d="M2.5 7.4 5.6 10.5 11.5 4" stroke="currentColor" strokeWidth="1.7"
          strokeLinecap="round" strokeLinejoin="round" />
  </svg>
)

const SendIcon = () => (
  <svg width="17" height="17" viewBox="0 0 18 18" fill="none" aria-hidden="true">
    <path d="M2 9h13M9.5 3.5 15 9l-5.5 5.5" stroke="currentColor" strokeWidth="1.9"
          strokeLinecap="round" strokeLinejoin="round" />
  </svg>
)

/* ------------------------------------------------------------------------- */

export default function App() {
  const [sessionId, setSessionId] = useState(null)
  const [messages, setMessages] = useState([])
  const [issues, setIssues] = useState([])
  const [thinking, setThinking] = useState(false)
  const [needsPayment, setNeedsPayment] = useState(false)
  const [escalated, setEscalated] = useState(false)
  const [input, setInput] = useState('')
  const [email, setEmail] = useState('')
  const [connected, setConnected] = useState(false)
  const [starting, setStarting] = useState(false)

  const ws = useRef(null)
  const bottom = useRef(null)
  const box = useRef(null)

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, thinking, needsPayment])

  // Grow the composer with its content instead of scrolling a fixed box.
  useEffect(() => {
    const el = box.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }, [input])

  const start = useCallback(async (seed) => {
    if (starting) return
    setStarting(true)
    try {
      const res = await fetch(`${API}/api/sessions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ customer_email: email || null }),
      })
      const data = await res.json()
      setSessionId(data.session_id)
      setMessages([{ role: 'agent', content: data.greeting }])

      const socket = new WebSocket(`${WS}/ws/chat/${data.session_id}`)
      socket.onopen = () => {
        setConnected(true)
        // A quick-start click is a first message, not a different mode — send it
        // once the socket is actually up rather than racing the handshake.
        if (typeof seed === 'string' && seed) {
          setMessages((m) => [...m, { role: 'customer', content: seed }])
          socket.send(JSON.stringify({ type: 'message', message: seed }))
          setThinking(true)
        }
      }
      socket.onclose = () => setConnected(false)
      socket.onmessage = (event) => {
        const msg = JSON.parse(event.data)
        if (msg.type === 'thinking') { setThinking(true); return }
        if (msg.type === 'history') return
        if (msg.type === 'message') {
          setThinking(false)
          setMessages((m) => [...m, {
            role: 'agent', content: msg.content, citations: msg.citations || [],
          }])
          if (msg.issues) setIssues(msg.issues)
          setNeedsPayment(Boolean(msg.requires_payment))
          if (msg.escalated) setEscalated(true)
        }
        if (msg.type === 'error') {
          setThinking(false)
          setMessages((m) => [...m, { role: 'system', content: msg.message }])
        }
      }
      ws.current = socket
    } finally {
      setStarting(false)
    }
  }, [email, starting])

  const send = () => {
    const text = input.trim()
    if (!text || !ws.current || thinking) return
    setMessages((m) => [...m, { role: 'customer', content: text }])
    ws.current.send(JSON.stringify({ type: 'message', message: text }))
    setInput('')
  }

  const pay = (cardNumber) => {
    if (!ws.current) return
    setNeedsPayment(false)
    setMessages((m) => [...m, {
      role: 'system', content: 'Card details sent securely to the payment provider.',
    }])
    ws.current.send(JSON.stringify({ type: 'payment', test_card: cardNumber }))
  }

  /* ---------------------------- start screen ----------------------------- */

  if (!sessionId) {
    return (
      <div className="shell">
        <div className="startcard">
          <div className="lockup">
            <Mark />
            <div className="wordmark">FitForge <span>Support</span></div>
          </div>

          <h1>Let’s get your machine working again.</h1>
          <p className="sub">
            Tell us what it’s doing. We’ll work through it one step at a time,
            using the service manual for your exact model.
          </p>

          <div className="field">
            <label htmlFor="email">Email you ordered with</label>
            <input
              id="email"
              value={email}
              placeholder="you@example.com"
              autoComplete="email"
              onChange={(e) => setEmail(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && start()}
            />
            <p className="hint">
              Optional. It lets us look up which machine you own — otherwise we’ll
              ask for the serial number on the frame.
            </p>
          </div>

          <button className="go" onClick={() => start()} disabled={starting}>
            {starting ? 'Connecting…' : 'Start chat'}
          </button>

          <div className="quickstart">
            <div className="qlabel">Or start with</div>
            <div className="chips">
              {QUICK_STARTS.map((q) => (
                <button key={q} className="chip" disabled={starting}
                        onClick={() => start(q)}>{q}</button>
              ))}
            </div>
          </div>

          <div className="trust">
            <div><CheckIcon /> Every step is cited to a page of your model’s manual.</div>
            <div><CheckIcon /> Card details go straight to the payment provider.</div>
            <div><CheckIcon /> A human is one message away, with your full history.</div>
          </div>

          {/* Demo affordance. A real deployment would not point customers at
              the internal console — this belongs behind staff auth. */}
          <a className="crosslink" href="/console/">Agent console →</a>
        </div>
      </div>
    )
  }

  /* ---------------------------- conversation ----------------------------- */

  return (
    <div className="shell chat-shell">
      <header>
        <div className="headleft">
          <div className="lockup">
            <Mark size={20} />
            <div className="wordmark">FitForge <span>Support</span></div>
          </div>
          <span className="status">
            <span className={connected ? 'dot ok' : 'dot off'} />
            {connected ? 'Live' : 'Reconnecting'}
          </span>
        </div>
        <div className="headright">
          {escalated && <span className="badge bad">Handed to a colleague</span>}
          <a className="crosslink" href="/console/">Agent console →</a>
        </div>
      </header>

      <div className="body">
        <main>
          <div className="messages">
            {messages.map((m, i) => (
              <div key={i} className={`msg ${m.role}`}>
                {m.role === 'agent' && <span className="avatar"><Mark size={14} /></span>}
                <div className="bubble">
                  {m.content.split('\n').map((line, j) => <p key={j}>{line}</p>)}
                  {m.citations?.length > 0 && (
                    <div className="cites">
                      {m.citations.map((c, k) => (
                        <span key={k} className="cite" title={c.heading || ''}>
                          <PageIcon />
                          {c.section}{c.pages ? ` · ${c.pages}` : ''}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            ))}
            {thinking && (
              <div className="msg agent">
                <span className="avatar"><Mark size={14} /></span>
                <div className="bubble thinking">
                  <span /><span /><span />
                </div>
              </div>
            )}
            <div ref={bottom} />
          </div>

          {needsPayment && (
            <div className="paybox">
              <div className="payhead">
                <LockIcon />
                <strong>Secure payment</strong>
                <span className="secured">PCI-scoped · tokenised</span>
              </div>
              <p className="hint">
                Your card goes straight to our payment provider. The support
                assistant never sees it. Pick a test card:
              </p>
              <div className="cards">
                {TEST_CARDS.map((c) => (
                  <button key={c.number} className="ghost" onClick={() => pay(c.number)}>
                    {c.label}
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="composer">
            <textarea
              ref={box}
              rows={1}
              value={input}
              placeholder={thinking ? 'Working on it…' : 'Describe what’s happening…'}
              disabled={thinking}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
              }}
            />
            <button onClick={send} disabled={thinking || !input.trim()} aria-label="Send">
              <SendIcon />
            </button>
          </div>
        </main>

        <aside>
          <h2>Your issues</h2>
          {issues.length === 0 && (
            <div className="emptyissues">
              Each problem you raise is tracked here separately.
            </div>
          )}
          {issues.map((issue) => {
            const tone = STATUS_STYLE[issue.status] || ''
            const used = Math.min(issue.step_budget_used || 0, STEP_BUDGET)
            return (
              <div key={issue.id} className={`issue ${tone}`}>
                <div className="issue-head">
                  <span className="seq">#{issue.seq}</span>
                  <span className={`badge ${tone}`}>
                    {issue.status.replace(/_/g, ' ')}
                  </span>
                </div>
                <div className="issue-title">{issue.title}</div>
                <div className="modelno">{issue.model_id || 'model not yet confirmed'}</div>
                <div className="steps-bar">
                  <span className="track">
                    <i style={{ width: `${(used / STEP_BUDGET) * 100}%` }} />
                  </span>
                  <em>{used}/{STEP_BUDGET}</em>
                </div>
                {issue.resolution_note && (
                  <div className="resolution">{issue.resolution_note}</div>
                )}
              </div>
            )
          })}
          <p className="asidenote">
            Raise a second problem whenever you like — we’ll hold your place on
            this one and come back to it.
          </p>
        </aside>
      </div>
    </div>
  )
}
