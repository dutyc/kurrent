import './Badge.css'

const VARIANT_MAP = {
  ok: 'badge-ok',
  ready: 'badge-ok',
  healthy: 'badge-ok',
  error: 'badge-error',
  degraded: 'badge-warn',
  installing: 'badge-warn',
  partial: 'badge-warn',
  started: 'badge-warn',
  failed: 'badge-error',
}

export default function Badge({ children, variant = 'default' }) {
  // children 可能是数组（多表达式），仅当其为字符串时才参与颜色映射
  const text = typeof children === 'string' ? children : ''
  const cls = VARIANT_MAP[text.toLowerCase()] || VARIANT_MAP[variant] || 'badge-default'
  return <span className={`badge ${cls}`}>{children ?? variant}</span>
}
