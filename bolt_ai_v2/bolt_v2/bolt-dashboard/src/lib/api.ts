/**
 * Bolt API client — all dashboard data fetched from the FastAPI backend.
 * Falls back to /data/*.json static files when backend is unavailable.
 */

const API_BASE = import.meta.env.VITE_API_URL || 'http://localhost:8000'

/**
 * Get the API key from environment variable or localStorage.
 * localStorage is set by the Login page when BOLT_API_KEY auth is enabled.
 */
function getApiKey(): string {
  return import.meta.env.VITE_API_KEY || localStorage.getItem('bolt_api_key') || ''
}

async function apiFetch<T>(path: string, opts?: RequestInit): Promise<T> {
  const apiKey = getApiKey()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(apiKey ? { 'X-API-Key': apiKey } : {}),
    ...(opts?.headers as Record<string, string> || {}),
  }
  const res = await fetch(`${API_BASE}${path}`, { ...opts, headers })
  if (res.status === 401) {
    // Auth required but key is invalid/missing -- redirect to login
    localStorage.removeItem('bolt_api_key')
    if (window.location.pathname !== '/login') {
      window.location.href = '/login'
    }
    throw new Error('Authentication required')
  }
  if (!res.ok) throw new Error(`API ${path} -> ${res.status}`)
  return res.json()
}

export const api = {
  // Status
  health:        ()              => apiFetch<any>('/api/health'),
  status:        ()              => apiFetch<any>('/api/status'),

  // Analytics
  analytics:     ()              => apiFetch<any>('/api/analytics'),

  // Scripts
  scripts:       (status?: string) => apiFetch<any>(`/api/scripts${status ? `?status=${status}` : ''}`),
  script:        (id: string)    => apiFetch<any>(`/api/scripts/${id}`),

  // HITL
  approve:       (id: string)    => apiFetch<any>(`/api/hitl/approve/${id}`, { method: 'POST', body: '{}' }),
  reject:        (id: string, reason = '') =>
                                    apiFetch<any>(`/api/hitl/reject/${id}`, {
                                      method: 'POST', body: JSON.stringify({ reason })
                                    }),
  pending:       ()              => apiFetch<any>('/api/hitl/pending'),

  // Pipeline control
  runPipeline:   ()              => apiFetch<any>('/api/pipeline/run',  { method: 'POST', body: '{}' }),
  runStep:       (step: string)  => apiFetch<any>(`/api/pipeline/${step}`, { method: 'POST', body: '{}' }),
  pipelineStatus:()              => apiFetch<any>('/api/pipeline/status'),

  // Costs
  costs:         (month?: string) => apiFetch<any>(`/api/costs${month ? `?month=${month}` : ''}`),

  // Backups
  backups:       ()              => apiFetch<any>('/api/backups'),
  createBackup:  (type = 'manual') => apiFetch<any>('/api/backups', {
                                      method: 'POST', body: JSON.stringify({ backup_type: type })
                                    }),
  restoreBackup: (id: string)    => apiFetch<any>(`/api/backups/${id}/restore`, { method: 'POST', body: '{}' }),

  // News
  news:          ()              => apiFetch<any>('/api/news'),

  // Jobs
  jobs:          ()              => apiFetch<any>('/api/jobs'),
}

/** Hook-style wrapper — returns {data, loading, error} */
export function useApi<T>(fetcher: () => Promise<T>) {
  return { fetcher }  // Components call fetcher in useEffect
}

export type { }
