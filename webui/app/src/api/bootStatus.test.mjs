import { test } from 'node:test'
import assert from 'node:assert/strict'

import { buildBootChecks, evidenceParts, relativeAge, formatTsas } from './bootStatus.js'

const ASSETS = {
  firmware: [{
    file: 'snponly.efi',
    status: 'ok',
    version: '2.0.0+ (ge6e51)',
    host_path: 'tftp/snponly.efi',
    capabilities: [
      { id: 'https', label: 'HTTPS 引导', present: true },
      { id: 'sign', label: '挑战签名', present: true },
    ],
  }],
  stage2: [
    { file: 'boot.ipxe', path: '/tftp/boot.ipxe', host_path: 'tftp/boot.ipxe', status: 'ok' },
    { file: 'boot.ipxe.cfg', path: '/tftp/boot.ipxe.cfg', host_path: 'tftp/boot.ipxe.cfg', status: 'ok' },
    { file: 'menu.ipxe', path: '/tftp/menu.ipxe', host_path: 'tftp/menu.ipxe', status: 'ok' },
  ],
  menu: { status: 'ok', items: 4 },
}

const AGENTS = [{
  id: 'storage-nvmet-01',
  enabled: true,
  health: 'ok',
  capabilities: { backend: 'nvmet', port: { trsvcid: '4421', tsas: 'tls1.3', ready: true } },
}]
const WORKERS = [{ worker_id: 'worker-01', readiness: 'ready' }]
const DEVICES = [{
  mac: '52:54:00:50:00:04',
  bound_worker_id: 'worker-01',
  last_seen: '2026-10-23T21:07:50+08:00',
}]
const PROBES = {
  '/tftp/boot.ipxe': true,
  '/tftp/boot.ipxe.cfg': true,
  '/tftp/menu.ipxe': true,
}
const NOW = Date.parse('2026-10-23T23:07:50+08:00')

const byId = (result) => Object.fromEntries(result.checks.map((check) => [check.id, check]))

test('五项齐全时整体正常，上报取最新一条', () => {
  const result = buildBootChecks({
    assets: ASSETS,
    agents: AGENTS,
    workers: WORKERS,
    devices: [
      { mac: 'aa:bb:cc:dd:ee:01', last_seen: '2026-10-20T08:00:00+08:00' },
      ...DEVICES,
    ],
    probes: PROBES,
    now: NOW,
  })
  assert.equal(result.overall, 'normal')
  assert.equal(result.counted, 5)
  assert.equal(result.normal, 5)
  const checks = byId(result)
  assert.equal(checks.firmware.detail.capsPresent, 2)
  assert.equal(checks.service.detail.ok, 3)
  assert.equal(checks.dataPlane.detail.port.tsas, 'tls1.3')
  assert.equal(checks.report.detail.mac, '52:54:00:50:00:04')
  assert.deepEqual(checks.report.detail.age, { value: 2, unit: 'hour' })
})

test('固件缺失为阻塞，能力缺口为降级', () => {
  const missing = byId(buildBootChecks({ assets: { ...ASSETS, firmware: [] }, now: NOW })).firmware
  assert.equal(missing.status, 'blocked')
  assert.equal(missing.detail.declared, false)

  const gap = {
    ...ASSETS,
    firmware: [{
      ...ASSETS.firmware[0],
      status: 'incomplete',
      capabilities: [
        { id: 'https', label: 'HTTPS 引导', present: true },
        { id: 'sign', label: '挑战签名', present: false },
      ],
    }],
  }
  const check = byId(buildBootChecks({ assets: gap, now: NOW })).firmware
  assert.equal(check.status, 'degraded')
  assert.deepEqual(check.detail.missing, ['挑战签名'])
})

test('脚本链缺失为阻塞，菜单不可解析为降级，HTTPS 取回失败为阻塞', () => {
  const broken = {
    ...ASSETS,
    stage2: [{ file: 'boot.ipxe', path: '/tftp/boot.ipxe', host_path: 'tftp/boot.ipxe', status: 'missing' }],
  }
  assert.equal(byId(buildBootChecks({ assets: broken, now: NOW })).service.status, 'blocked')

  const menu = { ...ASSETS, menu: { status: 'unavailable', items: 0 } }
  assert.equal(byId(buildBootChecks({ assets: menu, now: NOW })).service.status, 'degraded')

  const probes = { ...PROBES, '/tftp/menu.ipxe': false }
  const check = byId(buildBootChecks({ assets: ASSETS, probes, now: NOW })).service
  assert.equal(check.status, 'blocked')
  assert.deepEqual(check.detail.unreachable, ['/tftp/menu.ipxe'])
})

test('数据面按启用节点判定：无节点或缺端口均阻塞，部分就绪降级', () => {
  assert.equal(byId(buildBootChecks({ now: NOW })).dataPlane.status, 'unknown')
  assert.equal(byId(buildBootChecks({ agents: [], now: NOW })).dataPlane.status, 'blocked')

  const disabled = [{ ...AGENTS[0], enabled: false }]
  assert.equal(byId(buildBootChecks({ agents: disabled, now: NOW })).dataPlane.status, 'blocked')

  const down = [AGENTS[0], {
    id: 'storage-nvmet-02',
    enabled: true,
    health: 'ok',
    capabilities: { backend: 'nvmet', port: { trsvcid: '4421', tsas: 'tls1.3', ready: false } },
  }]
  const check = byId(buildBootChecks({ agents: down, now: NOW })).dataPlane
  assert.equal(check.status, 'degraded')
  assert.equal(check.detail.ready, 1)
  assert.equal(check.detail.down[0].id, 'storage-nvmet-02')
  assert.equal(check.detail.down[0].portReady, false)
})

test('Worker 未登记不计入判定，缺设备或缺盘降级，全部不就绪阻塞', () => {
  const empty = byId(buildBootChecks({ workers: [], now: NOW })).workers
  assert.equal(empty.status, 'unknown')
  assert.equal(empty.detail.empty, true)

  const partial = byId(buildBootChecks({
    workers: [WORKERS[0], { worker_id: 'worker-02', readiness: 'partial' }],
    now: NOW,
  })).workers
  assert.equal(partial.status, 'degraded')
  assert.deepEqual(partial.detail.notReady, [{ id: 'worker-02', readiness: 'partial' }])

  const none = byId(buildBootChecks({
    workers: [{ worker_id: 'worker-02', readiness: 'idle' }],
    now: NOW,
  })).workers
  assert.equal(none.status, 'blocked')
})

test('无上报记录不判定，无法判定的检查项不参与整体结论', () => {
  const noReport = byId(buildBootChecks({ devices: [], now: NOW })).report
  assert.equal(noReport.status, 'unknown')
  assert.equal(noReport.detail.reason, 'no-report')

  const result = buildBootChecks({ agents: AGENTS, workers: WORKERS, devices: DEVICES, now: NOW })
  assert.equal(result.counted, 3)
  assert.equal(result.normal, 3)
  assert.equal(result.overall, 'normal')
})

test('阻塞优先于降级', () => {
  const result = buildBootChecks({
    assets: { ...ASSETS, firmware: [] },
    agents: AGENTS,
    workers: [{ worker_id: 'worker-01', readiness: 'partial' }],
    devices: DEVICES,
    probes: PROBES,
    now: NOW,
  })
  assert.equal(result.overall, 'blocked')
  assert.equal(result.counted, 5)
  assert.equal(result.normal, 3)
})

test('每段证据都是非空对象，不混入裸字符串', () => {
  // 回归：Evidence 只读 part.text；裸字符串会让该段渲染为空。
  const t = (key) => key
  const normal = buildBootChecks({
    assets: ASSETS,
    agents: AGENTS,
    workers: WORKERS,
    devices: DEVICES,
    probes: PROBES,
    now: NOW,
  })
  for (const check of normal.checks) {
    const parts = evidenceParts(check, t, 'zh-CN')
    assert.ok(parts.length > 0, `${check.id} 无证据`)
    for (const part of parts) {
      assert.equal(typeof part, 'object', `${check.id} 混入裸字符串`)
      assert.equal(typeof part.text, 'string')
      assert.ok(part.text.length > 0, `${check.id} 有空证据`)
    }
  }
  const plane = evidenceParts(byId(normal).dataPlane, t, 'zh-CN').map((part) => part.text)
  assert.ok(plane.includes('TLS 1.3'))
  assert.ok(plane.includes('4421'))

  const empty = buildBootChecks({ now: NOW })
  for (const check of empty.checks) {
    const parts = evidenceParts(check, t, 'zh-CN')
    assert.ok(parts.length > 0 && parts.every((part) => part.text), `${check.id} 缺少空态证据`)
  }
})

test('相对时间与 TLS 标识格式化', () => {
  assert.deepEqual(relativeAge('2026-10-23T23:07:20+08:00', NOW), { value: 0, unit: 'minute' })
  assert.deepEqual(relativeAge('2026-10-23T21:07:50+08:00', NOW), { value: 2, unit: 'hour' })
  assert.deepEqual(relativeAge('2026-10-20T23:07:50+08:00', NOW), { value: 3, unit: 'day' })
  assert.equal(relativeAge('not-a-date', NOW), null)
  assert.equal(formatTsas('tls1.3'), 'TLS 1.3')
  assert.equal(formatTsas('plain'), 'plain')
  assert.equal(formatTsas(null), '')
})
