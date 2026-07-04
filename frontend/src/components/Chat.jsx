import { useState, useRef, useEffect } from 'react'
import './Chat.css'

const WELCOME = 'Hello! I\'m Phi4 Mini, your AI assistant. How can I help you today?'

export default function Chat({ accessToken, onAuthExpired }) {
  const [messages, setMessages] = useState([
    { role: 'assistant', content: WELCOME },
  ])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [error, setError] = useState(null)
  const [copiedCodeIndex, setCopiedCodeIndex] = useState(null)
  const bottomRef = useRef(null)
  const inputRef = useRef(null)
  const abortRef = useRef(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const sendMessage = async () => {
    const text = input.trim()
    if (!text || streaming) return

    setError(null)
    setInput('')

    const userMsg = { role: 'user', content: text }
    const history = [...messages, userMsg]
    setMessages([...history, { role: 'assistant', content: '' }])
    setStreaming(true)

    const controller = new AbortController()
    abortRef.current = controller

    const makeRequest = async (token) => {
      const headers = { 'Content-Type': 'application/json' }
      if (token) {
        headers.Authorization = `Bearer ${token}`
      }

      return fetch('/api/chat', {
        method: 'POST',
        headers,
        body: JSON.stringify({ messages: history }),
        signal: controller.signal,
      })
    }

    try {
      let response = await makeRequest(accessToken)

      if (response.status === 401 && onAuthExpired) {
        const nextAccessToken = await onAuthExpired()
        if (nextAccessToken) {
          response = await makeRequest(nextAccessToken)
        }
      }

      if (!response.ok) {
        throw new Error(`Server error: ${response.status}`)
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() // keep incomplete line in buffer

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          const raw = line.slice(6).trim()
          if (!raw) continue

          let parsed
          try {
            parsed = JSON.parse(raw)
          } catch {
            continue
          }

          if (parsed.error) {
            setError(parsed.error)
            break
          }

          const token = parsed.message?.content
          if (token) {
            setMessages(prev => {
              const updated = [...prev]
              const last = updated[updated.length - 1]
              updated[updated.length - 1] = {
                ...last,
                content: last.content + token,
              }
              return updated
            })
          }
        }
      }
    } catch (err) {
      if (err.name !== 'AbortError') {
        setError(err.message || 'Connection failed')
        // Remove the empty assistant placeholder on error
        setMessages(prev => {
          const updated = [...prev]
          if (updated[updated.length - 1].content === '') {
            return updated.slice(0, -1)
          }
          return updated
        })
      }
    } finally {
      setStreaming(false)
      abortRef.current = null
      inputRef.current?.focus()
    }
  }

  const stopStreaming = () => {
    abortRef.current?.abort()
  }

  const clearChat = () => {
    if (streaming) abortRef.current?.abort()
    setMessages([{ role: 'assistant', content: WELCOME }])
    setError(null)
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage()
    }
  }

  const extractCodeBlocks = (text) => {
    const matches = [...text.matchAll(/```[^\n]*\n?([\s\S]*?)```/g)]
    if (!matches.length) return ''
    return matches
      .map((match) => match[1].replace(/\n$/, ''))
      .join('\n\n')
      .trim()
  }

  const handleCopyCode = async (content, index) => {
    const codeOnly = extractCodeBlocks(content)
    if (!codeOnly) {
      setError('No fenced code blocks found in this response.')
      return
    }

    try {
      await navigator.clipboard.writeText(codeOnly)
      setCopiedCodeIndex(index)
      setTimeout(() => setCopiedCodeIndex((prev) => (prev === index ? null : prev)), 1800)
    } catch {
      setError('Could not copy code to clipboard.')
    }
  }

  return (
    <div className="chat">
      <div className="messages-wrapper">
        <div className="messages">
          {messages.map((msg, i) => (
            <div key={i} className={`message-row ${msg.role}`}>
              <div className="avatar">
                {msg.role === 'assistant' ? '🤖' : '🧑'}
              </div>
              <div className="message-content">
                <div className="bubble">
                  {msg.content || (streaming && i === messages.length - 1 ? (
                    <span className="cursor-blink">▍</span>
                  ) : null)}
                  {streaming && i === messages.length - 1 && msg.content && (
                    <span className="cursor-blink">▍</span>
                  )}
                </div>
                {msg.role === 'assistant' && /```/.test(msg.content) && (
                  <button
                    className="copy-code-btn"
                    onClick={() => handleCopyCode(msg.content, i)}
                    type="button"
                  >
                    {copiedCodeIndex === i ? 'Copied' : 'Copy code'}
                  </button>
                )}
              </div>
            </div>
          ))}
          {error && (
            <div className="error-banner">
              ⚠ {error}
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </div>

      <div className="input-bar">
        <button className="clear-btn" onClick={clearChat} title="Clear chat">
          ✕
        </button>
        <textarea
          ref={inputRef}
          className="input-field"
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Message Phi4… (Enter to send, Shift+Enter for newline)"
          rows={1}
          disabled={streaming}
        />
        {streaming ? (
          <button className="send-btn stop" onClick={stopStreaming}>
            ◼
          </button>
        ) : (
          <button
            className="send-btn"
            onClick={sendMessage}
            disabled={!input.trim()}
          >
            ➤
          </button>
        )}
      </div>
    </div>
  )
}
