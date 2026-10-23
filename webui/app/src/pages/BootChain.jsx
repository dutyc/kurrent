import { useEffect, useMemo, useState } from 'react'
import { getAgents, getBootAssets, getDevices, getWorkers } from '../api/client'
import { buildBootChecks, evidenceParts, probePaths } from '../api/bootStatus'
import { useI18n } from '../i18n'
import './BootChain.css'

function nodeReason(node, t) {
  if (node.health && node.health !== 'ok') {
    return t('assets.nodeReason.health', { health: node.health })
  }
  if (!node.hasPort) return t('assets.nodeReason.noPort')
  return t('assets.nodeReason.portDown', { port: node.port || '—' })
}

function Evidence({ parts }) {
  return (
    <span className="boot-evidence">
      {parts.map((part, index) => (
        <span key={`${part.text}:${index}`}>
          {index > 0 && <span className="boot-sep">·</span>}
          {part.mono ? <code>{part.text}</code> : part.text}
        </span>
      ))}
    </span>
  )
}

function Detail({ check, t, locale }) {
  const detail = check.detail || {}
  const rows = []
  let fix = null
  const join = locale === 'zh-CN' ? '、' : ', '

  if (check.id === 'firmware') {
    if (!detail.declared) {
      rows.push(t('assets.cause.firmwareUndeclared'))
      fix = 'assets.fix.firmwareFile'
    } else if (detail.fileStatus === 'incomplete') {
      rows.push(t('assets.cause.firmwareCaps', {
        file: detail.file,
        caps: (detail.missing || []).join(join),
      }))
      fix = 'assets.fix.firmwareCaps'
    } else {
      const keys = {
        missing: 'firmwareMissing',
        empty: 'firmwareEmpty',
        rejected: 'firmwareRejected',
      }
      const key = keys[detail.fileStatus]
      if (key) {
        rows.push(t(`assets.cause.${key}`, {
          file: detail.file,
          hostPath: detail.hostPath,
        }))
        fix = 'assets.fix.firmwareFile'
      }
    }
  }

  if (check.id === 'service') {
    if (detail.issues?.length) {
      rows.push(
        <ul className="boot-issue-list" key="issues">
          {detail.issues.map((file) => (
            <li key={file.file}>
              <code>{file.file}</code>
              <span className={`boot-file-state boot-file-${file.status}`}>
                {t(`assets.state.${file.status}`)}
              </span>
              {file.host_path && <span className="boot-issue-path">{file.host_path}</span>}
            </li>
          ))}
        </ul>
      )
      fix = 'assets.fix.chainFile'
    }
    if (detail.menuStatus !== 'ok') {
      rows.push(t('assets.cause.menuUnavailable'))
      if (!fix) fix = 'assets.fix.menu'
    }
    if (detail.unreachable?.length) {
      rows.push(t('assets.cause.unreachable', { files: detail.unreachable.join(join) }))
      if (!fix) fix = 'assets.fix.static'
    }
  }

  if (check.id === 'dataPlane') {
    if (!detail.total) {
      rows.push(t('assets.cause.agentsNone'))
    } else {
      rows.push(
        <ul className="boot-issue-list" key="down">
          {detail.down.map((node) => (
            <li key={node.id}>
              <code>{node.id}</code>
              <span className="boot-issue-path">{nodeReason(node, t)}</span>
            </li>
          ))}
        </ul>
      )
    }
    fix = 'assets.fix.dataPlane'
  }

  if (check.id === 'workers') {
    const nodes = detail.notReady
      .map((worker) => {
        const label = t(`assets.readiness.${worker.readiness}`)
        return `${worker.id}（${label}）`
      })
      .join(join)
    rows.push(t(detail.ready === 0 ? 'assets.cause.workersNone' : 'assets.cause.workersPartial', {
      nodes,
    }))
    fix = 'assets.fix.workers'
  }

  return (
    <div className="boot-detail">
      {rows.map((row, index) =>
        typeof row === 'string' ? (
          <p className="boot-cause" key={`cause:${index}`}>{row}</p>
        ) : (
          row
        )
      )}
      {fix && (
        <p className="boot-fix">
          <span className="boot-fix-label">{t('assets.fixLabel')}</span>
          {t(fix)}
        </p>
      )}
    </div>
  )
}

export default function BootChain() {
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
      const [assetRes, agentRes, workerRes, deviceRes] = await Promise.allSettled([
        getBootAssets(),
        getAgents(true),
        getWorkers(),
        getDevices('all'),
      ])
      if (cancelled) return

      const data = {
        assets: assetRes.status === 'fulfilled' ? assetRes.value : null,
        agents: agentRes.status === 'fulfilled' ? agentRes.value : null,
        workers: workerRes.status === 'fulfilled' ? workerRes.value : null,
        devices: deviceRes.status === 'fulfilled' ? deviceRes.value : null,
      }
      setSnapshot(data)

      const message = (result) => result.reason?.message || ''
      setErrors([
        assetRes.status === 'rejected' && { key: 'assets.loadFailed', message: message(assetRes) },
        agentRes.status === 'rejected' && { key: 'assets.agentsFailed', message: message(agentRes) },
        workerRes.status === 'rejected' && { key: 'assets.workersFailed', message: message(workerRes) },
        deviceRes.status === 'rejected' && { key: 'assets.devicesFailed', message: message(deviceRes) },
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

  const summary = result
    ? result.overall === 'unknown'
      ? t('assets.summary.unknown')
      : result.overall === 'normal'
        ? t('assets.summary.normal', { count: result.counted })
        : t(`assets.summary.${result.overall}`, {
          count: result.counted,
          failed: result.counted - result.normal,
        })
    : ''

  return (
    <div>
      <div className="boot-head">
        <div>
          <h2 className="page-title">{t('assets.title')}</h2>
          {result && (
            <p className={`boot-overall boot-overall-${result.overall}`}>
              <span className="boot-overall-label">{t(`assets.overall.${result.overall}`)}</span>
              <span className="boot-overall-note">{summary}</span>
            </p>
          )}
        </div>
        <div className="boot-actions">
          {checkedAt && (
            <span className="boot-checked">
              {t('assets.checkedAt', { time: checkedAt.toLocaleTimeString(locale) })}
            </span>
          )}
          <button className="boot-refresh" onClick={() => setNonce((n) => n + 1)}>
            {t('assets.refresh')}
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
        <>
          <div className="boot-board-head">
            <span>{t('assets.board.label')}</span>
            <span className="boot-board-count">
              {t('assets.board.count', { count: result.counted, normal: result.normal })}
            </span>
          </div>
          <div className="boot-board">
            {result.checks.map((check) => (
              <div className="boot-item" key={check.id}>
                <div className="boot-row">
                  <span className={`boot-state boot-state-${check.status}`}>
                    {t(`assets.status.${check.status}`)}
                  </span>
                  <span className="boot-stage">{t(`assets.checks.${check.id}`)}</span>
                  <Evidence parts={evidenceParts(check, t, locale)} />
                </div>
                {(check.status === 'blocked' || check.status === 'degraded') && (
                  <Detail check={check} t={t} locale={locale} />
                )}
              </div>
            ))}
          </div>
          <p className="boot-foot">
            {t('assets.foot').split('/boot-vars')[0]}
            <code>/boot-vars</code>
            {t('assets.foot').split('/boot-vars')[1]}
          </p>
        </>
      )}
    </div>
  )
}
