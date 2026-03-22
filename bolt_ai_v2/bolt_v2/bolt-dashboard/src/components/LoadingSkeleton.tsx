/**
 * Loading skeleton components for the dashboard.
 * Shows animated placeholder shapes while data is being fetched.
 */

export function SkeletonPulse({ width = '100%', height = 20, borderRadius = 6 }: {
  width?: string | number; height?: number; borderRadius?: number;
}) {
  return (
    <div
      className="skeleton-pulse"
      style={{
        width, height, borderRadius,
        background: 'linear-gradient(90deg, var(--bolt-surface) 25%, var(--bolt-border) 50%, var(--bolt-surface) 75%)',
        backgroundSize: '200% 100%',
        animation: 'skeleton-shimmer 1.5s ease-in-out infinite',
      }}
    />
  )
}

export function SkeletonCard({ lines = 3 }: { lines?: number }) {
  return (
    <div className="bolt-card" style={{ padding: '18px 20px' }}>
      <SkeletonPulse width="40%" height={12} />
      <div style={{ height: 12 }} />
      <SkeletonPulse width="70%" height={28} />
      {Array.from({ length: lines - 1 }).map((_, i) => (
        <div key={i}>
          <div style={{ height: 8 }} />
          <SkeletonPulse width={`${60 + Math.random() * 30}%`} height={14} />
        </div>
      ))}
    </div>
  )
}

export function SkeletonGrid({ cards = 4, columns = 4 }: { cards?: number; columns?: number }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: `repeat(${columns}, 1fr)`, gap: 16 }}>
      {Array.from({ length: cards }).map((_, i) => (
        <SkeletonCard key={i} lines={2} />
      ))}
    </div>
  )
}

export function SkeletonChart({ height = 300 }: { height?: number }) {
  return (
    <div className="bolt-card" style={{ padding: 20 }}>
      <SkeletonPulse width="30%" height={16} />
      <div style={{ height: 16 }} />
      <SkeletonPulse width="100%" height={height} borderRadius={8} />
    </div>
  )
}

export function SkeletonTable({ rows = 5 }: { rows?: number }) {
  return (
    <div className="bolt-card" style={{ padding: 20 }}>
      <SkeletonPulse width="25%" height={16} />
      <div style={{ height: 16 }} />
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} style={{ display: 'flex', gap: 12, marginBottom: 12 }}>
          <SkeletonPulse width="20%" height={16} />
          <SkeletonPulse width="50%" height={16} />
          <SkeletonPulse width="15%" height={16} />
          <SkeletonPulse width="15%" height={16} />
        </div>
      ))}
    </div>
  )
}
