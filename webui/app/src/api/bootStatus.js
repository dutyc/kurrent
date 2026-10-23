// 引导链状态聚合：纯函数，输入控制面四份台账与浏览器 HEAD 探测结果，输出五项检查。
// 只回答「现在能不能引导」，不展开脚本内容与菜单项。

const MINUTE = 60_000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

export function relativeAge(iso, now = Date.now()) {
  const at = Date.parse(iso)
  if (!Number.isFinite(at)) return null
  const diff = Math.max(0, now - at)
  if (diff < MINUTE) return { value: 0, unit: 'minute' }
  if (diff < HOUR) return { value: Math.round(diff / MINUTE), unit: 'minute' }
  if (diff < DAY) return { value: Math.round(diff / HOUR), unit: 'hour' }
  return { value: Math.round(diff / DAY), unit: 'day' }
}

export function formatTsas(value) {
  if (!value) return ''
  const match = /^tls\s*(\d+(?:\.\d+)?)$/i.exec(String(value))
  return match ? `TLS ${match[1]}` : String(value)
}

async function head(path) {
  try {
    const res = await fetch(path, { method: 'HEAD', cache: 'no-store' })
    return res.ok
  } catch {
    return null
  }
}

export async function probePaths(paths) {
  const pairs = await Promise.all(paths.map(async (path) => [path, await head(path)]))
  return Object.fromEntries(pairs)
}

export function relativeLabel(age, t, locale) {
  if (!age) return null
  if (age.value === 0) return t('assets.evidence.justNow')
  return new Intl.RelativeTimeFormat(locale, { numeric: 'auto' }).format(-age.value, age.unit)
}

export function evidenceParts(check, t, locale) {
  const detail = check.detail || {}
  const parts = []
  const push = (text, mono = false) => {
    if (text === undefined || text === null || text === '') return
    parts.push({ text: String(text), mono })
  }

  if (check.status === 'unknown') {
    if (check.id === 'workers' && detail.empty) push(t('assets.evidence.noWorkers'))
    else if (check.id === 'report' && detail.reason === 'no-report') {
      push(t('assets.evidence.noReport'))
    } else push(t('assets.evidence.unknown'))
    return parts
  }

  switch (check.id) {
    case 'firmware':
      if (!detail.declared) {
        push(t('assets.evidence.firmwareUndeclared'))
        break
      }
      push(detail.file, true)
      if (detail.version) push(`iPXE ${detail.version}`)
      if (detail.capsTotal) {
        push(t('assets.evidence.firmwareCaps', {
          present: detail.capsPresent,
          total: detail.capsTotal,
        }))
      }
      break
    case 'service':
      if (detail.total) {
        push(
          detail.verified
            ? t('assets.evidence.retrieved', { ok: detail.ok, total: detail.total })
            : t('assets.evidence.retrievedUnverified')
        )
      }
      push(
        detail.menuStatus === 'ok'
          ? t('assets.evidence.menuOk')
          : t('assets.evidence.menuUnavailable')
      )
      break
    case 'dataPlane':
      if (!detail.total) {
        push(t('assets.evidence.noAgents'))
        break
      }
      push(t('assets.evidence.agentsReady', { ready: detail.ready, total: detail.total }))
      if (detail.port?.tsas) push(formatTsas(detail.port.tsas))
      if (detail.port?.trsvcid) push(detail.port.trsvcid, true)
      break
    case 'workers':
      if (!detail.total) push(t('assets.evidence.noWorkers'))
      else push(t('assets.evidence.workersReady', { ready: detail.ready, total: detail.total }))
      break
    case 'report':
      if (detail.reason === 'no-report') {
        push(t('assets.evidence.noReport'))
        break
      }
      push(detail.mac, true)
      if (detail.worker) push(detail.worker)
      push(relativeLabel(detail.age, t, locale))
      break
    default:
      break
  }
  return parts
}

function unknown(detail = {}) {
  return { status: 'unknown', detail }
}

function firmwareCheck(assets) {
  if (!assets) return unknown()
  const firmware = assets.firmware || []
  if (firmware.length === 0) {
    return { status: 'blocked', detail: { declared: false } }
  }
  const item = firmware[0]
  const caps = item.capabilities || []
  const detail = {
    declared: true,
    file: item.file,
    fileStatus: item.status,
    version: item.version || null,
    hostPath: item.host_path || null,
    capsPresent: caps.filter((cap) => cap.present).length,
    capsTotal: caps.length,
    missing: caps.filter((cap) => !cap.present).map((cap) => cap.label || cap.id),
  }
  if (item.status === 'incomplete') return { status: 'degraded', detail }
  if (item.status !== 'ok') return { status: 'blocked', detail }
  return { status: 'normal', detail }
}

function serviceCheck(assets, probes) {
  if (!assets) return unknown()
  const chain = assets.stage2 || []
  const issues = chain.filter((file) => file.status !== 'ok')
  const paths = chain.map((file) => file.path)
  const results = probes ? paths.map((path) => probes[path]) : []
  const verified = results.filter((value) => value === true || value === false).length
  const ok = results.filter((value) => value === true).length
  const unreachable = paths.filter((path) => probes?.[path] === false)
  const detail = {
    total: chain.length,
    ok,
    verified,
    issues,
    unreachable,
    menuStatus: assets.menu?.status || 'unavailable',
    menuItems: assets.menu?.items || 0,
  }
  if (chain.length === 0 || issues.length > 0) return { status: 'blocked', detail }
  if (detail.menuStatus !== 'ok') return { status: 'degraded', detail }
  if (unreachable.length > 0) return { status: 'blocked', detail }
  return { status: 'normal', detail }
}

function dataPlaneCheck(agents) {
  if (!agents) return unknown()
  const nvmet = agents.filter(
    (agent) => agent.enabled !== false && agent.capabilities?.backend === 'nvmet'
  )
  const down = nvmet.filter(
    (agent) => agent.health !== 'ok' || agent.capabilities?.port?.ready !== true
  )
  const ready = nvmet.length - down.length
  const sample = nvmet.find((agent) => agent.capabilities?.port)?.capabilities.port || null
  const detail = {
    total: nvmet.length,
    ready,
    port: sample ? { trsvcid: sample.trsvcid || null, tsas: sample.tsas || null } : null,
    down: down.map((agent) => ({
      id: agent.id,
      health: agent.health || null,
      hasPort: Boolean(agent.capabilities?.port),
      portReady: agent.capabilities?.port?.ready === true,
      port: agent.capabilities?.port?.trsvcid || null,
    })),
  }
  if (nvmet.length === 0) return { status: 'blocked', detail }
  if (ready === 0) return { status: 'blocked', detail }
  if (down.length > 0) return { status: 'degraded', detail }
  return { status: 'normal', detail }
}

function workerCheck(workers) {
  if (!workers) return unknown()
  if (workers.length === 0) return unknown({ empty: true, total: 0, ready: 0, notReady: [] })
  const notReady = workers.filter((worker) => worker.readiness !== 'ready')
  const ready = workers.length - notReady.length
  const detail = {
    total: workers.length,
    ready,
    notReady: notReady.map((worker) => ({
      id: worker.worker_id,
      readiness: worker.readiness || 'idle',
    })),
  }
  if (ready === 0) return { status: 'blocked', detail }
  if (notReady.length > 0) return { status: 'degraded', detail }
  return { status: 'normal', detail }
}

function reportCheck(devices, now) {
  if (!devices) return unknown({ reason: 'load-failed' })
  const reported = devices
    .map((device) => ({ device, at: Date.parse(device.last_seen) }))
    .filter((entry) => Number.isFinite(entry.at))
  if (reported.length === 0) return unknown({ reason: 'no-report' })
  reported.sort((a, b) => b.at - a.at)
  const latest = reported[0].device
  return {
    status: 'normal',
    detail: {
      mac: latest.mac,
      worker: latest.bound_worker_id || null,
      at: latest.last_seen,
      age: relativeAge(latest.last_seen, now),
    },
  }
}

export function buildBootChecks({
  assets = null,
  agents = null,
  workers = null,
  devices = null,
  probes = null,
  now = Date.now(),
} = {}) {
  const checks = [
    { id: 'firmware', ...firmwareCheck(assets) },
    { id: 'service', ...serviceCheck(assets, probes) },
    { id: 'dataPlane', ...dataPlaneCheck(agents) },
    { id: 'workers', ...workerCheck(workers) },
    { id: 'report', ...reportCheck(devices, now) },
  ]
  const counted = checks.filter((check) => check.status !== 'unknown')
  const normal = counted.filter((check) => check.status === 'normal').length
  const overall = counted.some((check) => check.status === 'blocked')
    ? 'blocked'
    : counted.some((check) => check.status === 'degraded')
      ? 'degraded'
      : counted.length > 0
        ? 'normal'
        : 'unknown'
  return { checks, overall, counted: counted.length, normal }
}
