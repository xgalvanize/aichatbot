import { useState } from 'react'
import {
  signInWithPopup,
  signInWithEmailAndPassword,
  createUserWithEmailAndPassword,
  sendPasswordResetEmail,
  updateProfile,
} from 'firebase/auth'
import { auth, googleProvider } from '../firebase'
import './AuthPanel.css'

function firebaseErrorMessage(err) {
  switch (err.code) {
    case 'auth/invalid-email':           return 'Invalid email address.'
    case 'auth/user-not-found':
    case 'auth/wrong-password':
    case 'auth/invalid-credential':      return 'Incorrect email or password.'
    case 'auth/email-already-in-use':    return 'An account with this email already exists.'
    case 'auth/weak-password':           return 'Password must be at least 6 characters.'
    case 'auth/too-many-requests':       return 'Too many attempts. Please try again later.'
    case 'auth/network-request-failed':  return 'Network error. Check your connection.'
    case 'auth/popup-blocked':           return 'Popup blocked. Allow popups for this site.'
    case 'auth/configuration-not-found': return 'Auth provider not enabled in Firebase Console.'
    default: return err.message || 'Authentication failed.'
  }
}

export default function AuthPanel({ onAuthSuccess }) {
  const [mode, setMode] = useState('signin')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [resetSent, setResetSent] = useState(false)

  const switchMode = (next) => {
    setMode(next)
    setError(null)
    setResetSent(false)
  }

  // Exchange a Firebase ID token for global-identity JWT tokens
  const exchange = async (firebaseUser) => {
    const idToken = await firebaseUser.getIdToken()
    const resp = await fetch('/identity/auth/firebase/exchange', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id_token: idToken }),
    })
    if (!resp.ok) {
      const details = await resp.json().catch(() => null)
      throw new Error(details?.detail || `Identity exchange failed (${resp.status})`)
    }
    return resp.json()
  }

  const handleGoogle = async () => {
    setLoading(true)
    setError(null)
    try {
      const cred = await signInWithPopup(auth, googleProvider)
      onAuthSuccess(await exchange(cred.user))
    } catch (err) {
      if (err.code !== 'auth/popup-closed-by-user') setError(firebaseErrorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  const handleSignIn = async (e) => {
    e.preventDefault()
    setLoading(true)
    setError(null)
    try {
      const cred = await signInWithEmailAndPassword(auth, email, password)
      onAuthSuccess(await exchange(cred.user))
    } catch (err) {
      setError(firebaseErrorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  const handleSignUp = async (e) => {
    e.preventDefault()
    setLoading(true)
    setError(null)
    try {
      const cred = await createUserWithEmailAndPassword(auth, email, password)
      if (displayName.trim()) await updateProfile(cred.user, { displayName: displayName.trim() })
      onAuthSuccess(await exchange(cred.user))
    } catch (err) {
      setError(firebaseErrorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  const handleReset = async (e) => {
    e.preventDefault()
    setLoading(true)
    setError(null)
    try {
      await sendPasswordResetEmail(auth, email)
      setResetSent(true)
    } catch (err) {
      setError(firebaseErrorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  // ── Password reset screen ──────────────────────────────────────────────────
  if (mode === 'reset') {
    return (
      <div className="auth-wrap">
        <form className="auth-card" onSubmit={handleReset}>
          <h2>Reset password</h2>
          <p className="auth-subtitle">Enter your email and we'll send a reset link.</p>
          {resetSent ? (
            <p className="auth-success">Check your inbox for a reset link.</p>
          ) : (
            <>
              <label>
                Email
                <input type="email" value={email} onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com" required />
              </label>
              {error && <div className="auth-error">{error}</div>}
              <button className="auth-submit" disabled={loading}>
                {loading ? 'Sending...' : 'Send reset link'}
              </button>
            </>
          )}
          <button type="button" className="auth-link" onClick={() => switchMode('signin')}>
            Back to sign in
          </button>
        </form>
      </div>
    )
  }

  // ── Sign in / Sign up screen ───────────────────────────────────────────────
  return (
    <div className="auth-wrap">
      <form className="auth-card" onSubmit={mode === 'signin' ? handleSignIn : handleSignUp}>
        <h2>{mode === 'signin' ? 'Sign in' : 'Create account'}</h2>

        <button type="button" className="auth-google-btn" onClick={handleGoogle} disabled={loading}>
          <svg className="auth-google-icon" viewBox="0 0 48 48" aria-hidden="true">
            <path fill="#4285F4" d="M46.5 24.5c0-1.6-.1-3.1-.4-4.5H24v8.5h12.7c-.5 2.8-2.1 5.1-4.5 6.7v5.5h7.3c4.3-3.9 6.8-9.7 6.8-16.2z"/>
            <path fill="#34A853" d="M24 48c6.5 0 11.9-2.1 15.9-5.8l-7.3-5.5c-2.1 1.4-4.8 2.2-7.6 2.2-5.9 0-10.8-3.9-12.6-9.2H3.9v5.7C7.8 42.5 15.4 48 24 48z"/>
            <path fill="#FBBC05" d="M11.4 28.7c-.5-1.4-.7-2.9-.7-4.5s.2-3.1.7-4.5v-5.7H3.9C2.3 17.3 1.5 20.5 1.5 24s.8 6.7 2.4 9.5l7.5-4.8z"/>
            <path fill="#EA4335" d="M24 10.1c3.3 0 6.3 1.2 8.6 3.4l6.5-6.5C35.9 3.3 30.5 1 24 1 15.4 1 7.8 6.5 3.9 14.5l7.5 4.7C13.2 14 18.1 10.1 24 10.1z"/>
          </svg>
          Continue with Google
        </button>

        <div className="auth-divider"><span>or</span></div>

        <div className="auth-toggle">
          <button type="button" className={mode === 'signin' ? 'active' : ''} onClick={() => switchMode('signin')}>Sign in</button>
          <button type="button" className={mode === 'signup' ? 'active' : ''} onClick={() => switchMode('signup')}>Sign up</button>
        </div>

        {mode === 'signup' && (
          <label>
            Display name
            <input type="text" value={displayName} onChange={(e) => setDisplayName(e.target.value)}
              placeholder="Your name" />
          </label>
        )}

        <label>
          Email
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)}
            placeholder="you@example.com" required />
        </label>

        <label>
          Password
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
            placeholder="At least 6 characters" minLength={6} required />
        </label>

        {error && <div className="auth-error">{error}</div>}

        <button className="auth-submit" disabled={loading}>
          {loading ? 'Please wait...' : mode === 'signin' ? 'Sign in' : 'Create account'}
        </button>

        {mode === 'signin' && (
          <button type="button" className="auth-link" onClick={() => switchMode('reset')}>
            Forgot password?
          </button>
        )}
      </form>
    </div>
  )
}
