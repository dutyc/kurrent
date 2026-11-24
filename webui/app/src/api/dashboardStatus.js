// 概览页聚合：由各台账投影出统计卡与异常计数，纯函数，组件只做文案与渲染。

export function workerSummary(workers) {
  const list = Array.isArray(workers) ? workers : []
  const ready = list.filter((worker) => worker.readiness === 'ready').length
  return { total: list.length, ready, notReady: list.length - ready }
}

export function deviceSummary(devices) {
  const list = Array.isArray(devices) ? devices : []
  const counts = { pooled: 0, bound: 0, revoked: 0 }
  for (const device of list) {
    const state = device?.state
    if (state in counts) counts[state] += 1
  }
  return { total: list.length, ...counts }
}

export function agentSummary(agents) {
  const list = Array.isArray(agents) ? agents : []
  const healthy = list.filter((agent) => agent.health === 'ok').length
  const nvmet = list.filter((agent) => agent.capabilities?.backend === 'nvmet')
  const tlsReady = nvmet.filter((agent) => agent.capabilities?.port?.ready === true).length
  return {
    total: list.length,
    healthy,
    tlsReady,
    tlsTotal: nvmet.length,
    portDown: nvmet.length - tlsReady,
  }
}

// 末次证明失败：PCR 基线不符，或校验过程出错（unproven 保留失败原因）。
export function attestationFailures(devices) {
  if (!Array.isArray(devices)) return null
  return devices.filter((device) => {
    const attestation = device?.attestation || {}
    return (
      attestation.state === 'mismatch' ||
      (attestation.state === 'unproven' && Boolean(attestation.last_reason))
    )
  }).length
}

// 注册窗口卡片模型：开启时给剩余分钟与额度；关闭时保留上次窗口时长与入池数。
export function windowModel(win) {
  if (!win) return null
  const admitted = Number.isInteger(win.admitted) ? win.admitted : 0
  const max = Number.isInteger(win.max_devices) ? win.max_devices : null
  if (win.open) {
    const seconds = Number(win.remaining_seconds) || 0
    return { open: true, minutes: Math.max(1, Math.ceil(seconds / 60)), admitted, max }
  }
  return {
    open: false,
    ttl: Number.isInteger(win.ttl_minutes) ? win.ttl_minutes : null,
    admitted,
    max,
  }
}

// 引导链条摘要：结论、检查数与首跳固件能力计数。
export function chainSummary(result) {
  if (!result) return null
  const firmware = result.checks.find((check) => check.id === 'firmware')?.detail || {}
  const firmwares = firmware.declared
    ? (firmware.firmwares || [])
      .filter((item) => item.file)
      .map((item) => (item.capsTotal ? `${item.file} ${item.capsPresent}/${item.capsTotal}` : item.file))
    : []
  return {
    overall: result.overall,
    counted: result.counted,
    failed: result.counted - result.normal,
    firmwares,
  }
}
