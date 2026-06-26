import { useState, useRef, useEffect } from 'react'

const API = 'http://127.0.0.1:8000/api'

function ParticleBackground() {
  const canvasRef = useRef(null)
  useEffect(() => {
    const canvas = canvasRef.current
    const ctx = canvas.getContext('2d')
    let particles = [], animId

    function resize() {
      canvas.width = window.innerWidth
      canvas.height = window.innerHeight
      particles = []
      const count = Math.floor((canvas.width * canvas.height) / 12000)
      for (let i = 0; i < count; i++) {
        particles.push({
          x: Math.random() * canvas.width,
          y: Math.random() * canvas.height,
          vx: (Math.random() - 0.5) * 0.3,
          vy: (Math.random() - 0.5) * 0.3,
          size: Math.random() * 1.5 + 0.5,
          opacity: Math.random() * 0.35 + 0.08,
        })
      }
    }

    function animate() {
      ctx.clearRect(0, 0, canvas.width, canvas.height)
      for (const p of particles) {
        p.x += p.vx; p.y += p.vy
        if (p.x < 0) p.x = canvas.width
        if (p.x > canvas.width) p.x = 0
        if (p.y < 0) p.y = canvas.height
        if (p.y > canvas.height) p.y = 0
        ctx.beginPath()
        ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2)
        ctx.fillStyle = `rgba(6,182,212,${p.opacity})`
        ctx.fill()
      }
      animId = requestAnimationFrame(animate)
    }

    resize()
    window.addEventListener('resize', resize)
    animate()
    return () => { cancelAnimationFrame(animId); window.removeEventListener('resize', resize) }
  }, [])

  return (
    <canvas ref={canvasRef} style={{
      position: 'fixed', inset: 0, width: '100%', height: '100%',
      pointerEvents: 'none', zIndex: 0,
    }} />
  )
}

// Simple markdown renderer
function MarkdownText({ text }) {
  const lines = text.split('\n')
  const elements = []
  let i = 0

  while (i < lines.length) {
    const line = lines[i]

    // Heading
    if (line.startsWith('### ')) {
      elements.push(<div key={i} style={{ fontWeight: 700, fontSize: 15, marginTop: 8, marginBottom: 2, color: '#a5f3fc' }}>{renderInline(line.slice(4))}</div>)
    } else if (line.startsWith('## ')) {
      elements.push(<div key={i} style={{ fontWeight: 700, fontSize: 16, marginTop: 10, marginBottom: 4, color: '#a5f3fc' }}>{renderInline(line.slice(3))}</div>)
    } else if (line.startsWith('# ')) {
      elements.push(<div key={i} style={{ fontWeight: 700, fontSize: 17, marginTop: 10, marginBottom: 4, color: '#a5f3fc' }}>{renderInline(line.slice(2))}</div>)
    }
    // Bullet
    else if (line.match(/^[\-\*] /)) {
      elements.push(
        <div key={i} style={{ display: 'flex', gap: 8, marginTop: 3 }}>
          <span style={{ color: '#06b6d4', flexShrink: 0, marginTop: 1 }}>•</span>
          <span>{renderInline(line.slice(2))}</span>
        </div>
      )
    }
    // Numbered list
    else if (line.match(/^\d+\. /)) {
      const num = line.match(/^(\d+)\. /)[1]
      elements.push(
        <div key={i} style={{ display: 'flex', gap: 8, marginTop: 3 }}>
          <span style={{ color: '#06b6d4', flexShrink: 0, minWidth: 18 }}>{num}.</span>
          <span>{renderInline(line.replace(/^\d+\. /, ''))}</span>
        </div>
      )
    }
    // Empty line
    else if (line.trim() === '') {
      elements.push(<div key={i} style={{ height: 6 }} />)
    }
    // Normal paragraph
    else {
      elements.push(<div key={i} style={{ marginTop: 2 }}>{renderInline(line)}</div>)
    }
    i++
  }

  return <div style={{ lineHeight: 1.65 }}>{elements}</div>
}

function renderInline(text) {
  const parts = []
  const regex = /(\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`)/g
  let last = 0
  let match

  while ((match = regex.exec(text)) !== null) {
    if (match.index > last) parts.push(text.slice(last, match.index))
    if (match[2]) parts.push(<strong key={match.index} style={{ fontWeight: 700, color: '#e2e8f0' }}>{match[2]}</strong>)
    else if (match[3]) parts.push(<em key={match.index} style={{ fontStyle: 'italic' }}>{match[3]}</em>)
    else if (match[4]) parts.push(<code key={match.index} style={{ background: 'rgba(6,182,212,0.15)', borderRadius: 4, padding: '1px 5px', fontSize: 13, fontFamily: 'monospace', color: '#a5f3fc' }}>{match[4]}</code>)
    last = match.index + match[0].length
  }
  if (last < text.length) parts.push(text.slice(last))
  return parts.length === 1 && typeof parts[0] === 'string' ? parts[0] : parts
}

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false)

  const copy = () => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }

  return (
    <button onClick={copy} style={{
      background: 'none', border: 'none', cursor: 'pointer',
      color: copied ? '#06b6d4' : '#475569',
      fontSize: 11, padding: '2px 6px', borderRadius: 4,
      transition: 'color 0.2s', fontFamily: 'inherit',
      marginTop: 4, alignSelf: 'flex-end',
    }}>
      {copied ? '✓ Copied' : 'Copy'}
    </button>
  )
}

export default function App() {
  const [docId, setDocId] = useState(null)
  const [filename, setFilename] = useState('')
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [pendingEdit, setPendingEdit] = useState(null)
  const [downloadReady, setDownloadReady] = useState(false)
  const bottomRef = useRef(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const upload = async (e) => {
    const file = e.target.files[0]
    if (!file) return
    setUploading(true)
    const fd = new FormData()
    fd.append('file', file)
    try {
      const res = await fetch(`${API}/documents/upload`, { method: 'POST', body: fd })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Upload failed')
      setDocId(data.doc_id)
      setFilename(data.filename)
      setMessages([{ role: 'assistant', content: `Ready! Ask me anything about "${data.filename}" or tell me what to edit.` }])
      setDownloadReady(false)
      setPendingEdit(null)
    } catch (err) { alert(err.message) }
    finally { setUploading(false) }
  }

  const send = async () => {
    if (!input.trim() || !docId || loading) return
    const text = input.trim()
    setInput('')
    setLoading(true)
    setMessages(m => [...m, { role: 'user', content: text }])
    try {
      const res = await fetch(`${API}/documents/${docId}/messages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Send failed')
      setMessages(m => [...m, { role: 'assistant', content: data.content, kind: data.kind }])
      if (data.kind === 'edit_proposal') setPendingEdit(data)
    } catch (err) {
      setMessages(m => [...m, { role: 'assistant', content: err.message }])
    } finally { setLoading(false) }
  }

  const confirmEdit = async () => {
    if (!pendingEdit || !docId || loading) return
    setLoading(true)
    try {
      const res = await fetch(`${API}/documents/${docId}/edits/confirm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ edit_id: pendingEdit.edit_id }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Confirm failed')
      setMessages(m => [...m, { role: 'assistant', content: 'Edit applied. Download your updated document below.' }])
      setPendingEdit(null)
      setDownloadReady(true)
    } catch (err) { alert(err.message) }
    finally { setLoading(false) }
  }

  const cancelEdit = async () => {
    if (!pendingEdit || !docId) return
    try {
      const res = await fetch(`${API}/documents/${docId}/edits/cancel`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ edit_id: pendingEdit.edit_id }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Cancel failed')
      setMessages(m => [...m, { role: 'assistant', content: 'Edit cancelled.' }])
      setPendingEdit(null)
    } catch (err) { alert(err.message) }
  }

  const download = async () => {
    if (!docId) return
    try {
      const res = await fetch(`${API}/documents/${docId}/download`)
      if (!res.ok) throw new Error('Download failed')
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url; a.download = filename; a.click()
      URL.revokeObjectURL(url)
    } catch (err) { alert(err.message) }
  }

  const clearChat = () => {
    setMessages([{ role: 'assistant', content: `Chat cleared! Ask me anything about "${filename}" or tell me what to edit.` }])
    setPendingEdit(null)
    setDownloadReady(false)
  }

  return (
    <div style={styles.page}>
      <ParticleBackground />

      <header style={styles.header}>
        <div style={styles.headerLeft}>
          <div style={styles.logoMark}>R</div>
          <h1 style={styles.title}>Rey</h1>
        </div>
        <div style={styles.headerRight}>
          {filename && (
            <div style={styles.filenameChip}>
              <span style={styles.filenameText}>{filename}</span>
            </div>
          )}
          {docId && (
            <button style={styles.clearBtn} onClick={clearChat}>Clear</button>
          )}
          {downloadReady && (
            <button style={styles.downloadBtn} onClick={download}>↓ Download</button>
          )}
        </div>
      </header>

      <main style={styles.main}>
        {!docId ? (
          <label style={styles.uploadZone}>
            <input type="file" accept=".docx,.pdf,.pptx,.xlsx" onChange={upload} hidden />
            <div style={styles.uploadInner}>
              <div style={styles.uploadIcon}>⬆</div>
              <div style={styles.uploadTitle}>{uploading ? 'Uploading…' : 'Drop your document here'}</div>
              <div style={styles.uploadSub}>Supports .docx · .pdf · .pptx · .xlsx</div>
            </div>
          </label>
        ) : (
          <>
            <div style={styles.messages}>
              {messages.map((msg, i) => (
                <div key={i} style={styles.messageRow(msg.role)}>
                  {msg.role === 'assistant' && <div style={styles.avatar}>Rey</div>}
                  <div style={{ display: 'flex', flexDirection: 'column', maxWidth: '78%' }}>
                    <div style={{
                      ...styles.bubble,
                      ...(msg.role === 'user' ? styles.userBubble : styles.assistantBubble),
                    }}>
                      {msg.role === 'assistant'
                        ? <MarkdownText text={msg.content} />
                        : msg.content}
                    </div>
                    {msg.role === 'assistant' && <CopyButton text={msg.content} />}
                  </div>

                </div>
              ))}

              {loading && (
                <div style={styles.messageRow('assistant')}>
                  <div style={styles.avatar}>Rey</div>
                  <div style={{ ...styles.bubble, ...styles.assistantBubble }}>
                    <div style={styles.typingDots}>
                      <span style={styles.dot(0)} />
                      <span style={styles.dot(1)} />
                      <span style={styles.dot(2)} />
                    </div>
                  </div>
                </div>
              )}
              <div ref={bottomRef} />
            </div>

            {pendingEdit && (
              <div style={styles.editCard}>
                <div style={styles.editCardHeader}>
                  <span style={styles.editBadge}>✎ Proposed Edit</span>
                </div>
                <div style={styles.diff}>
                  <div style={styles.diffLabel}>Remove</div>
                  <div style={styles.oldText}>{pendingEdit.old_text}</div>
                  <div style={styles.diffLabel}>Replace with</div>
                  <div style={styles.newText}>{pendingEdit.new_text}</div>
                </div>
                <div style={styles.editActions}>
                  <button style={styles.confirmBtn} onClick={confirmEdit} disabled={loading}>✓ Apply</button>
                  <button style={styles.cancelBtn} onClick={cancelEdit} disabled={loading}>✕ Cancel</button>
                </div>
              </div>
            )}

            <div style={styles.inputWrapper}>
              <div style={styles.inputRow}>
                <input
                  style={styles.input}
                  value={input}
                  onChange={e => setInput(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && !e.shiftKey && send()}
                  placeholder="Ask a question or request an edit…"
                  disabled={loading}
                />
                <button
                  style={{ ...styles.sendBtn, opacity: (loading || !input.trim()) ? 0.45 : 1 }}
                  onClick={send}
                  disabled={loading || !input.trim()}
                >↑</button>
              </div>
            </div>
          </>
        )}
      </main>

      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');
        *, *::before, *::after { box-sizing: border-box; }
        body { margin: 0; }
        @keyframes blink {
          0%, 80%, 100% { opacity: 0.15; transform: scale(0.8); }
          40% { opacity: 1; transform: scale(1); }
        }
        ::-webkit-scrollbar { width: 4px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: rgba(6,182,212,0.3); border-radius: 4px; }

        /* Responsive */
        @media (max-width: 600px) {
          .rey-main { padding: 12px 12px 10px !important; }
          .rey-header { padding: 10px 14px !important; }
          .rey-bubble { max-width: 90% !important; font-size: 13.5px !important; }
          .rey-input { font-size: 14px !important; }
          .rey-avatar { width: 26px !important; height: 26px !important; font-size: 9px !important; }
          .rey-filename { display: none !important; }
        }
        @media (min-width: 601px) and (max-width: 1024px) {
          .rey-main { max-width: 700px !important; }
        }
      `}</style>
    </div>
  )
}

const styles = {
  page: {
    fontFamily: "'Inter', system-ui, sans-serif",
    minHeight: '100vh',
    background: 'linear-gradient(135deg, #0a0a12 0%, #0f0a1e 50%, #0a0f1a 100%)',
    color: '#e2e8f0',
    display: 'flex',
    flexDirection: 'column',
    position: 'relative',
  },
  header: {
    padding: '14px 24px',
    borderBottom: '1px solid rgba(6,182,212,0.2)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    position: 'relative',
    zIndex: 1,
    background: 'rgba(10,10,20,0.6)',
    backdropFilter: 'blur(20px)',
    WebkitBackdropFilter: 'blur(20px)',
    flexWrap: 'wrap',
    gap: 10,
  },
  headerLeft: { display: 'flex', alignItems: 'center', gap: 10 },
  headerRight: { display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' },
  logoMark: {
    width: 32, height: 32, borderRadius: 8,
    background: 'linear-gradient(135deg, #06b6d4, #0891b2)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    fontWeight: 700, fontSize: 15, color: '#fff',
    boxShadow: '0 0 12px rgba(6,182,212,0.5)',
    flexShrink: 0,
  },
  title: {
    margin: 0, fontSize: 18, fontWeight: 600,
    background: 'linear-gradient(90deg, #e2e8f0, #67e8f9)',
    WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent', backgroundClip: 'text',
  },
  filenameChip: {
    display: 'flex', alignItems: 'center', gap: 6,
    background: 'rgba(6,182,212,0.12)',
    border: '1px solid rgba(6,182,212,0.25)',
    borderRadius: 20, padding: '4px 12px',
  },
  filenameText: { color: '#67e8f9', fontSize: 13, fontWeight: 500 },
  clearBtn: {
    padding: '6px 14px',
    background: 'rgba(255,255,255,0.06)',
    color: '#94a3b8', border: '1px solid rgba(255,255,255,0.1)',
    borderRadius: 20, cursor: 'pointer', fontWeight: 500,
    fontSize: 13, fontFamily: 'inherit',
  },
  downloadBtn: {
    padding: '7px 18px',
    background: 'linear-gradient(135deg, #22c55e, #16a34a)',
    color: '#fff', border: 'none', borderRadius: 20,
    cursor: 'pointer', fontWeight: 600, fontSize: 13,
    boxShadow: '0 0 16px rgba(34,197,94,0.35)', fontFamily: 'inherit',
  },
  main: {
    flex: 1, display: 'flex', flexDirection: 'column',
    maxWidth: 860, width: '100%', margin: '0 auto',
    padding: '24px 24px 16px', position: 'relative', zIndex: 1,
  },
  uploadZone: {
    flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
    border: '1.5px dashed rgba(6,182,212,0.4)', borderRadius: 20,
    cursor: 'pointer', minHeight: 240,
    background: 'rgba(6,182,212,0.04)',
  },
  uploadInner: { display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10, textAlign: 'center', padding: 24 },
  uploadIcon: {
    fontSize: 28, width: 64, height: 64, borderRadius: 16,
    background: 'rgba(6,182,212,0.15)', border: '1px solid rgba(6,182,212,0.3)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
  },
  uploadTitle: { fontSize: 16, fontWeight: 600, color: '#a5f3fc' },
  uploadSub: { fontSize: 13, color: '#64748b' },
  messages: {
    flex: 1, overflowY: 'auto', display: 'flex',
    flexDirection: 'column', gap: 16, marginBottom: 16, paddingRight: 4,
  },
  messageRow: (role) => ({
    display: 'flex', alignItems: 'flex-start', gap: 10,
    justifyContent: role === 'user' ? 'flex-end' : 'flex-start',
  }),
  avatar: {
    width: 32, height: 32, borderRadius: 8,
    background: 'linear-gradient(135deg, #06b6d4, #0891b2)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    fontSize: 10, fontWeight: 700, color: '#fff', flexShrink: 0,
    boxShadow: '0 0 8px rgba(6,182,212,0.4)', letterSpacing: '0.02em',
    marginTop: 2,
  },
  userAvatar: {
    background: 'linear-gradient(135deg, #3b82f6, #2563eb)',
    boxShadow: '0 0 8px rgba(59,130,246,0.4)',
  },
  bubble: {
    padding: '11px 16px', borderRadius: 16,
    lineHeight: 1.6, whiteSpace: 'pre-wrap', fontSize: 14.5,
  },
  userBubble: {
    background: 'linear-gradient(135deg, #3b82f6, #2563eb)',
    color: '#fff', borderBottomRightRadius: 4,
    boxShadow: '0 4px 20px rgba(59,130,246,0.25)',
  },
  assistantBubble: {
    background: 'rgba(255,255,255,0.05)',
    backdropFilter: 'blur(12px)', WebkitBackdropFilter: 'blur(12px)',
    border: '1px solid rgba(6,182,212,0.2)',
    borderBottomLeftRadius: 4, color: '#e2e8f0',
    boxShadow: '0 4px 20px rgba(0,0,0,0.2)',
  },
  typingDots: { display: 'flex', gap: 5, alignItems: 'center', height: 18 },
  dot: (i) => ({
    width: 7, height: 7, borderRadius: '50%', background: '#06b6d4',
    display: 'inline-block',
    animation: `blink 1.2s ease-in-out ${i * 0.2}s infinite`,
  }),
  editCard: {
    background: 'rgba(255,255,255,0.04)',
    backdropFilter: 'blur(16px)', WebkitBackdropFilter: 'blur(16px)',
    border: '1px solid rgba(6,182,212,0.3)',
    borderRadius: 16, padding: 18, marginBottom: 16,
    boxShadow: '0 0 30px rgba(6,182,212,0.1)',
  },
  editCardHeader: { marginBottom: 14 },
  editBadge: {
    fontSize: 12, fontWeight: 600, color: '#67e8f9',
    background: 'rgba(6,182,212,0.15)',
    border: '1px solid rgba(6,182,212,0.25)',
    borderRadius: 20, padding: '3px 10px', letterSpacing: '0.02em',
  },
  diff: { display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 14 },
  diffLabel: {
    fontSize: 11, fontWeight: 600, color: '#64748b',
    textTransform: 'uppercase', letterSpacing: '0.08em', marginTop: 4,
  },
  oldText: {
    padding: '10px 14px',
    background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.2)',
    borderRadius: 10, textDecoration: 'line-through', fontSize: 13.5, color: '#fca5a5',
  },
  newText: {
    padding: '10px 14px',
    background: 'rgba(34,197,94,0.08)', border: '1px solid rgba(34,197,94,0.2)',
    borderRadius: 10, fontSize: 13.5, color: '#86efac',
  },
  editActions: { display: 'flex', gap: 8 },
  confirmBtn: {
    padding: '8px 20px',
    background: 'linear-gradient(135deg, #22c55e, #16a34a)',
    color: '#fff', border: 'none', borderRadius: 10,
    cursor: 'pointer', fontWeight: 600, fontSize: 13, fontFamily: 'inherit',
    boxShadow: '0 0 12px rgba(34,197,94,0.3)',
  },
  cancelBtn: {
    padding: '8px 20px',
    background: 'rgba(255,255,255,0.06)', color: '#94a3b8',
    border: '1px solid rgba(255,255,255,0.1)',
    borderRadius: 10, cursor: 'pointer', fontWeight: 500, fontSize: 13, fontFamily: 'inherit',
  },
  inputWrapper: {
    background: 'rgba(255,255,255,0.04)',
    backdropFilter: 'blur(20px)', WebkitBackdropFilter: 'blur(20px)',
    border: '1px solid rgba(6,182,212,0.2)',
    borderRadius: 16, padding: 6,
  },
  inputRow: { display: 'flex', gap: 6, alignItems: 'center' },
  input: {
    flex: 1, padding: '11px 16px', borderRadius: 12,
    border: 'none', background: 'transparent',
    color: '#e2e8f0', fontSize: 14.5, outline: 'none', fontFamily: 'inherit',
  },
  sendBtn: {
    width: 40, height: 40,
    background: 'linear-gradient(135deg, #06b6d4, #0891b2)',
    color: '#fff', border: 'none', borderRadius: 12,
    cursor: 'pointer', fontWeight: 700, fontSize: 18,
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    boxShadow: '0 0 16px rgba(6,182,212,0.4)',
    flexShrink: 0, fontFamily: 'inherit', transition: 'opacity 0.2s',
  },
}