import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  agentLabels,
  isManagementToken,
  newNodeId,
  nodeName,
  splitSessions,
  splitTokens,
  tokenPurpose,
  tokenUsable,
} from './identity.js'

const token = (overrides = {}) => ({
  token_id: 'aaaaaa',
  usage: ['enroll'],
  component: 'agent',
  agent_id: '7f3a9c2b10de',
  groups: [],
  scopes: [],
  expires_at: '2026-10-24T17:41:00Z',
  last_used_at: '',
  ...overrides,
})

test('tokenPurpose：管理 token 按 scope/组区分，节点 token 按组件区分', () => {
  assert.equal(tokenPurpose(token({ usage: ['admin'], groups: ['admin'] })), 'admin')
  assert.equal(tokenPurpose(token({ usage: ['admin'], groups: ['readonly'] })), 'readonly')
  assert.equal(tokenPurpose(token({ usage: ['admin'], scopes: ['enroll'] })), 'enroll')
  assert.equal(tokenPurpose(token({ component: 'nvmet-host' })), 'derived')
  assert.equal(tokenPurpose(token()), 'bootstrap')
})

test('splitTokens：管理 token 与节点 token 分流并按身份排序', () => {
  const { management, node } = splitTokens([
    token({ token_id: 'cccccc', usage: ['admin'], groups: ['admin'], agent_id: '' }),
    token({ token_id: 'bbbbbb', agent_id: '9c1e4a7b2d60', component: 'agent' }),
    token({ token_id: 'aaaaaa', agent_id: '7f3a9c2b10de', component: 'agent' }),
    token({ token_id: 'dddddd', agent_id: '7f3a9c2b10de', component: 'nvmet-host' }),
  ])
  assert.deepEqual(management.map((item) => item.token_id), ['cccccc'])
  assert.deepEqual(node.map((item) => item.token_id), ['aaaaaa', 'dddddd', 'bbbbbb'])
})

test('splitTokens：一次性令牌用尽后不再出现在节点令牌中', () => {
  const { node } = splitTokens([
    token({ token_id: 'aaaaaa', uses: 1, max_uses: 1 }),
    token({ token_id: 'bbbbbb', uses: 0, max_uses: 1 }),
    token({ token_id: 'cccccc', uses: 0, max_uses: 0 }),
  ])
  assert.deepEqual(node.map((item) => item.token_id), ['bbbbbb', 'cccccc'])
  assert.equal(tokenUsable(token({ uses: 1, max_uses: 1 })), false)
  assert.equal(tokenUsable(token({ uses: 0, max_uses: 0 })), true)
})

test('isManagementToken：usage 缺省/非数组均按节点 token 处理', () => {
  assert.equal(isManagementToken(token({ usage: ['admin'] })), true)
  assert.equal(isManagementToken(token({ usage: undefined })), false)
  assert.equal(isManagementToken(token({ usage: ['enroll'] })), false)
})

test('splitSessions：有证书指纹的是用户会话，空指纹的是令牌会话', () => {
  const { user, automation } = splitSessions([
    { session_id: 's1', user: 'alice', cert_fp: 'aa', created_at: '2026-10-24T02:00:00Z' },
    { session_id: 's2', user: 'token:abcdef', cert_fp: '', created_at: '2026-10-24T01:00:00Z' },
  ])
  assert.deepEqual(user.map((item) => item.session_id), ['s1'])
  assert.deepEqual(automation.map((item) => item.session_id), ['s2'])
})

test('agentLabels / nodeName：备注优先，缺失回退机器身份', () => {
  const labels = agentLabels([
    { id: '7f3a9c2b10de', remark: '主存储节点' },
    { id: '9c1e4a7b2d60', remark: '  ' },
  ])
  assert.equal(nodeName('7f3a9c2b10de', labels), '主存储节点')
  assert.equal(nodeName('9c1e4a7b2d60', labels), '9c1e4a7b2d60')
  assert.equal(nodeName('unknown', labels), 'unknown')
})

test('newNodeId：12 位小写 hex，多次生成不重复', () => {
  const ids = new Set(Array.from({ length: 20 }, () => newNodeId()))
  assert.equal(ids.size, 20)
  for (const id of ids) assert.match(id, /^[0-9a-f]{12}$/)
})
