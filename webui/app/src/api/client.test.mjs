import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  extractDetail,
  crc32,
  generateDHHC1,
  getComponentCerts,
  revokeComponentCert,
  revokeUserCert,
} from './client.js'
import { deriveEncryptionPosture } from './posture.js'

// extractDetail 与 CLI detailOf（cli/api.go）同语义：[object Object] 回归的锚点。

test('字符串 detail 直用', () => {
  assert.equal(extractDetail({ detail: 'worker not found: worker-04' }), 'worker not found: worker-04')
})

test('FastAPI 422 数组映射为 "field.path: msg"', () => {
  const payload = { detail: [
    { loc: ['body', 'iqn'], msg: 'field required', type: 'missing' },
    { loc: ['body', 'size'], msg: 'must be a string', type: 'string_type' },
  ] }
  assert.equal(extractDetail(payload), 'iqn: field required; size: must be a string')
})

test('{agent, error} 契约提取 error（Agent 代理错误透传族）', () => {
  const payload = { detail: { agent: 'storage-lio-01', error: 'iqn base mismatch: ...' } }
  assert.equal(extractDetail(payload), 'iqn base mismatch: ...')
})

test('对象 detail 无字符串 error 字段 → JSON 文本', () => {
  assert.equal(
    extractDetail({ detail: { agents: [], error: 123 } }),
    '{"agents":[],"error":123}',
  )
})

test('顶层 error 键（非 detail 结构）直用', () => {
  assert.equal(extractDetail({ error: 'boom' }), 'boom')
})

test('空对象 → ""（调用侧落 HTTP 状态兜底，不显示 "{}"）', () => {
  assert.equal(extractDetail({}), '')
})

test('无已知字段的响应体 → 原样 JSON', () => {
  assert.equal(extractDetail({ foo: 1 }), '{"foo":1}')
})

test('畸形输入不崩（loc 非数组 / 元素非对象 / 非对象 payload）', () => {
  assert.equal(extractDetail({ detail: [{ loc: 'body.iqn', msg: 'bad' }] }), 'bad')
  assert.equal(extractDetail({ detail: ['plain string element'] }), 'plain string element')
  assert.equal(extractDetail(null), '')
  assert.equal(extractDetail('oops'), '')
})

// DHHC-1 生成（固件 2.1 契约）：前缀 + 两位类型 + 36 字节载荷 + CRC32 小端终值。

test('crc32 对齐标准校验值（"123456789" → 0xCBF43926）', () => {
  assert.equal(crc32(new TextEncoder().encode('123456789')), 0xcbf43926)
})

test('generateDHHC1 格式与 CRC 自检通过', () => {
  const secret = generateDHHC1()
  assert.match(secret, /^DHHC-1:01:/)
  const raw = Uint8Array.from(atob(secret.slice('DHHC-1:01:'.length)), (c) => c.charCodeAt(0))
  assert.equal(raw.length, 36)
  const key = raw.slice(0, 32)
  const view = new DataView(raw.buffer, 32, 4)
  assert.equal(view.getUint32(0, true), crc32(key))
})

test('generateDHHC1 两次生成不同（随机源生效）', () => {
  assert.notEqual(generateDHHC1(), generateDHHC1())
})

// 引导加密姿态（段 2.5）：结论只认 /boot-vars 投影，port/credential 仅用于归因。
// 变异验证锚点——把结论来源改成「只看 credentialExists」或「只看 port.ready」时，
// 对应「端口未就绪」/「无密钥」两例必须变红，证明测试在测投影这个 oracle。

const NVME_DISK = { hasNvmeofDisk: true, backend: 'nvmet' }
const TLS_PORT_OK = { trsvcid: '4421', tsas: 'tls1.3', ready: true }
const TLS_PORT_DOWN = { trsvcid: '4421', tsas: 'tls1.3', ready: false }
const PROJECTION_ON = { nbft_tls_psk: 'AAAA', nbft_tls_port: '4421' }

test('加密生效：投影含两键（与 port/credential 状态无关的唯一下结论来源）', () => {
  assert.deepEqual(
    deriveEncryptionPosture({
      ...NVME_DISK,
      port: TLS_PORT_OK,
      credentialExists: true,
      bootVars: PROJECTION_ON,
    }),
    { state: 'encrypted', reason: null },
  )
})

test('将拒绝引导·端口未就绪：投影为空 + port.ready=false（即使有密钥）', () => {
  assert.deepEqual(
    deriveEncryptionPosture({
      ...NVME_DISK,
      port: TLS_PORT_DOWN,
      credentialExists: true,
      bootVars: {},
    }),
    { state: 'refused', reason: 'port_not_ready' },
  )
})

test('将拒绝引导·无密钥：投影为空 + 端口就绪但 credentialExists=false', () => {
  assert.deepEqual(
    deriveEncryptionPosture({
      ...NVME_DISK,
      port: TLS_PORT_OK,
      credentialExists: false,
      bootVars: {},
    }),
    { state: 'refused', reason: 'no_credential' },
  )
})

test('不适用：非 nvmet 后端（stgt/lio 走 iSCSI）', () => {
  assert.deepEqual(
    deriveEncryptionPosture({
      hasNvmeofDisk: true,
      backend: 'lio',
      port: null,
      credentialExists: true,
      bootVars: { base_iqn: 'iqn.2024-01.local' },
    }),
    { state: 'n/a', reason: null },
  )
})

test('不适用：该 Worker 无 NVMe-oF 盘（hasNvmeofDisk=false）', () => {
  assert.deepEqual(
    deriveEncryptionPosture({ hasNvmeofDisk: false, backend: undefined, bootVars: {} }),
    { state: 'n/a', reason: null },
  )
})

test('归因不越权：两端布尔都满足却投影为空 → 如实报 projection_empty，不编造原因', () => {
  assert.deepEqual(
    deriveEncryptionPosture({
      ...NVME_DISK,
      port: TLS_PORT_OK,
      credentialExists: true,
      bootVars: {},
    }),
    { state: 'refused', reason: 'projection_empty' },
  )
})

test('不可用：投影未取回（null）不得伪装成「加密未生效」', () => {
  assert.deepEqual(
    deriveEncryptionPosture({
      ...NVME_DISK,
      port: TLS_PORT_OK,
      credentialExists: true,
      bootVars: null,
    }),
    { state: 'unavailable', reason: null },
  )
})

test('不可用：节点后端未知（未登记 / 能力未取回）', () => {
  assert.deepEqual(
    deriveEncryptionPosture({
      hasNvmeofDisk: true,
      backend: undefined,
      port: undefined,
      credentialExists: true,
      bootVars: {},
    }),
    { state: 'unavailable', reason: null },
  )
})

// 证书台账请求形状：吊销走 DELETE + reason 查询参数；列表不误用写方法。

async function captureRequest(fn) {
  const calls = []
  const original = globalThis.fetch
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options })
    return { ok: true, json: async () => [] }
  }
  try {
    await fn()
  } finally {
    globalThis.fetch = original
  }
  return calls
}

test('revokeUserCert：DELETE /pki/users/<cn>?reason=...', async () => {
  const calls = await captureRequest(() => revokeUserCert('alice', 'key_compromise'))
  assert.equal(calls[0].options.method, 'DELETE')
  assert.equal(calls[0].url, '/api/cp/pki/users/alice?reason=key_compromise')
})

test('revokeComponentCert：节点 ID 编码后走 DELETE + reason', async () => {
  const calls = await captureRequest(() => revokeComponentCert('node a/b', 'superseded'))
  assert.equal(calls[0].options.method, 'DELETE')
  assert.equal(calls[0].url, '/api/cp/pki/components/node%20a%2Fb?reason=superseded')
})

test('getComponentCerts：GET /pki/components', async () => {
  const calls = await captureRequest(() => getComponentCerts())
  assert.equal(calls[0].options.method, 'GET')
  assert.equal(calls[0].url, '/api/cp/pki/components')
})
