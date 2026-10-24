// 证书台账分组：接口按序列号返回证书，页面按身份聚合。
// 节点按 agent_id 分组，每种证书（agent / nvmet-host）取最新一张作为当前身份；
// 历史吊销证书不参与状态判定，只计入页头的吊销总数。
// 用户按 CN 分组，取最新一张（users 域每类只有一个证书种类）。

const parseTs = (ts) => {
  const time = Date.parse(ts || '')
  return Number.isFinite(time) ? time : null
}

export function certState(cert, now = Date.now()) {
  if (cert.revoked_at) return 'revoked'
  if (typeof cert.expired === 'boolean') return cert.expired ? 'expired' : 'ok'
  const expires = parseTs(cert.expires_at)
  return expires !== null && expires <= now ? 'expired' : 'ok'
}

function latest(certs) {
  return certs.reduce((best, cert) => {
    if (!best) return cert
    const a = parseTs(cert.created_at) ?? 0
    const b = parseTs(best.created_at) ?? 0
    if (a !== b) return a > b ? cert : best
    return String(cert.serial) > String(best.serial) ? cert : best
  }, null)
}

function summarize(certs, now) {
  const states = certs.map((cert) => certState(cert, now))
  const activeCount = states.filter((state) => state === 'ok').length
  let status = 'ok'
  if (activeCount === 0) {
    status = states.includes('revoked') ? 'revoked' : 'expired'
  } else if (activeCount < certs.length) {
    status = 'partial'
  }
  const active = certs.filter((_, index) => states[index] === 'ok')
  const pool = active.length > 0 ? active : certs
  const expiry = pool.reduce((best, cert) => {
    const time = parseTs(cert.expires_at)
    if (time === null) return best
    return !best || time > best.time ? { time, ts: cert.expires_at } : best
  }, null)
  const revoked = certs.find((cert) => cert.revoked_at)
  return {
    status,
    activeCount,
    total: certs.length,
    activeKinds: [...new Set(active.map((cert) => cert.kind).filter(Boolean))],
    expiresAt: expiry ? expiry.ts : '',
    reason: revoked ? revoked.reason || '' : '',
  }
}

const byName = (a, b) => a.localeCompare(b)

export function groupUserCerts(entries, now = Date.now()) {
  const groups = new Map()
  for (const cert of entries || []) {
    const cn = cert.cn || ''
    if (!groups.has(cn)) groups.set(cn, { cn, groups: [], certs: [] })
    const group = groups.get(cn)
    group.certs.push(cert)
    if (Array.isArray(cert.groups)) group.groups = cert.groups
  }
  return [...groups.values()]
    .map((group) => {
      const current = latest(group.certs)
      return {
        cn: group.cn,
        groups: group.groups,
        certs: [current],
        serials: current.revoked_at ? [] : [current.serial],
        ...summarize([current], now),
      }
    })
    .sort((a, b) => byName(a.cn, b.cn))
}

export function groupComponentCerts(entries, now = Date.now()) {
  // 每台节点有两类组件（agent / nvmet-host），各自持有 client + serving 两张证书；
  // CN 前缀区分组件，按 (CN, kind) 取最新一张，共 2~4 张当前证书。
  const groups = new Map()
  for (const cert of entries || []) {
    const agentId = cert.agent_id || cert.cn || ''
    if (!groups.has(agentId)) groups.set(agentId, { agentId, current: new Map() })
    const key = `${cert.cn || ''}|${cert.kind || 'unknown'}`
    const current = groups.get(agentId).current
    current.set(key, current.has(key) ? latest([current.get(key), cert]) : cert)
  }
  return [...groups.values()]
    .map((group) => {
      const certs = [...group.current.values()]
      return {
        agentId: group.agentId,
        kinds: [...new Set(certs.map((cert) => cert.kind).filter(Boolean))].sort(),
        certs,
        serials: certs.filter((cert) => !cert.revoked_at).map((cert) => cert.serial),
        ...summarize(certs, now),
      }
    })
    .sort((a, b) => byName(a.agentId, b.agentId))
}

export function groupControlCerts(entries, now = Date.now()) {
  const groups = new Map()
  for (const cert of entries || []) {
    const cn = cert.cn || ''
    if (!groups.has(cn)) groups.set(cn, [])
    groups.get(cn).push(cert)
  }
  return [...groups.entries()]
    .map(([cn, certs]) => {
      const current = latest(certs)
      return {
        cn,
        certs: [current],
        serials: current.revoked_at ? [] : [current.serial],
        ...summarize([current], now),
      }
    })
    .sort((a, b) => byName(a.cn, b.cn))
}

export function revokedCertCount(...lists) {
  return lists.flat().filter((cert) => cert.revoked_at).length
}
