import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  getAgents,
  getBootAssets,
  getDevices,
  getOperations,
  getRegistrationWindow,
  getWorkers,
} from '../api/client'
import { buildBootChecks, probePaths } from '../api/bootStatus'
import {
  agentSummary,
  attestationFailures,
  chainSummary,
  deviceSummary,
  windowModel,
  workerSummary,
} from '../api/dashboardStatus'
import { useI18n } from '../i18n'
import Card from '../components/Card'
import Badge from '../components/Badge'
import Divider from '../components/Divider'
import './Dashboard.css'

const OVERALL_VARIANT = { normal: 'ok', degraded: 'degraded', blocked: 'error', unknown: 'default' }
const OP_VARIANT = { ok: 'ok', started: 'degraded', error: 'error', failed: 'error', rejected: 'error', denied: 'error' }

function chainText(chain, t) {
  if (!chain) return ''
  if (chain.overall === 'unknown') return t('dashboard.chainUnknown')
  const parts = [t('dashboard.chainChecks', { count: chain.counted })]
  if (chain.failed > 0) parts.push(t('dashboard.chainFailed', { count: chain.failed }))
  if (chain.firmwares.length > 0) {
    parts.push(t('dashboard.chainFirmware', { items: chain.firmwares.join(' · ') }))
  }
  return parts.join(' · ')
}

function windowSub(win, t) {
  if (!win) return null
  const quota = win.max === null
    ? t('dashboard.quotaUnlimited', { admitted: win.admitted })
    : t('dashboard.quota', { admitted: win.admitted, max: win.max })
  if (win.open) return quota
  if (win.ttl === null) return t('dashboard.windowNever')
  return `${t('dashboard.windowLast', { ttl: win.ttl })} · ${quota}`
}

function formatTime(ts, locale) {
  const at = new Date(ts)
  return Number.isNaN(at.getTime()) ? String(ts) : at.toLocaleTimeString(locale)
}

export default function Dashboard() {
  const { t, locale } = useI18n()
  const [snapshot, setSnapshot] = useState(null)
  const [probes, setProbes] = useState(null)
  const [errors, setErrors] = useState([])
  const [checkedAt, setCheckedAt] = useState(null)
  const [loading, setLoading] = useState(true)
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setErrors([])
    setProbes(null)

    ;(async () => {
      const [assetRes, workerRes, deviceRes, agentRes, windowRes, opsRes] = await Promise.allSettled([
        getBootAssets(),
        getWorkers(),
        getDevices('all'),
        getAgents(true),
        getRegistrationWindow(),
        getOperations(0, 10, null, 'desc'),
      ])
      if (cancelled) return

      const data = {
        assets: assetRes.status === 'fulfilled' ? assetRes.value : null,
        workers: workerRes.status === 'fulfilled' ? workerRes.value : null,
        devices: deviceRes.status === 'fulfilled' ? deviceRes.value : null,
        agents: agentRes.status === 'fulfilled' ? agentRes.value : null,
        window: windowRes.status === 'fulfilled' ? windowRes.value : null,
        ops: opsRes.status === 'fulfilled' ? (opsRes.value.entries || null) : null,
      }
      setSnapshot(data)

      const message = (result) => result.reason?.message || ''
      setErrors([
        assetRes.status === 'rejected' && { key: 'dashboard.failedAssets', message: message(assetRes) },
        workerRes.status === 'rejected' && { key: 'dashboard.failedWorkers', message: message(workerRes) },
        deviceRes.status === 'rejected' && { key: 'dashboard.failedDevices', message: message(deviceRes) },
        agentRes.status === 'rejected' && { key: 'dashboard.failedAgents', message: message(agentRes) },
        windowRes.status === 'rejected' && { key: 'dashboard.failedWindow', message: message(windowRes) },
        opsRes.status === 'rejected' && { key: 'dashboard.failedOps', message: message(opsRes) },
      ].filter(Boolean))

      if (data.assets) {
        const paths = (data.assets.stage2 || []).map((file) => file.path)
        const result = await probePaths(paths)
        if (cancelled) return
        setProbes(result)
      }
      setCheckedAt(new Date())
      setLoading(false)
    })()

    return () => { cancelled = true }
  }, [nonce])

  const result = useMemo(
    () => (snapshot ? buildBootChecks({ ...snapshot, probes }) : null),
    [snapshot, probes]
  )
  const chain = chainSummary(result)
  const workers = snapshot?.workers ? workerSummary(snapshot.workers) : null
  const devices = snapshot?.devices ? deviceSummary(snapshot.devices) : null
  const agents = snapshot?.agents ? agentSummary(snapshot.agents) : null
  const win = snapshot ? windowModel(snapshot.window) : null
  const attestation = snapshot?.devices ? attestationFailures(snapshot.devices) : null
  const portDown = agents ? agents.portDown : null
  const winSub = windowSub(win, t)
  const ops = snapshot?.ops ?? null

  return (
    <div>
      <div className="dash-head">
        <h2 className="page-title">{t('dashboard.title')}</h2>
        <div className="dash-actions">
          <span className="dash-checked">
            {loading
              ? t('dashboard.refreshing')
              : checkedAt && t('dashboard.updatedAt', { time: checkedAt.toLocaleTimeString(locale) })}
          </span>
          <button className="dash-refresh" onClick={() => setNonce((n) => n + 1)} disabled={loading}>
            {t('dashboard.refresh')}
          </button>
        </div>
      </div>

      {errors.map((error) => (
        <p className="page-error" key={error.key}>
          {t(error.key, { message: error.message })}
        </p>
      ))}
      {loading && !result && <p className="page-loading">{t('common.loading')}</p>}

      {result && (
        <div className="dash-chain">
          <Badge variant={OVERALL_VARIANT[chain.overall]}>
            {t(`assets.overall.${chain.overall}`)}
          </Badge>
          <span className="dash-chain-stage">{t('dashboard.chainStage')}</span>
          <span className="dash-chain-text">{chainText(chain, t)}</span>
          <Link className="dash-chain-go" to="/boot">{t('dashboard.viewChain')}</Link>
        </div>
      )}

      <div className="stat-grid">
        <Card className="stat-card">
          <span className="stat-value">{workers ? `${workers.ready}/${workers.total}` : '—'}</span>
          <span className="stat-label">{t('dashboard.workerReady')}</span>
          {workers && (
            <span className="stat-sub">{t('dashboard.workerNotReady', { n: workers.notReady })}</span>
          )}
        </Card>

        <Card className="stat-card">
          <span className="stat-value">{devices ? devices.total : '—'}</span>
          <span className="stat-label">{t('dashboard.devicePool')}</span>
          {devices && (
            <span className="stat-sub">
              {t('dashboard.deviceBreakdown', {
                pooled: devices.pooled,
                bound: devices.bound,
                revoked: devices.revoked,
              })}
            </span>
          )}
        </Card>

        <Card className="stat-card">
          <span className="stat-value">
            {win?.open ? win.minutes : '—'}
            {win?.open && <span className="stat-unit">{t('dashboard.minutes')}</span>}
          </span>
          <span className="stat-label-row">
            <span className="stat-label">{t('dashboard.registration')}</span>
            {win && (
              <Badge variant={win.open ? 'ok' : 'degraded'}>
                {win.open ? t('dashboard.windowOpen') : t('dashboard.windowClosed')}
              </Badge>
            )}
          </span>
          {winSub && <span className="stat-sub">{winSub}</span>}
        </Card>

        <Card className="stat-card">
          <span className="stat-value">{agents ? `${agents.healthy}/${agents.total}` : '—'}</span>
          <span className="stat-label">{t('dashboard.storageNodes')}</span>
          {agents && (
            <span className="stat-sub">
              {t('dashboard.tlsReady', { ready: agents.tlsReady, total: agents.tlsTotal })}
            </span>
          )}
        </Card>
      </div>

      {snapshot && (
        <div className="dash-anoms">
          <Link to="/devices" className={attestation > 0 ? 'hot' : ''}>
            {t('dashboard.anomAttestation', { count: attestation ?? '—' })}
          </Link>
          <Link to="/agents" className={portDown > 0 ? 'hot' : ''}>
            {t('dashboard.anomPort', { count: portDown ?? '—' })}
          </Link>
        </div>
      )}

      <Divider>{t('dashboard.recentOps')}</Divider>

      {loading && !ops ? (
        <p className="page-loading">{t('common.loading')}</p>
      ) : ops && ops.length === 0 ? (
        <p className="page-muted">{t('dashboard.noOps')}</p>
      ) : ops && (
        <>
          <div className="ops-list">
            {ops.map((op) => (
              <div key={op.id} className="ops-row">
                <span className="ops-id">#{op.id}</span>
                <span className="ops-ts">{formatTime(op.ts, locale)}</span>
                <span className="ops-type">{op.op}</span>
                <Badge variant={OP_VARIANT[op.status] || 'default'}>{op.status}</Badge>
                {op.worker_id && (
                  <Link to={`/workers/${op.worker_id}`} className="ops-link">
                    {op.worker_id}
                  </Link>
                )}
              </div>
            ))}
          </div>
          <div className="ops-more">
            <Link to="/operations">{t('dashboard.viewAll')}</Link>
          </div>
        </>
      )}
    </div>
  )
}
