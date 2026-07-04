import { useEffect, useState } from 'react'
import Chat from './components/Chat'
import AuthPanel from './components/AuthPanel'
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

  const clearTokens = () => {
    setAuthTokens(null)
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
        await fetch('/identity/auth/signout', {
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
          <span className="logo">⚡</span>
          <h1>Phi4 Chat</h1>
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
      <main className="app-main">
        <Chat
          accessToken={authTokens?.access_token}
          onAuthExpired={refreshAccessToken}
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
  )
}

export default App
