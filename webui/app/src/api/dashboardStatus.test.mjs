import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  agentSummary,
  attestationFailures,
  chainSummary,
  deviceSummary,
  windowModel,
  workerSummary,
} from './dashboardStatus.js'

test('Worker 就绪按 readiness 统计，未就绪含 partial 与 idle', () => {
  assert.deepEqual(workerSummary(null), { total: 0, ready: 0, notReady: 0 })
  assert.deepEqual(
    workerSummary([
      { worker_id: 'w1', readiness: 'ready' },
      { worker_id: 'w2', readiness: 'partial' },
      { worker_id: 'w3', readiness: 'idle' },
    ]),
    { total: 3, ready: 1, notReady: 2 }
  )
})

test('设备池按 state 分类，未知状态不计入三类', () => {
  assert.deepEqual(deviceSummary(null), { total: 0, pooled: 0, bound: 0, revoked: 0 })
  const summary = deviceSummary([
    { state: 'pooled' },
    { state: 'pooled' },
    { state: 'bound' },
    { state: 'revoked' },
    { state: 'unknown' },
  ])
  assert.equal(summary.total, 5)
  assert.deepEqual(
    [summary.pooled, summary.bound, summary.revoked],
    [2, 1, 1]
  )
})

test('存储节点统计健康数与 nvmet TLS 端口就绪数', () => {
  const summary = agentSummary([
    { health: 'ok', capabilities: { backend: 'nvmet', port: { ready: true } } },
    { health: 'ok', capabilities: { backend: 'nvmet', port: { ready: false } } },
    { health: 'error', capabilities: { backend: 'iscsi' } },
  ])
  assert.deepEqual(summary, { total: 3, healthy: 2, tlsReady: 1, tlsTotal: 2, portDown: 1 })
  assert.equal(agentSummary(undefined).total, 0)
})

test('证明异常只计末次失败：PCR 不符或校验出错', () => {
  assert.equal(attestationFailures(null), null)
  const count = attestationFailures([
    { attestation: { state: 'proven', last_reason: null } },
    { attestation: { state: 'mismatch', last_reason: 'pcr mismatch' } },
    { attestation: { state: 'unproven', last_reason: 'quote invalid' } },
    { attestation: { state: 'unproven', last_reason: null } },
    {},
  ])
  assert.equal(count, 2)
})

test('注册窗口模型：开启给剩余分钟，关闭保留上次时长与额度', () => {
  assert.equal(windowModel(null), null)

  const open = windowModel({
    open: true,
    remaining_seconds: 1470,
    admitted: 2,
    max_devices: 5,
    ttl_minutes: 30,
  })
  assert.deepEqual(open, { open: true, minutes: 25, admitted: 2, max: 5 })

  const unlimited = windowModel({ open: true, remaining_seconds: 9, admitted: 3, max_devices: null })
  assert.deepEqual(unlimited, { open: true, minutes: 1, admitted: 3, max: null })

  const closed = windowModel({
    open: false,
    admitted: 5,
    max_devices: 5,
    ttl_minutes: 30,
    remaining_seconds: 0,
  })
  assert.deepEqual(closed, { open: false, ttl: 30, admitted: 5, max: 5 })

  const never = windowModel({ open: false, admitted: 0, max_devices: null, ttl_minutes: null })
  assert.deepEqual(never, { open: false, ttl: null, admitted: 0, max: null })
})

test('引导链条摘要：检查数、未通过数与固件能力', () => {
  assert.equal(chainSummary(null), null)

  const firmware = {
    id: 'firmware',
    status: 'degraded',
    detail: {
      declared: true,
      firmwares: [
        { file: 'snponly.efi', capsPresent: 6, capsTotal: 6 },
        { file: 'ipxe.efi', capsPresent: 5, capsTotal: 6 },
      ],
    },
  }
  const summary = chainSummary({
    checks: [{ id: 'service', status: 'normal', detail: {} }, firmware],
    overall: 'degraded',
    counted: 5,
    normal: 4,
  })
  assert.deepEqual(summary, {
    overall: 'degraded',
    counted: 5,
    failed: 1,
    firmwares: ['snponly.efi 6/6', 'ipxe.efi 5/6'],
  })

  const undeclared = chainSummary({
    checks: [{ id: 'firmware', status: 'blocked', detail: { declared: false } }],
    overall: 'blocked',
    counted: 5,
    normal: 4,
  })
  assert.deepEqual(undeclared.firmwares, [])
})
