import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { getWorkers, getAgents, getDevices, getOperations } from '../api/client'
import { useI18n } from '../i18n'
import Card from '../components/Card'
import Badge from '../components/Badge'
import Divider from '../components/Divider'
import './Dashboard.css'

function StatCard({ label, value, sub }) {
  return (
    <Card className="stat-card">
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
      {sub && <span className="stat-sub">{sub}</span>}
    </Card>
  )
}

export default function Dashboard() {
  const { t } = useI18n()
  const [stats, setStats] = useState({ workers: 0, agents: 0, agentHealthy: 0, devices: 0, dataPlaneReady: 0, dataPlaneTotal: 0 })
  const [recentOps, setRecentOps] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const [workers, agents, devices, ops] = await Promise.all([
          getWorkers().catch(() => []),
          getAgents(true).catch(() => []),
          getDevices().catch(() => []),
          getOperations(0, 10).catch(() => ({ entries: [] })),
        ])
        if (cancelled) return
        const agentList = Array.isArray(agents) ? agents : []
        const agentHealthy = agentList.filter((a) => a.health === 'ok').length
        // 节点级加密前置缺口：nvmet 节点中数据面 TLS 端口就绪数（不逐 Worker 轮询，避免 N+1）
        const nvmetAgents = agentList.filter((a) => a.capabilities?.backend === 'nvmet')
        const dataPlaneReady = nvmetAgents.filter((a) => a.capabilities?.port?.ready === true).length
        setStats({
          workers: Array.isArray(workers) ? workers.length : 0,
          devices: Array.isArray(devices) ? devices.length : 0,
          agents: agentList.length,
          agentHealthy,
          dataPlaneReady,
          dataPlaneTotal: nvmetAgents.length,
        })
        setRecentOps((ops.entries || []).slice().reverse())
      } catch (e) {
        if (!cancelled) setError(e.message)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    load()
    return () => { cancelled = true }
  }, [])

  return (
    <div>
      <h2 className="page-title">{t('dashboard.title')}</h2>

      {error && <p className="page-error">{error}</p>}

      <div className="stat-grid">
        <StatCard
          label={t('dashboard.workers')}
          value={loading ? '—' : stats.workers}
        />
        <StatCard
          label={t('dashboard.devices')}
          value={loading ? '—' : stats.devices}
        />
        <StatCard
          label={t('dashboard.agents')}
          value={loading ? '—' : `${stats.agentHealthy}/${stats.agents}`}
          sub={t('dashboard.healthyTotal')}
        />
        <StatCard
          label={t('dashboard.dataPlane')}
          value={loading ? '—' : `${stats.dataPlaneReady}/${stats.dataPlaneTotal}`}
          sub={t('dashboard.dataPlaneSub')}
        />
      </div>

      <Divider>{t('dashboard.recentOps')}</Divider>

      {loading ? (
        <p className="page-loading">{t('common.loading')}</p>
      ) : recentOps.length === 0 ? (
        <p className="page-muted">{t('dashboard.noOps')}</p>
      ) : (
        <div className="ops-list">
          {recentOps.map((op) => (
            <div key={op.id} className="ops-row">
              <span className="ops-id">#{op.id}</span>
              <span className="ops-ts">{op.ts}</span>
              <span className="ops-type">{op.op}</span>
              <Badge>{op.status}</Badge>
              {op.worker_id && (
                <Link to={`/workers/${op.worker_id}`} className="ops-link">
                  {op.worker_id}
                </Link>
              )}
            </div>
          ))}
        </div>
      )}

      {recentOps.length > 0 && (
        <div className="ops-more">
          <Link to="/operations">{t('dashboard.viewAll')}</Link>
        </div>
      )}
    </div>
  )
}
