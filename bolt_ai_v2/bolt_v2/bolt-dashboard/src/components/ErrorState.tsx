/**
 * Error state component with retry button.
 * Shown when API calls fail.
 */
import { AlertTriangle, RefreshCw } from 'lucide-react'

export default function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="bolt-card" style={{
      padding: '40px 24px', textAlign: 'center', display: 'flex',
      flexDirection: 'column', alignItems: 'center', gap: 16,
    }}>
      <AlertTriangle size={40} color="var(--bolt-orange)" />
      <div>
        <div style={{ fontSize: 16, fontWeight: 600, color: 'var(--bolt-text)', marginBottom: 8 }}>
          Something went wrong
        </div>
        <div style={{ fontSize: 13, color: 'var(--bolt-text-muted)', maxWidth: 400 }}>
          {message}
        </div>
      </div>
      {onRetry && (
        <button
          onClick={onRetry}
          style={{
            display: 'flex', alignItems: 'center', gap: 8,
            padding: '8px 20px', borderRadius: 8,
            background: 'var(--bolt-accent)', color: '#000',
            border: 'none', cursor: 'pointer', fontWeight: 600, fontSize: 13,
          }}
        >
          <RefreshCw size={14} /> Retry
        </button>
      )}
    </div>
  )
}
