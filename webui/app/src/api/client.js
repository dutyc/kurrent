const BASE = '/api/cp';

// 会话失活全局钩子（AuthProvider 注册）：任何请求 401 → 自动回登录页
let unauthorizedHandler = null;

export function setUnauthorizedHandler(handler) {
  unauthorizedHandler = handler;
}

// 从错误响应体提取人类可读的 detail（与 CLI detailOf，cli/api.go 同语义）：
// - 字符串 detail 直用；
// - {agent, error} 对象契约（API 文档「LUN 管理」节）提取 error；
// - FastAPI 422 数组映射为 "field.path: msg"；
// - 其余对象 JSON 序列化——防御未知结构裸奔成 "[object Object]"。
export function extractDetail(payload) {
  if (!payload || typeof payload !== 'object') return '';
  const { detail } = payload;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        if (!d || typeof d !== 'object') return String(d);
        const msg = typeof d.msg === 'string' ? d.msg : JSON.stringify(d);
        const path = Array.isArray(d.loc) ? d.loc.slice(1).join('.') : '';
        return path ? `${path}: ${msg}` : msg;
      })
      .join('; ');
  }
  for (const c of [detail, payload.error, payload.message]) {
    if (typeof c === 'string' && c) return c;
    if (c && typeof c === 'object') {
      if (typeof c.error === 'string' && c.error) return c.error;
      try {
        return JSON.stringify(c);
      } catch {
        // 循环引用等无法序列化：继续尝试下一候选
      }
    }
  }
  try {
    // 无已知字段的非标准响应体：原样 JSON（空对象除外，交由调用侧落 HTTP 状态兜底）
    const text = JSON.stringify(payload);
    return text && text !== '{}' ? text : '';
  } catch {
    return '';
  }
}

async function request(path, options = {}) {
  const { method = 'GET', body, params, raw = false, sessionToken = '' } = options;

  let url = `${BASE}${path}`;
  if (params) {
    const sp = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') sp.append(k, v);
    });
    const qs = sp.toString();
    if (qs) url += `?${qs}`;
  }

  const headers = {};

  if (sessionToken) {
    // 仅 POST /session 使用：管理 token 换会话 Cookie（此后凭据在 HttpOnly Cookie 中，
    // 前端不存任何 token——localStorage 明文凭据已退役）
    headers['Authorization'] = `Bearer ${sessionToken}`;
  }

  if (body) {
    headers['Content-Type'] = 'application/json';
  }

  const res = await fetch(url, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
    credentials: 'same-origin', // 会话 Cookie（HttpOnly，登录态）随同源请求携带
  });

  if (!res.ok) {
    if (res.status === 401 && !raw && unauthorizedHandler) {
      unauthorizedHandler();
    }
    let detail = '';
    let payload = null;
    try {
      payload = await res.json();
      detail = extractDetail(payload);
    } catch {
      // 非 JSON 响应（如网关 HTML）：退回状态文本
      detail = res.statusText;
    }
    const error = new Error(detail || res.statusText || `HTTP ${res.status}`);
    error.status = res.status;
    // 原始响应体：detail 之外的字段（如放置失败的 excluded 逐节点原因）调用侧按需自取
    error.payload = payload;
    throw error;
  }

  return res.json();
}

// ===== Auth（会话与身份） =====
export function whoami() {
  // 探测当前凭据：浏览器携带管理证书（mTLS）→ 200 直接进入；否则 401 → 登录页
  return request('/whoami', { raw: true });
}

export function sessionLogin(token) {
  // 管理 token → POST /session 换 HttpOnly 会话 Cookie（TTL 7 天滑动续期）
  return request('/session', { method: 'POST', raw: true, sessionToken: token });
}

export function sessionLogout() {
  // 登出：清除会话 Cookie（无状态签名会话，服务端无吊销态）
  return request('/session', { method: 'DELETE', raw: true });
}

// ===== Health =====
export function healthz() {
  return request('/healthz');
}

// ===== Boot Vars =====
export function bootVars(params) {
  return request('/boot-vars', { params });
}

// 引导链状态：UEFI 固件能力、脚本链与菜单可用性（控制面解析 dnsmasq 与 iPXE 脚本后返回）
export function getBootAssets() {
  return request('/boot-assets');
}

// ===== Settings =====
export function getRegistrationWindow() {
  // 注册窗口状态: { open, opened_at, ttl_minutes, closes_at, remaining_seconds,
  //                max_devices, admitted, remaining_devices, quota_exhausted }
  // （TTL 到期自动关闭，懒计算；额度用尽后不再放行新设备）
  return request('/settings/registration-window');
}

export function openRegistrationWindow(ttlMinutes, maxDevices = null) {
  // TTL 1-60 分钟硬上限（代码层不可配永久）；max_devices 1-1000，null = 不限
  // 已开启返回 409（先关闭再开）
  return request('/settings/registration-window', {
    method: 'POST',
    body: { ttl_minutes: ttlMinutes, max_devices: maxDevices },
  });
}

export function closeRegistrationWindow() {
  return request('/settings/registration-window', { method: 'DELETE' });
}

export function getEnforcement() {
  // 设备身份验签强制开关（过渡期兼容：关闭时无密钥设备照现状放行）
  return request('/settings/enforcement');
}

export function setEnforcement(enabled) {
  return request('/settings/enforcement', { method: 'PUT', body: { enabled } });
}

// ===== Agents =====
export function getAgents(live = true) {
  return request('/agents', { params: { live } });
}

export function updateAgent(agentId, data) {
  return request(`/agents/${agentId}`, { method: 'PUT', body: data });
}

export function issueBootstrapToken(agentId) {
  // 签发 bootstrap token：绑定节点身份（agent_id = 节点声明 metadata.name），每次新签
  return request('/pki/tokens', { method: 'POST', body: { usage: 'bootstrap', agent_id: agentId } });
}

export function probeAgent(data) {
  return request('/agents/probe', { method: 'POST', body: data });
}

// ===== Credentials（token 台账：列表 / 吊销 / 签发管理 token） =====
export function getTokens() {
  // 未过期 token 条目：{token_id, usage, groups, scopes, uses, max_uses, component,
  // agent_id, name, created_at, expires_at, last_used_at}；不返回明文与 hash
  return request('/pki/tokens');
}

export function revokeToken(tokenId) {
  // 吊销 = 删条目（立即失效）；不存在 → 404
  return request(`/pki/tokens/${tokenId}`, { method: 'DELETE' });
}

export function issueAdminToken() {
  // 签发管理 token（认证器链 Bearer / WebUI 登录）；明文仅本次响应可见
  return request('/pki/tokens', { method: 'POST', body: { usage: 'admin' } });
}

// ===== Sessions（服务端会话登记） =====
export function getSessions() {
  // {session_id, user, groups, created_at, last_seen, expires_at, cert_fp}
  return request('/sessions');
}

export function revokeSession(sessionId) {
  // 删除登记：下一请求 401
  return request(`/sessions/${sessionId}`, { method: 'DELETE' });
}

export function revokeAllSessions() {
  // 全部踢下线：清空登记并递增 epoch
  return request('/sessions', { method: 'DELETE' });
}

// ===== Agent LUNs =====
export function getAgentLuns(agentId) {
  return request(`/agents/${agentId}/luns`);
}

export function createAgentDiskLun(agentId, data) {
  return request(`/agents/${agentId}/luns/disk`, { method: 'POST', body: data });
}

export function createAgentCdLun(agentId, data) {
  return request(`/agents/${agentId}/luns/cd`, { method: 'POST', body: data });
}

export function deleteAgentLun(agentId, iqn, deleteFile = false) {
  return request(`/agents/${agentId}/luns`, {
    method: 'DELETE',
    params: { iqn, delete_file: deleteFile },
  });
}

export function scanAgentLuns(agentId) {
  return request(`/agents/${agentId}/luns/scan`, { method: 'POST' });
}

// ===== Masters =====
export function getMasters() {
  // 聚合列出全部启用磁盘角色 Agent 上的母盘: { agents: [{ agent, storager_ip, masters: [{name,size,mtime,os?,os_version?}] }] }
  return request('/masters');
}

export function setMasterTag(agentId, masterName, os, osVersion = '', remark = '') {
  // 登记母盘标签（控制面台账，备注性质）
  return request(`/agents/${agentId}/masters/${encodeURIComponent(masterName)}/tag`, {
    method: 'PUT',
    body: { os, os_version: osVersion, remark },
  });
}

export function clearMasterTag(agentId, masterName) {
  return request(`/agents/${agentId}/masters/${encodeURIComponent(masterName)}/tag`, {
    method: 'DELETE',
  });
}

// ===== Workers =====
export function createWorker(data) {
  return request('/workers', { method: 'POST', body: data });
}

export function batchCreateWorkers(data) {
  // 批量创建：{ count, name_prefix, arch?, macs? } → { succeeded, skipped, failed }
  return request('/workers/batch', { method: 'POST', body: data });
}

export function getWorkers() {
  return request('/workers');
}

export function getWorker(workerId) {
  return request(`/workers/${workerId}`);
}

export function createWorkerDisk(workerId, data) {
  return request(`/workers/${workerId}/luns/disk`, { method: 'POST', body: data });
}

export function batchCreateWorkerDisks(data) {
  // 批量创建：{ type, os, name|size, targets: [{worker_id, agent}] }
  return request('/workers/luns/disk/batch', { method: 'POST', body: data });
}

export function deleteWorkerDisk(workerId, osTag, deleteFile = false, ignoreMissing = false) {
  return request(`/workers/${workerId}/luns/disk/${encodeURIComponent(osTag)}`, {
    method: 'DELETE',
    params: {
      delete_file: deleteFile,
      ignore_missing_target: ignoreMissing,
    },
  });
}

export function setWorkerDefaultBoot(workerId, data) {
  // disk(os_tag) / menu_default / menu_timeout 可设可清；传 null 清除对应项
  return request(`/workers/${workerId}/default-disk`, { method: 'PUT', body: data });
}

export function updateWorkerMac(workerId, mac) {
  // 修改 MAC 绑定（hostname 不变），审计记录旧/新 MAC
  return request(`/workers/${workerId}/mac`, { method: 'PUT', body: { mac } });
}

// ===== Worker 凭据（NVMe-oF DHHC-1：查看 / 设置 / 吊销） =====
export function getWorkerCredential(workerId) {
  // { worker_id, exists, secret_hash?, created_at?, updated_at?,
  //   push: { result: ok|failed|pending|n/a|none, updated_at, agents: [{agent_id, result, error?, updated_at}] } }
  return request(`/workers/${encodeURIComponent(workerId)}/credential`);
}

export function setWorkerCredential(workerId, secret) {
  // 设置/轮换 DHHC-1 密钥；响应含逐 Agent 推送状态（未送达由控制面对账线程自动重试）
  return request(`/workers/${encodeURIComponent(workerId)}/credential`, {
    method: 'PUT',
    body: { secret },
  });
}

export function revokeWorkerCredential(workerId) {
  return request(`/workers/${encodeURIComponent(workerId)}/credential`, { method: 'DELETE' });
}

// crc32：CRC-32/ISO-HDLC（与 zlib.crc32 一致）；DHHC-1 载荷尾 4 字节为其小端序
export function crc32(bytes) {
  let crc = 0xffffffff;
  for (const b of bytes) {
    crc ^= b;
    for (let i = 0; i < 8; i++) {
      crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
    }
  }
  return (crc ^ 0xffffffff) >>> 0;
}

// generateDHHC1：就地生成 32 字节密钥（DHHC-1:01:<base64(key + CRC32 小端)>，固件 2.1 契约）。
// 明文不经浏览器持久化：仅随本次请求发给控制面，固件经 /boot-vars 注入。
export function generateDHHC1() {
  const key = crypto.getRandomValues(new Uint8Array(32));
  const payload = new Uint8Array(36);
  payload.set(key);
  new DataView(payload.buffer).setUint32(32, crc32(key), true);
  return 'DHHC-1:01:' + btoa(String.fromCharCode(...payload));
}

export function getWorkerStatus(workerId) {
  return request(`/workers/${workerId}/status`);
}

export function deleteWorker(workerId, deleteDisk = false, ignoreMissing = false) {
  return request(`/workers/${workerId}`, {
    method: 'DELETE',
    params: {
      delete_disk: deleteDisk,
      ignore_missing_target: ignoreMissing,
    },
  });
}

export function batchDeleteWorkers(workerIds, deleteDisk = false, ignoreMissing = true) {
  return request('/workers/delete/batch', {
    method: 'POST',
    body: {
      worker_ids: workerIds,
      delete_disk: deleteDisk,
      ignore_missing_target: ignoreMissing,
    },
  });
}

// ===== Devices（设备池） =====
export function getDevices(state = 'all') {
  return request('/devices', { params: { state } });
}

export function getDevice(mac) {
  return request(`/devices/${encodeURIComponent(mac)}`);
}

export function createDevice(data) {
  return request('/devices', { method: 'POST', body: data });
}

export function importDevices(entries) {
  return request('/devices/import', { method: 'POST', body: { entries } });
}

export function bindDevice(mac, workerId, force = false) {
  return request(`/devices/${encodeURIComponent(mac)}/bind`, {
    method: 'POST',
    params: { worker_id: workerId, force },
  });
}

export function unbindDevice(mac) {
  return request(`/devices/${encodeURIComponent(mac)}/bind`, { method: 'DELETE' });
}

export function batchBindPreview(data) {
  return request('/devices/bind/batch/preview', { method: 'POST', body: data });
}

export function batchBind(data) {
  return request('/devices/bind/batch', { method: 'POST', body: data });
}

// ===== Operations =====
export function getOperations(since = 0, limit = 50, mac = null) {
  const params = mac ? { since, limit, mac } : { since, limit };
  return request('/operations', { params });
}
