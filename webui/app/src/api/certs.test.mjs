import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  certState,
  groupComponentCerts,
  groupControlCerts,
  groupUserCerts,
  revokedCertCount,
} from './certs.js'

const NOW = Date.parse('2026-10-24T00:00:00Z')

const cert = (overrides = {}) => ({
  cn: 'admin',
  serial: '01',
  created_at: '2026-01-01T00:00:00Z',
  expires_at: '2027-01-01T00:00:00Z',
  revoked_at: '',
  reason: '',
  ...overrides,
})

test('certState：吊销优先于过期，过期按 expires_at 判定', () => {
  assert.equal(certState(cert({ revoked_at: '2026-02-01T00:00:00Z', expired: true }), NOW), 'revoked')
  assert.equal(certState(cert({ expires_at: '2026-10-01T00:00:00Z' }), NOW), 'expired')
  assert.equal(certState(cert({ expired: true }), NOW), 'expired')
  assert.equal(certState(cert(), NOW), 'ok')
})

test('groupUserCerts：按 CN 取最新证书，组来自台账', () => {
  const rows = groupUserCerts([
    cert({ cn: 'alice', serial: '02', created_at: '2026-03-01T00:00:00Z', groups: ['admin'] }),
    cert({ cn: 'alice', serial: '01', created_at: '2026-01-01T00:00:00Z' }),
    cert({ cn: 'bob', serial: '03', expires_at: '2026-09-01T00:00:00Z', groups: ['viewer'] }),
  ], NOW)
  assert.deepEqual(rows.map((row) => row.cn), ['alice', 'bob'])
  assert.equal(rows[0].total, 1)
  assert.equal(rows[0].certs[0].serial, '02')
  assert.deepEqual(rows[0].groups, ['admin'])
  assert.equal(rows[0].status, 'ok')
  assert.equal(rows[1].status, 'expired')
  assert.deepEqual(rows[1].serials, ['03'])
})

test('groupUserCerts：最新证书已吊销 → 已吊销且无可用序列号', () => {
  const [row] = groupUserCerts([
    cert({ cn: 'carol', serial: '09', revoked_at: '2026-10-01T00:00:00Z', reason: 'superseded' }),
  ], NOW)
  assert.equal(row.status, 'revoked')
  assert.equal(row.reason, 'superseded')
  assert.deepEqual(row.serials, [])
})

test('groupComponentCerts：每类取最新，历史过期证书不影响状态', () => {
  const [row] = groupComponentCerts([
    cert({ cn: 'agent-a', agent_id: 'node-a', kind: 'client', serial: '01',
      expires_at: '2026-06-01T00:00:00Z' }),
    cert({ cn: 'agent-a', agent_id: 'node-a', kind: 'client', serial: '04',
      created_at: '2026-05-01T00:00:00Z' }),
    cert({ cn: 'nvmet-a', agent_id: 'node-a', kind: 'serving', serial: '02',
      created_at: '2026-05-01T00:00:00Z' }),
  ], NOW)
  assert.equal(row.status, 'ok')
  assert.equal(row.total, 2)
  assert.deepEqual(row.kinds, ['client', 'serving'])
  assert.deepEqual(row.serials.sort(), ['02', '04'])
  assert.equal(row.expiresAt, '2027-01-01T00:00:00Z')
})

test('groupComponentCerts：agent 与 nvmet-host 各自的 client/serving 都保留', () => {
  const [row] = groupComponentCerts([
    cert({ cn: 'agent-a', agent_id: 'node-a', kind: 'client', serial: '01' }),
    cert({ cn: 'agent-a', agent_id: 'node-a', kind: 'serving', serial: '02' }),
    cert({ cn: 'nvmet-a', agent_id: 'node-a', kind: 'client', serial: '03',
      created_at: '2026-02-01T00:00:00Z' }),
    cert({ cn: 'nvmet-a', agent_id: 'node-a', kind: 'serving', serial: '04',
      created_at: '2026-02-01T00:00:00Z' }),
  ], NOW)
  assert.equal(row.total, 4)
  assert.equal(row.activeCount, 4)
  assert.deepEqual(row.serials.sort(), ['01', '02', '03', '04'])
  assert.deepEqual(row.kinds, ['client', 'serving'])
})

test('groupComponentCerts：一张吊销一张有效 → 部分可用', () => {
  const [row] = groupComponentCerts([
    cert({ agent_id: 'node-b', kind: 'client', serial: '01',
      revoked_at: '2026-10-01T00:00:00Z', reason: 'key_compromise' }),
    cert({ agent_id: 'node-b', kind: 'serving', serial: '02' }),
  ], NOW)
  assert.equal(row.status, 'partial')
  assert.equal(row.activeCount, 1)
  assert.equal(row.reason, 'key_compromise')
  assert.deepEqual(row.serials, ['02'])
  assert.deepEqual(row.activeKinds, ['serving'])
})

test('groupComponentCerts：两类全失效 → 已吊销（含过期）', () => {
  const [row] = groupComponentCerts([
    cert({ agent_id: 'node-c', kind: 'client', serial: '01',
      revoked_at: '2026-10-01T00:00:00Z' }),
    cert({ agent_id: 'node-c', kind: 'serving', serial: '02',
      expires_at: '2026-08-01T00:00:00Z' }),
  ], NOW)
  assert.equal(row.status, 'revoked')
  assert.equal(row.activeCount, 0)
  // 未吊销的过期证书仍在吊销目标内（API 按「未吊销」选择）
  assert.deepEqual(row.serials, ['02'])
})

test('groupControlCerts：按 CN 取最新并保留序列号', () => {
  const [row] = groupControlCerts([
    cert({ cn: 'control-plane', serial: '07', created_at: '2026-04-01T00:00:00Z' }),
    cert({ cn: 'control-plane', serial: '05', created_at: '2026-01-01T00:00:00Z' }),
  ], NOW)
  assert.equal(row.certs[0].serial, '07')
  assert.deepEqual(row.serials, ['07'])
})

test('revokedCertCount：统计历史吊销证书数（不是身份数）', () => {
  const users = [cert({ revoked_at: '2026-01-01T00:00:00Z' }), cert({ serial: '02' })]
  const nodes = [cert({ revoked_at: '2026-02-01T00:00:00Z' })]
  assert.equal(revokedCertCount(users, nodes, []), 2)
})
