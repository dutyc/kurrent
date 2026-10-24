// 身份与访问页的纯数据分类：token 按用途/绑定的节点拆分，会话按是否有证书指纹
// 区分用户与自动化；节点标识由系统生成（12 位 hex，与 join / token create 同构）。

const usageOf = (token) => (Array.isArray(token.usage) ? token.usage : [])

export function isManagementToken(token) {
  return usageOf(token).includes('admin')
}

export function tokenUsable(token) {
  const maxUses = Number(token.max_uses || 0)
  return maxUses <= 0 || Number(token.uses || 0) < maxUses
}

export function tokenPurpose(token) {
  if (isManagementToken(token)) {
    const scopes = Array.isArray(token.scopes) ? token.scopes : []
    if (scopes.includes('enroll')) return 'enroll'
    const groups = Array.isArray(token.groups) ? token.groups : []
    return groups.includes('admin') ? 'admin' : 'readonly'
  }
  return token.component === 'nvmet-host' ? 'derived' : 'bootstrap'
}

export function splitTokens(tokens) {
  const management = []
  const node = []
  for (const token of tokens || []) {
    if (isManagementToken(token)) management.push(token)
    else if (tokenUsable(token)) node.push(token)
  }
  management.sort((a, b) => String(a.expires_at || '').localeCompare(String(b.expires_at || '')))
  node.sort((a, b) =>
    String(a.agent_id || '').localeCompare(String(b.agent_id || '')) ||
    String(a.component || '').localeCompare(String(b.component || '')) ||
    String(a.expires_at || '').localeCompare(String(b.expires_at || ''))
  )
  return { management, node }
}

export function splitSessions(sessions) {
  const user = []
  const automation = []
  for (const session of sessions || []) {
    if (session.cert_fp) user.push(session)
    else automation.push(session)
  }
  user.sort((a, b) => String(a.created_at || '').localeCompare(String(b.created_at || '')))
  automation.sort((a, b) => String(a.created_at || '').localeCompare(String(b.created_at || '')))
  return { user, automation }
}

export function agentLabels(agents) {
  const labels = new Map()
  for (const agent of agents || []) {
    labels.set(agent.id, String(agent.remark || '').trim())
  }
  return labels
}

export function nodeName(agentId, labels) {
  const remark = labels instanceof Map ? labels.get(agentId) : ''
  return remark || agentId || ''
}

export function newNodeId() {
  const bytes = new Uint8Array(6)
  if (!globalThis.crypto?.getRandomValues) {
    throw new Error('crypto.getRandomValues unavailable')
  }
  globalThis.crypto.getRandomValues(bytes)
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
}
