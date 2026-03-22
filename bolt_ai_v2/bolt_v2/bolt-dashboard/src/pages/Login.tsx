/**
 * Login page -- collects API key and stores in localStorage.
 * Shown when BOLT_API_KEY is required but not yet provided.
 */
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Zap, Key } from 'lucide-react'

export default function Login() {
  const [apiKey, setApiKey] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const navigate = useNavigate()

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!apiKey.trim()) {
      setError('API key is required')
      return
    }

    setLoading(true)
    setError('')

    try {
      // Validate the key by calling health endpoint with it
      const API_BASE = import.meta.env.VITE_API_URL || 'http://localhost:8000'
      const res = await fetch(`${API_BASE}/api/status`, {
        headers: { 'X-API-Key': apiKey.trim() },
      })

      if (res.status === 401) {
        setError('Invalid API key')
        setLoading(false)
        return
      }

      // Key is valid -- store and redirect
      localStorage.setItem('bolt_api_key', apiKey.trim())
      navigate('/')
    } catch {
      // Backend might be down -- store key anyway and let the dashboard handle retries
      localStorage.setItem('bolt_api_key', apiKey.trim())
      navigate('/')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{
      minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center',
      background: 'var(--bolt-dark, #0a0e1a)',
    }}>
      <div style={{
        width: 380, padding: 32, borderRadius: 16,
        background: 'var(--bolt-surface, #111827)',
        border: '1px solid var(--bolt-border, #1f2937)',
      }}>
        <div style={{ textAlign: 'center', marginBottom: 32 }}>
          <Zap size={40} color="var(--bolt-accent, #facc15)" />
          <h1 style={{ fontSize: 22, fontWeight: 700, color: 'var(--bolt-text, #f9fafb)', marginTop: 12 }}>
            Bolt AI Dashboard
          </h1>
          <p style={{ fontSize: 13, color: 'var(--bolt-text-muted, #9ca3af)', marginTop: 8 }}>
            Enter your API key to access the dashboard
          </p>
        </div>

        <form onSubmit={handleLogin}>
          <div style={{ marginBottom: 16 }}>
            <label style={{ display: 'block', fontSize: 12, fontWeight: 600, color: 'var(--bolt-text-dim, #6b7280)', marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              <Key size={12} style={{ marginRight: 4, verticalAlign: 'middle' }} />
              API Key
            </label>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="Enter BOLT_API_KEY..."
              style={{
                width: '100%', padding: '10px 14px', borderRadius: 8, fontSize: 14,
                background: 'var(--bolt-dark, #0a0e1a)',
                border: `1px solid ${error ? 'var(--bolt-red, #ef4444)' : 'var(--bolt-border, #1f2937)'}`,
                color: 'var(--bolt-text, #f9fafb)',
                outline: 'none', boxSizing: 'border-box',
              }}
              autoFocus
            />
          </div>

          {error && (
            <div style={{ fontSize: 13, color: 'var(--bolt-red, #ef4444)', marginBottom: 12 }}>
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            style={{
              width: '100%', padding: '10px 0', borderRadius: 8,
              background: 'var(--bolt-accent, #facc15)', color: '#000',
              border: 'none', cursor: loading ? 'wait' : 'pointer',
              fontWeight: 700, fontSize: 14, opacity: loading ? 0.7 : 1,
            }}
          >
            {loading ? 'Verifying...' : 'Sign In'}
          </button>
        </form>

        <p style={{ fontSize: 11, color: 'var(--bolt-text-muted, #6b7280)', textAlign: 'center', marginTop: 20 }}>
          Set BOLT_API_KEY in .env to enable authentication.
          <br />Leave empty for local development mode.
        </p>
      </div>
    </div>
  )
}
