import { useEffect, useState } from 'react'
import Chat from './components/Chat'
import AuthPanel from './components/AuthPanel'
import Sidebar from './components/Sidebar'
import './App.css'

const AUTH_STORAGE_KEY = 'chatbot_auth_tokens'

function App() {
  const [showAuthPanel, setShowAuthPanel] = useState(false)
  const [authMode, setAuthMode] = useState('signin')
  const [authTokens, setAuthTokens] = useState(() => {
    try {
      const raw = localStorage.getItem(AUTH_STORAGE_KEY)
      return raw ? JSON.parse(raw) : null
    } catch {
      return null
    }
  })

  // ── Conversation / sidebar state ──────────────────────────────────────────
  const [conversations, setConversations] = useState([])
  const [activeConversationId, setActiveConversationId] = useState(null)
  const [sidebarOpen, setSidebarOpen] = useState(false)

  useEffect(() => {
    const setAppHeight = () => {
      document.documentElement.style.setProperty('--app-height', `${window.innerHeight}px`)
    }

    setAppHeight()
    window.addEventListener('resize', setAppHeight)
    window.addEventListener('orientationchange', setAppHeight)
    window.visualViewport?.addEventListener('resize', setAppHeight)

    return () => {
      window.removeEventListener('resize', setAppHeight)
      window.removeEventListener('orientationchange', setAppHeight)
      window.visualViewport?.removeEventListener('resize', setAppHeight)
    }
  }, [])

  const persistTokens = (tokens) => {
    setAuthTokens(tokens)
    localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(tokens))
  }

  // ── Conversation helpers ───────────────────────────────────────────────────
  const loadConversations = async (token) => {
    const t = token ?? authTokens?.access_token
    if (!t) return
    try {
      const resp = await fetch('/api/conversations', {
        headers: { Authorization: `Bearer ${t}` },
      })
      if (resp.ok) setConversations(await resp.json())
    } catch {}
  }

  useEffect(() => {
    if (authTokens) {
      loadConversations(authTokens.access_token)
    } else {
      setConversations([])
      setActiveConversationId(null)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authTokens?.access_token])

  const handleNewChat = () => setActiveConversationId(null)

  const handleSelectConversation = (id) => setActiveConversationId(id)

  const handleConversationStart = async (firstMessage) => {
    if (!authTokens) return null
    try {
      const resp = await fetch('/api/conversations', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${authTokens.access_token}`,
        },
        body: JSON.stringify({ title: firstMessage.slice(0, 60) }),
      })
      if (!resp.ok) return null
      const conv = await resp.json()
      // Do NOT call setActiveConversationId here — doing so would cause Chat's
      // useEffect to fire mid-stream, reloading (empty) messages and racing
      // with the streaming update.  We set the active ID in handleMessageSent
      // once the stream has completed.
      return conv.id
    } catch {
      return null
    }
  }

  // Called by Chat after each completed stream.  If a new conversation was
  // created during that stream (newConvId differs from activeConversationId)
  // we update the active ID and refresh the sidebar list.
  const handleMessageSent = async (newConvId) => {
    if (newConvId && newConvId !== activeConversationId) {
      setActiveConversationId(newConvId)
    }
    await loadConversations()
  }

  const handleDeleteConversation = async (id) => {
    if (!authTokens) return
    try {
      await fetch(`/api/conversations/${id}`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${authTokens.access_token}` },
      })
      if (activeConversationId === id) setActiveConversationId(null)
      await loadConversations()
    } catch {}
  }

  const handleRenameConversation = async (id, newTitle) => {
    if (!authTokens) return
    try {
      await fetch(`/api/conversations/${id}/title`, {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${authTokens.access_token}`,
        },
        body: JSON.stringify({ title: newTitle }),
      })
      await loadConversations()
    } catch {}
  }

  const clearTokens = () => {
    setAuthTokens(null)
    setConversations([])
    setActiveConversationId(null)
    localStorage.removeItem(AUTH_STORAGE_KEY)
  }

  const handleAuthSuccess = (tokens) => {
    persistTokens(tokens)
    setShowAuthPanel(false)
  }

  const openAuthPanel = (mode) => {
    setAuthMode(mode)
    setShowAuthPanel(true)
  }

  const handleSignout = async () => {
    if (authTokens?.refresh_token) {
      try {
        await fetch('/identity/auth/logout', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: authTokens.refresh_token }),
        })
      } catch {
        // local signout should still proceed even if API call fails
      }
    }
    clearTokens()
  }

  const refreshAccessToken = async () => {
    if (!authTokens?.refresh_token) {
      clearTokens()
      return null
    }

    try {
      const resp = await fetch('/identity/auth/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: authTokens.refresh_token }),
      })

      if (!resp.ok) {
        clearTokens()
        return null
      }

      const nextTokens = await resp.json()
      persistTokens(nextTokens)
      return nextTokens.access_token
    } catch {
      clearTokens()
      return null
    }
  }

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-content">
          {authTokens && (
            <button
              className="header-btn sidebar-toggle-btn"
              onClick={() => setSidebarOpen(v => !v)}
              title={sidebarOpen ? 'Hide sidebar' : 'Show sidebar'}
            >
              ☰
            </button>
          )}
          <a href="https://xgalvanize.ca" className="app-logo" target="_blank" rel="noreferrer">
            <span className="logo-bracket">&lt;</span>XG<span className="logo-bracket">/&gt;</span>
          </a>
          <h1>AI Chat</h1>
          <span className="model-badge">phi4-mini</span>
        </div>
        <div className="auth-actions">
          {authTokens ? (
            <button className="header-btn" onClick={handleSignout}>
              Sign out
            </button>
          ) : (
            <>
              <button className="header-btn secondary" onClick={() => openAuthPanel('signin')}>
                Sign in
              </button>
              <button className="header-btn" onClick={() => openAuthPanel('signup')}>
                Sign up
              </button>
            </>
          )}
        </div>
      </header>

      <div className="app-body">
        {authTokens && (
          <Sidebar
            conversations={conversations}
            activeId={activeConversationId}
            onSelect={handleSelectConversation}
            onNew={handleNewChat}
            onDelete={handleDeleteConversation}
            onRename={handleRenameConversation}
            open={sidebarOpen}
          />
        )}
        <main className="app-main">
          <Chat
            accessToken={authTokens?.access_token}
            onAuthExpired={refreshAccessToken}
            conversationId={activeConversationId}
            onConversationStart={handleConversationStart}
            onMessageSent={handleMessageSent}
          />
          {showAuthPanel && (
            <div className="auth-modal-backdrop" onClick={() => setShowAuthPanel(false)}>
              <div className="auth-modal" onClick={(e) => e.stopPropagation()}>
                <button className="auth-modal-close" onClick={() => setShowAuthPanel(false)} aria-label="Close auth dialog">
                  ✕
                </button>
                <AuthPanel key={authMode} initialMode={authMode} onAuthSuccess={handleAuthSuccess} />
              </div>
            </div>
          )}
        </main>
      </div>
    </div>
  )
}

export default App
