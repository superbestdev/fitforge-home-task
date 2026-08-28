import { useCallback, useEffect, useRef, useState } from 'react'
import { API } from '../api.js'


const JOB_TONE = {
  queued: 'info', detecting: 'info', ingesting: 'warn',
  awaiting_model: 'warn', done: 'ok', failed: 'bad',
}

const ACTIVE = new Set(['queued', 'detecting', 'ingesting'])
const bytes = (n) => (n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`)

export default function Manuals({ onCoverageChanged }) {
  const [jobs, setJobs] = useState([])
  const [backfill, setBackfill] = useState([])
  const [coverage, setCoverage] = useState(null)
  const [dragging, setDragging] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  // When set, the next upload is filed against this model instead of being
  // auto-detected — this is how the backfill queue hands work to the picker.
  const [target, setTarget] = useState(null)
  const fileInput = useRef(null)

  const refresh = useCallback(async () => {
    const [j, b, m] = await Promise.all([
      fetch(`${API}/api/manuals/jobs`).then((r) => r.json()),
      fetch(`${API}/api/manuals/backfill`).then((r) => r.json()),
      fetch(`${API}/api/metrics`).then((r) => r.json()),
    ])
    setJobs(j.jobs || [])
    setBackfill(b.models || [])
    setCoverage(m.coverage || null)
    // Let the app header update too; it polls slowly, and a header that
    // disagrees with the panel right below it looks broken.
    onCoverageChanged?.()
  }, [onCoverageChanged])

  useEffect(() => { refresh() }, [refresh])

  // Poll only while something is actually in flight. Ingestion takes seconds to
  // minutes, so the alternative is either a stale screen or pointless traffic.
  useEffect(() => {
    if (!jobs.some((j) => ACTIVE.has(j.status))) return
    const t = setInterval(refresh, 2500)
    return () => clearInterval(t)
  }, [jobs, refresh])

  const upload = useCallback(async (files) => {
    if (!files?.length) return
    setBusy(true); setError(null)
    try {
      for (const file of files) {
        const body = new FormData()
        body.append('file', file)
        body.append('uploaded_by', 'console')
        if (target) body.append('model_id', target.model_id)

        const res = await fetch(`${API}/api/manuals/upload`, { method: 'POST', body })
        if (!res.ok) {
          const detail = await res.json().catch(() => ({}))
          setError(detail.detail || `Upload failed (${res.status})`)
          break
        }
      }
      setTarget(null)
      await refresh()
    } finally {
      setBusy(false)
      if (fileInput.current) fileInput.current.value = ''
    }
  }, [target, refresh])

  const onDrop = (e) => {
    e.preventDefault(); setDragging(false)
    upload([...e.dataTransfer.files])
  }

  return (
    <div className="packet">
      <div className="pkthead">
        <div>
          <h1>Service manuals</h1>
          <div className="muted small">
            Uploads run the same pipeline as the seeded corpus — scanned PDFs are
            OCR'd and carry a lower confidence score.
          </div>
        </div>
        {coverage && (
          <div className="stats">
            <div className="stat"><div className="sval">{coverage.backed}</div>
              <div className="slabel">backed</div></div>
            <div className="stat"><div className="sval warn">{coverage.degraded}</div>
              <div className="slabel">degraded</div></div>
            <div className="stat">
              <div className={`sval ${coverage.unbacked > 0 ? 'warn' : ''}`}>
                {coverage.unbacked}
              </div>
              <div className="slabel">unbacked</div>
            </div>
          </div>
        )}
      </div>

      {/* ---- drop zone ---- */}
      <section>
        <h3><span className="caret open">▸</span> Upload</h3>
        <div className="scontent">
          {target && (
            <div className="targetbar">
              Filing under <strong>{target.name}</strong>
              <span className="muted tiny"> ({target.model_id})</span>
              <button className="ghost tinybtn" onClick={() => setTarget(null)}>
                clear
              </button>
            </div>
          )}

          <div
            className={`dropzone ${dragging ? 'over' : ''} ${busy ? 'busy' : ''}`}
            onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            onClick={() => !busy && fileInput.current?.click()}
          >
            <input
              ref={fileInput} type="file" accept="application/pdf,.pdf" multiple
              style={{ display: 'none' }}
              onChange={(e) => upload([...e.target.files])}
            />
            {busy ? (
              <><strong>Uploading…</strong><p className="muted small">
                Indexing continues in the background.</p></>
            ) : (
              <>
                <strong>Drop service manual PDFs here</strong>
                <p className="muted small">
                  or click to choose files · up to 60 MB each
                </p>
                <p className="hint">
                  {target
                    ? `They will be filed under ${target.model_id}.`
                    : 'The model is read from the document itself. If it cannot be identified, you will be asked to pick one.'}
                </p>
              </>
            )}
          </div>

          {error && <div className="uploaderr">{error}</div>}
        </div>
      </section>

      {/* ---- backfill queue ---- */}
      <section>
        <h3><span className="caret open">▸</span> Needs a manual ({backfill.length})</h3>
        <div className="scontent">
          {backfill.length === 0 && (
            <p className="muted small">
              Every model has usable documentation.
            </p>
          )}
          {backfill.length > 0 && (
            <p className="muted small" style={{ marginTop: 0 }}>
              Ordered by how much support traffic each model has generated, so the
              most costly gaps come first.
            </p>
          )}
          {backfill.slice(0, 25).map((m) => (
            <div key={m.model_id} className="row">
              <div>
                <strong>{m.name}</strong>
                <div className="muted tiny">
                  {m.model_id} · {m.notes}
                </div>
              </div>
              <div className="rowright">
                {m.sessions > 0 && (
                  <span className="muted small">{m.sessions} session(s)</span>
                )}
                <span className={`badge ${m.status === 'unbacked' ? 'bad' : 'warn'}`}>
                  {m.status}
                </span>
                <button className="ghost tinybtn"
                        onClick={() => {
                          setTarget({ model_id: m.model_id, name: m.name })
                          window.scrollTo({ top: 0, behavior: 'smooth' })
                        }}>
                  Upload for this
                </button>
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* ---- jobs ---- */}
      <section>
        <h3><span className="caret open">▸</span> Recent uploads</h3>
        <div className="scontent">
          {jobs.length === 0 && <p className="muted small">Nothing uploaded yet.</p>}
          {jobs.map((job) => (
            <JobRow key={job.id} job={job} onChanged={refresh} />
          ))}
        </div>
      </section>
    </div>
  )
}

function JobRow({ job, onChanged }) {
  const tone = JOB_TONE[job.status] || 'info'
  const result = job.result || {}
  const detection = job.detection || {}

  return (
    <div className="jobrow">
      <div className="jobhead">
        <div>
          <strong>{job.filename}</strong>
          <div className="muted tiny">
            {bytes(job.size_bytes)}
            {job.model_name && <> · {job.model_name} ({job.model_id})</>}
          </div>
        </div>
        <div className="rowright">
          {ACTIVE.has(job.status) && <span className="spinner" />}
          <span className={`badge ${tone}`}>{job.status.replace(/_/g, ' ')}</span>
        </div>
      </div>

      {job.stage && <div className="muted small">{job.stage}</div>}

      {detection.mismatch && (
        <div className="mismatch">⚠ {detection.mismatch}</div>
      )}

      {job.status === 'done' && (
        <div className="jobresult">
          <span>{result.chunks} sections indexed</span>
          <span>{result.error_codes} error codes</span>
          <span>confidence {Number(result.confidence).toFixed(2)}</span>
          {result.ocr_used && <span className="badge warn">OCR'd</span>}
          {result.coverage && (
            <span className={`badge ${result.coverage.status === 'backed' ? 'ok' : 'warn'}`}>
              {result.coverage.status}
            </span>
          )}
        </div>
      )}

      {job.status === 'failed' && <div className="uploaderr">{job.error}</div>}

      {job.status === 'awaiting_model' && (
        <ModelPicker jobId={job.id} suggestions={detection.candidates || []}
                     onAssigned={onChanged} />
      )}
    </div>
  )
}

/**
 * Shown when the document could not identify itself. Filing a manual against
 * the wrong SKU is worse than leaving it unfiled, so this asks rather than
 * guessing.
 */
function ModelPicker({ jobId, suggestions, onAssigned }) {
  const [q, setQ] = useState('')
  const [options, setOptions] = useState(suggestions)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (!q.trim()) { setOptions(suggestions); return }
    const t = setTimeout(async () => {
      const res = await fetch(`${API}/api/models/search?q=${encodeURIComponent(q)}`)
      const data = await res.json()
      setOptions(data.models || [])
    }, 250)
    return () => clearTimeout(t)
  }, [q, suggestions])

  const assign = async (modelId) => {
    setSaving(true)
    await fetch(`${API}/api/manuals/jobs/${jobId}/model`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model_id: modelId }),
    })
    setSaving(false)
    onAssigned()
  }

  return (
    <div className="picker">
      <p className="muted small">
        We could not tell which model this manual is for. Search and choose:
      </p>
      <input
        value={q} placeholder="model number or name…" disabled={saving}
        onChange={(e) => setQ(e.target.value)}
      />
      <div className="options">
        {options.slice(0, 8).map((m) => (
          <button key={m.id} className="ghost tinybtn" disabled={saving}
                  onClick={() => assign(m.id)}>
            {m.name} <span className="muted tiny">({m.id})</span>
          </button>
        ))}
        {options.length === 0 && q && (
          <span className="muted small">No models match “{q}”.</span>
        )}
      </div>
    </div>
  )
}
