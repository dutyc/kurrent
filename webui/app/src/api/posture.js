// NVMe-oF 引导加密姿态（段 2.5）。
//
// 结论**只认控制面 /boot-vars 投影**——它是固件实际拿到的东西（同时含 nbft_tls_psk 与
// nbft_tls_port 即加密生效）。不要在调用侧复制控制面的裁决逻辑（段 2.4 在校验「有认证密钥
// + 数据面端口就绪」之后决定是否下发引导变量），前端复制一份必然漂移。
//
// capabilities.port.ready 与 credential.exists 两个布尔**只用于归因**，不用于下结论：
// 投影为空时，!port.ready → 「数据面端口未就绪」；否则 !credential.exists → 「无认证密钥」；
// 两者都满足却仍为空 → 如实报「投影为空」（控制面 bug），不编造原因。
//
// 取不到数据（节点能力 / 引导变量未取回）→ 'unavailable'，绝不显示成「加密未生效」。

// state：encrypted（加密生效）/ refused（将拒绝引导）/ n/a（非 NVMe-oF）/ unavailable（不可判定）
// reason：port_not_ready | no_credential | projection_empty（仅 refused 有值）
export function deriveEncryptionPosture({ hasNvmeofDisk, backend, port, credentialExists, bootVars }) {
  if (!hasNvmeofDisk) return { state: 'n/a', reason: null }
  // backend 未知（节点未登记 / 能力未取回）→ 不可判定；已知且非 nvmet → 不适用
  if (!backend) return { state: 'unavailable', reason: null }
  if (backend !== 'nvmet') return { state: 'n/a', reason: null }

  // oracle 取不到（null = 请求失败）→ 不可用；{} = 已取到但投影为空（拒绝引导）
  if (!bootVars) return { state: 'unavailable', reason: null }
  if (bootVars.nbft_tls_psk && bootVars.nbft_tls_port) return { state: 'encrypted', reason: null }

  if (!(port && port.ready === true)) return { state: 'refused', reason: 'port_not_ready' }
  if (credentialExists === false) return { state: 'refused', reason: 'no_credential' }
  return { state: 'refused', reason: 'projection_empty' }
}
