import { test } from 'node:test'
import assert from 'node:assert/strict'

import { extractDetail, crc32, generateDHHC1 } from './client.js'

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
