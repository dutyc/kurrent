import { useState, useEffect } from 'react'
import { useParams, Link, useNavigate } from 'react-router-dom'
import {
  getWorker,
  getWorkerStatus,
  deleteWorker,
  bootVars,
  createWorkerDisk,
  deleteWorkerDisk,
  setWorkerDefaultBoot,
  getAgents,
  getMasters,
  updateWorkerMac,
  getWorkerCredential,
  setWorkerCredential,
  revokeWorkerCredential,
  generateDHHC1,
} from '../api/client'
import { useI18n } from '../i18n'
import Button from '../components/Button'
import Card from '../components/Card'
import Badge from '../components/Badge'
import Divider from '../components/Divider'
import CodeBlock from '../components/CodeBlock'
import ConfirmAction from '../components/ConfirmAction'
import EmptyState from '../components/EmptyState'
import Modal from '../components/Modal'
import Input from '../components/Input'
import Select from '../components/Select'
import './WorkerDetail.css'

// 母盘文件大小格式化（与 Agent 详情页 LUN 列表同款）
function formatSize(bytes) {
  if (bytes == null) return '—'
  const gb = bytes / 1024 ** 3
  if (gb >= 1) return `${gb.toFixed(1)} GB`
  const mb = bytes / 1024 ** 2
  if (mb >= 1) return `${mb.toFixed(0)} MB`
  return `${bytes} B`
}

function InfoRow({ label, value, mono = false }) {
  if (value === undefined || value === null) return null
  return (
    <div className="info-row">
      <span className="info-label">{label}</span>
      <span className={`info-value${mono ? ' info-mono' : ''}`}>{String(value)}</span>
    </div>
  )
}

export default function WorkerDetail() {
  const { t } = useI18n()
  const { id } = useParams()
  const navigate = useNavigate()
  const [worker, setWorker] = useState(null)
  const [status, setStatus] = useState(null)
  const [bootVarsCode, setBootVarsCode] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [statusLoading, setStatusLoading] = useState(false)
  const [agentsList, setAgentsList] = useState([])
  const [mastersData, setMastersData] = useState(null)
  const [diskForm, setDiskForm] = useState({ os: 'ubuntu', os_version: '', type: 'empty', name: '', size: '40G', disk_agent: '' })
  const [creatingDisk, setCreatingDisk] = useState(false)
  const [diskCreateError, setDiskCreateError] = useState(null)
  const [deletingDisk, setDeletingDisk] = useState(null)
  const [bootForm, setBootForm] = useState({ disk: '', menu_default: '', menu_timeout: '', clear_timeout: false })
  const [savingBoot, setSavingBoot] = useState(false)
  const [bootSaveError, setBootSaveError] = useState(null)
  // MAC 绑定编辑（修改后审计记录旧/新 MAC）
  const [macEdit, setMacEdit] = useState(false)
  const [macValue, setMacValue] = useState('')
  const [macSaving, setMacSaving] = useState(false)
  const [macError, setMacError] = useState(null)
  // NVMe-oF 认证凭据（DHHC-1：状态 + 推送投影 + 生成设置/吊销）
  const [cred, setCred] = useState(null)
  const [credBusy, setCredBusy] = useState(false)
  const [credError, setCredError] = useState(null)
  // 分类页（与 Agent 详情页一致）：identity / disks / boot / cd / credential / status
  const [activeTab, setActiveTab] = useState('identity')
  // 系统盘 Tab：建盘弹窗开关 + 展开中的盘（os_tag 唯一键）
  const [diskModalOpen, setDiskModalOpen] = useState(false)
  const [expandedDisk, setExpandedDisk] = useState(null)

  // menu.ipxe 导航项（与后端 MENU_NAV_ITEMS 一致）：MAIN MENU 动态化后 OS 项已收敛为
  // 唯一通用项 boot-os（由默认盘配置推导，不在此可选），这里仅保留非 OS 导航值
  const MENU_OPTIONS = ['menu-diag', 'menu-install', 'config', 'shell', 'reboot', 'exit'].map((v) => ({ value: v, label: v }))
  const CLEAR_OPTION = { value: '__clear__', label: t('workerDetail.clear') }

  const DISK_TYPE_OPTIONS = [
    { value: 'empty', label: t('workers.plan.empty') },
    { value: 'master', label: t('workers.plan.master') },
  ]

  // 盘显示标签：os[ os_version]（版本为备注性质，'' 不显示）
  const diskLabel = (d) => (d.os_version ? `${d.os} ${d.os_version}` : d.os)

  // 母盘清单按当前所选存储节点过滤（value 直接存母盘名，agent 已由 disk_agent 单独选择）；
  // 有登记标签的母盘 label 显示「name (os version)」，选中自动带出 os/os_version（备注性质）
  const masterOptions = (mastersData?.agents || [])
    .filter((entry) => !diskForm.disk_agent || entry.agent === diskForm.disk_agent)
    .flatMap((entry) =>
      (entry.masters || []).map((m) => {
        const tag = m.os ? ` (${m.os}${m.os_version ? ' ' + m.os_version : ''})` : ''
        // 512 扇区布局母盘不可克隆（nvmet 按底层 4K 块导出，EDK2 找不到 ESP）：禁用并标注
        const badGpt = m.gpt === '512'
        return {
          value: m.name,
          label: badGpt ? `${m.name}${tag} — ${t('workers.masterGpt512')}` : `${m.name}${tag}`,
          disabled: badGpt,
          os: m.os || '',
          os_version: m.os_version || '',
        }
      })
    )

  // 母盘文件大小：按名称索引（卡片收起态显示克隆盘大小）
  const masterSizeBy = (() => {
    const map = {}
    for (const entry of mastersData?.agents || []) {
      for (const m of entry.masters || []) {
        if (m.size != null) map[m.name] = m.size
      }
    }
    return map
  })()

  const buildBootVarsCode = (bv, worker) => {
    if (bv && Object.keys(bv).length > 0) {
      const lines = ['#!ipxe', `# boot vars for ${(worker && worker.hostname) || id}`]
      if (bv.base_nqn) lines.push(`set base-nqn ${bv.base_nqn}`)
      if (bv.base_iqn) lines.push(`set base-iqn ${bv.base_iqn}`)
      if (bv.storager_ip) lines.push(`set storager-ip ${bv.storager_ip}`)
      if (bv.iscsi_sep) lines.push(`set iscsi-sep ${bv.iscsi_sep}`)
      if (bv.nbft_secret) lines.push(`set nbft-secret ${bv.nbft_secret}`)
      if (bv.hostnqn) lines.push(`set hostnqn ${bv.hostnqn}`)
      if (bv.os) lines.push(`set os ${bv.os}`)
      if (bv.os_version) lines.push(`set os-version ${bv.os_version}`)
      if (bv.os_tag) lines.push(`set os-tag ${bv.os_tag}`)
      if (bv.menu_default) lines.push(`set menu-default ${bv.menu_default}`)
      if (bv.menu_timeout !== undefined) lines.push(`set menu-timeout ${bv.menu_timeout}`)
      return lines.join('\n')
    }
    return '#!ipxe\n# no per-worker boot vars found'
  }

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const [w, s, bv, cred] = await Promise.all([
          getWorker(id),
          getWorkerStatus(id).catch(() => null),
          bootVars({ hostname: id, format: 'json' }).catch(() => null),
          getWorkerCredential(id).catch(() => null),
        ])
        if (cancelled) return
        setWorker(w)
        setStatus(s)
        setCred(cred)
        setBootForm({
          disk: w.default_disk || '',
          menu_default: w.boot?.menu_default || w.boot?.['menu-default'] || '',
          menu_timeout: w.boot?.menu_timeout ?? w.boot?.['menu-timeout'] ?? '',
          clear_timeout: false,
        })
        setBootVarsCode(buildBootVarsCode(bv, w))
      } catch (e) {
        if (!cancelled) setError(e.message)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    load()
    return () => { cancelled = true }
  }, [id])

  useEffect(() => {
    let cancelled = false
    async function loadDiskAgents() {
      try {
        const [agents, masters] = await Promise.all([
          getAgents(false),
          getMasters().catch(() => null), // 全部 Agent 失败时聚合端点 502，母盘下拉置空即可
        ])
        if (cancelled) return
        const diskAgents = (Array.isArray(agents) ? agents : []).filter(
          (a) => a.enabled && a.role?.disk
        )
        setAgentsList(diskAgents)
        setMastersData(masters)
        if (diskAgents.length > 0) {
          setDiskForm((prev) => ({ ...prev, disk_agent: prev.disk_agent || diskAgents[0].id }))
        }
      } catch {
        setAgentsList([])
        setMastersData(null)
      }
    }
    loadDiskAgents()
    return () => { cancelled = true }
  }, [id])

  const refreshStatus = async () => {
    setStatusLoading(true)
    try {
      const s = await getWorkerStatus(id)
      setStatus(s)
    } catch {
      // ignore
    } finally {
      setStatusLoading(false)
    }
  }

  const handleDelete = async (extra) => {
    try {
      await deleteWorker(id, extra.delete_disk, extra.ignore_missing)
      navigate('/workers')
    } catch (e) {
      alert(e.message)
    }
  }

  // 删除单个系统盘后刷新台账与默认启动表单（default_disk / menu_default 可能被联动清除）
  const reloadWorkerAndBoot = async () => {
    const w = await getWorker(id)
    const bv = await bootVars({ hostname: id, format: 'json' }).catch(() => null)
    setWorker(w)
    setBootForm({
      disk: w.default_disk || '',
      menu_default: w.boot?.menu_default || w.boot?.['menu-default'] || '',
      menu_timeout: w.boot?.menu_timeout ?? w.boot?.['menu-timeout'] ?? '',
      clear_timeout: false,
    })
    setBootVarsCode(buildBootVarsCode(bv, w))
  }

  const handleDeleteDisk = async (osTag, extra) => {
    setDeletingDisk(osTag)
    try {
      await deleteWorkerDisk(id, osTag, extra.delete_file, extra.ignore_missing)
      await reloadWorkerAndBoot()
    } catch (e) {
      alert(e.message)
    } finally {
      setDeletingDisk(null)
    }
  }

  const handleCreateDisk = async (e) => {
    e.preventDefault()
    setCreatingDisk(true)
    setDiskCreateError(null)
    const body = { type: diskForm.type, os: diskForm.os, os_version: diskForm.os_version.trim() }
    if (diskForm.type === 'master') {
      body.name = diskForm.name.trim()
    } else {
      body.size = diskForm.size.trim()
    }
    if (diskForm.disk_agent) {
      body.disk_agent = diskForm.disk_agent
    }
    try {
      await createWorkerDisk(id, body)
      const w = await getWorker(id)
      setWorker(w)
      setBootForm((prev) => ({ ...prev, disk: w.default_disk || prev.disk }))
      setDiskModalOpen(false) // 创建成功关闭弹窗，表单不占用页面空间
    } catch (err) {
      setDiskCreateError(err.message)
    } finally {
      setCreatingDisk(false)
    }
  }

  const handleSaveBoot = async (e) => {
    e.preventDefault()
    setSavingBoot(true)
    setBootSaveError(null)
    const body = {}
    if (bootForm.disk === '__clear__') body.disk = null
    else if (bootForm.disk) body.disk = bootForm.disk
    if (bootForm.menu_default === '__clear__') body.menu_default = null
    else if (bootForm.menu_default) body.menu_default = bootForm.menu_default
    if (bootForm.clear_timeout) body.menu_timeout = null
    else if (bootForm.menu_timeout !== '' && bootForm.menu_timeout !== undefined) {
      body.menu_timeout = Number(bootForm.menu_timeout)
    }
    if (Object.keys(body).length === 0) {
      setBootSaveError(t('workerDetail.bootNothing'))
      setSavingBoot(false)
      return
    }
    try {
      await setWorkerDefaultBoot(id, body)
      const w = await getWorker(id)
      const bv = await bootVars({ hostname: id, format: 'json' }).catch(() => null)
      setWorker(w)
      setBootForm({
        disk: w.default_disk || '',
        menu_default: w.boot?.menu_default || w.boot?.['menu-default'] || '',
        menu_timeout: w.boot?.menu_timeout ?? w.boot?.['menu-timeout'] ?? '',
        clear_timeout: false,
      })
      setBootVarsCode(buildBootVarsCode(bv, w))
    } catch (err) {
      setBootSaveError(err.message)
    } finally {
      setSavingBoot(false)
    }
  }

  const handleSaveMac = async () => {
    setMacSaving(true)
    setMacError(null)
    try {
      const w = await updateWorkerMac(id, macValue.trim())
      setWorker(w)
      setMacEdit(false)
      setMacValue('')
    } catch (err) {
      setMacError(err.message)
    } finally {
      setMacSaving(false)
    }
  }

  // 凭据设置：密钥在浏览器就地生成（明文只随本次请求发出，不落浏览器存储）
  const handleSetCredential = async () => {
    setCredBusy(true)
    setCredError(null)
    try {
      await setWorkerCredential(id, generateDHHC1())
      setCred(await getWorkerCredential(id).catch(() => null))
    } catch (err) {
      setCredError(err.message)
    } finally {
      setCredBusy(false)
    }
  }

  const handleRevokeCredential = async () => {
    setCredBusy(true)
    setCredError(null)
    try {
      await revokeWorkerCredential(id)
      setCred(await getWorkerCredential(id).catch(() => null))
    } catch (err) {
      setCredError(err.message)
    } finally {
      setCredBusy(false)
    }
  }

  // 推送状态文案（ok 单目标不展开明细；failed 列原因）
  const PUSH_LABEL = {
    ok: t('workerDetail.pushOk'),
    failed: t('workerDetail.pushFailed'),
    pending: t('workerDetail.pushPending'),
    'n/a': t('workerDetail.pushNa'),
  }
  const renderCredentialPush = () => {
    const push = cred?.push
    if (!push || push.result === 'none') return null
    const label = PUSH_LABEL[push.result] || push.result
    const agents = push.agents || []
    const details = agents.filter((a) => a.result !== 'ok')
    return (
      <div className="cred-push">
        <span className="info-label">{t('workerDetail.credentialPush')}</span>
        <span className="cred-push-body">
          <Badge variant={push.result === 'ok' ? 'ok' : push.result === 'failed' ? 'failed' : 'degraded'}>
            {label}
          </Badge>
          {(details.length > 0 ? details : agents.length > 1 ? agents : []).map((a) => (
            <span key={a.agent_id} className="cred-agent">
              <span className="info-mono">{a.agent_id}</span>
              <span>{PUSH_LABEL[a.result] || a.result}</span>
              {a.error && <span className="cred-agent-error">{a.error}</span>}
            </span>
          ))}
        </span>
      </div>
    )
  }

  if (loading) return <p className="page-loading">{t('common.loading')}</p>
  if (error) return <p className="page-error">{error}</p>
  if (!worker) return <EmptyState message={t('workerDetail.notFoundMsg')} />

  const { disks = [], cd, mac } = worker
  // 凭据按盘生效：盘标识权威 = NQN，缺 NQN 的存量盘不支持 NVMe-oF 凭据（与后端推送目标口径一致），
  // 故无 NVMe-oF 盘时整个页签不出现（不支持即隐藏）
  const hasNvmeof = disks.some((d) => d.nqn)

  return (
    <div>
      <div className="detail-nav">
        <Link to="/workers" className="back-link">
          {t('workerDetail.back')}
        </Link>
      </div>

      <div className="detail-header">
        <div className="detail-title-row">
          <h2 className="page-title" style={{ marginBottom: 0 }}>
            {worker.worker_id}
          </h2>
          <Badge>{worker.state || 'unknown'}</Badge>
        </div>
        <ConfirmAction
          trigger={<Button variant="danger">{t('workerDetail.delete')}</Button>}
          message={t('workerDetail.deleteConfirm', { id })}
          onConfirm={handleDelete}
          extraFields={[
            { name: 'delete_disk', label: t('workerDetail.deleteDisk') },
            { name: 'ignore_missing', label: t('workerDetail.ignoreMissing') },
          ]}
        />
      </div>

      {/* 分类 Tab：基本信息 / 系统盘 / 启动配置 / 光驱 / NVMe-oF 凭据（有 NVMe-oF 盘才出现）/ 实时状态 */}
      <div className="agent-tabs">
        <button
          className={`agent-tab${activeTab === 'identity' ? ' active' : ''}`}
          onClick={() => setActiveTab('identity')}
        >
          {t('workerDetail.tabIdentity')}
        </button>
        <button
          className={`agent-tab${activeTab === 'disks' ? ' active' : ''}`}
          onClick={() => setActiveTab('disks')}
        >
          {t('workerDetail.tabDisks')}
        </button>
        <button
          className={`agent-tab${activeTab === 'boot' ? ' active' : ''}`}
          onClick={() => setActiveTab('boot')}
        >
          {t('workerDetail.tabBoot')}
        </button>
        {cd && (
          <button
            className={`agent-tab${activeTab === 'cd' ? ' active' : ''}`}
            onClick={() => setActiveTab('cd')}
          >
            {t('workerDetail.tabCd')}
          </button>
        )}
        {hasNvmeof && (
          <button
            className={`agent-tab${activeTab === 'credential' ? ' active' : ''}`}
            onClick={() => setActiveTab('credential')}
          >
            {t('workerDetail.tabCredential')}
          </button>
        )}
        <button
          className={`agent-tab${activeTab === 'status' ? ' active' : ''}`}
          onClick={() => setActiveTab('status')}
        >
          {t('workerDetail.tabStatus')}
        </button>
      </div>

      {activeTab === 'identity' && (
        <>
      {/* Identity */}
      <Divider>{t('workerDetail.identity')}</Divider>
      <Card className="detail-card">
        <InfoRow label={t('workerDetail.workerId')} value={worker.worker_id} mono />
        <InfoRow label={t('workerDetail.hostname')} value={worker.hostname} mono />
        <div className="info-row">
          <span className="info-label">{t('workerDetail.mac')}</span>
          {macEdit ? (
            <span className="mac-edit-fields">
              <Input
                name="mac_edit"
                value={macValue}
                onChange={(e) => setMacValue(e.target.value)}
                placeholder={t('workers.macPlaceholder')}
              />
              <Button onClick={handleSaveMac} disabled={macSaving}>
                {macSaving ? t('workerDetail.macSaving') : t('workerDetail.macSave')}
              </Button>
              <Button variant="ghost" onClick={() => { setMacEdit(false); setMacError(null) }} disabled={macSaving}>
                {t('workers.cancel')}
              </Button>
            </span>
          ) : (
            <span className="mac-display">
              <span className="info-value info-mono">{mac || '—'}</span>
              <Button
                variant="ghost"
                className="mac-edit-btn"
                onClick={() => { setMacValue(mac || ''); setMacEdit(true) }}
              >
                {t('workerDetail.macEdit')}
              </Button>
            </span>
          )}
        </div>
        {macEdit && <p className="mac-edit-hint">{t('workerDetail.macHint')}</p>}
        {macError && <p className="mac-edit-error">{macError}</p>}
        <InfoRow
          label={t('workerDetail.os')}
          value={disks.map(diskLabel).join(', ') || t('workerDetail.noDisk')}
        />
        <InfoRow label={t('workerDetail.arch')} value={worker.arch} />
        <InfoRow label={t('workerDetail.state')} value={worker.state} />
      </Card>
        </>
      )}

      {activeTab === 'credential' && hasNvmeof && (
        // NVMe-oF 认证凭据（DHHC-1，按 worker 跟盘）：保存态 + 逐 Agent 推送状态 + 生成/轮换/吊销
        <Card className="detail-card">
          {cred === null ? (
            <p className="page-muted">{t('workerDetail.credentialUnavailable')}</p>
          ) : (
            <>
              <div className="info-row">
                <span className="info-label">{t('workerDetail.credentialState')}</span>
                <span className="cred-state">
                  <Badge variant={cred.exists ? 'ok' : 'default'}>
                    {cred.exists ? t('workerDetail.credentialSetState') : t('workerDetail.credentialNone')}
                  </Badge>
                  {cred.exists && <span className="info-value info-mono">{cred.secret_hash}</span>}
                </span>
              </div>
              {cred.exists && (
                <InfoRow label={t('workerDetail.credentialUpdated')} value={cred.updated_at} mono />
              )}
              {renderCredentialPush()}
              <p className="cred-hint">{t('workerDetail.credentialHint')}</p>
              <div className="cred-actions">
                <ConfirmAction
                  trigger={
                    <Button disabled={credBusy}>
                      {credBusy
                        ? t('workerDetail.credentialBusy')
                        : cred.exists
                          ? t('workerDetail.credentialRotate')
                          : t('workerDetail.credentialSet')}
                    </Button>
                  }
                  message={t('workerDetail.credentialSetConfirm', { id })}
                  onConfirm={handleSetCredential}
                />
                {cred.exists && (
                  <ConfirmAction
                    trigger={
                      <Button variant="danger" disabled={credBusy}>
                        {t('workerDetail.credentialRevoke')}
                      </Button>
                    }
                    message={t('workerDetail.credentialRevokeConfirm', { id })}
                    onConfirm={handleRevokeCredential}
                  />
                )}
              </div>
              {credError && <p className="create-error">{credError}</p>}
            </>
          )}
        </Card>
      )}

      {activeTab === 'disks' && (
        <>
          {/* 工具栏：盘数量 + 创建按钮（建盘表单收进全局弹窗，不占页面空间） */}
          <div className="disks-toolbar">
            <span className="disks-meta">
              {t('workerDetail.diskCount', { count: disks.length })}
            </span>
            <Button variant="secondary" onClick={() => setDiskModalOpen(true)}>
              {t('workerDetail.createDisk')}
            </Button>
          </div>

          {/* 系统盘卡片：默认收起核心属性（系统/来源/大小），点击展开全部参数 */}
          {disks.length > 0 && (
            <div className="disk-cards">
              {disks.map((d, i) => {
                const expanded = expandedDisk === d.os_tag
                const masterSize =
                  d.source?.type === 'master' ? masterSizeBy[d.source.name] : null
                return (
                  <div
                    key={d.iqn || `disk-${i}`}
                    className={`disk-card${expanded ? ' expanded' : ''}`}
                  >
                    <div
                      className="disk-card-head"
                      onClick={() => setExpandedDisk(expanded ? null : d.os_tag)}
                    >
                      <span className="dc-os">{diskLabel(d)}</span>
                      <span className="dc-source">
                        {d.source?.type === 'master'
                          ? t('workerDetail.diskSourceMaster', { name: d.source.name })
                          : t('workerDetail.diskSourceEmpty', { size: d.source?.size || '' })}
                        {masterSize ? ` · ${formatSize(masterSize)}` : ''}
                      </span>
                      <span className="dc-arrow">{expanded ? '▾' : '▸'}</span>
                    </div>
                    {expanded && (
                      <div className="disk-card-body">
                        <InfoRow label={t('workerDetail.osTag')} value={d.os_tag} mono />
                        {d.remark && (
                          <InfoRow label={t('workerDetail.remark')} value={d.remark} />
                        )}
                        <InfoRow label={t('workerDetail.agent')} value={d.agent} mono />
                        {d.nqn && <InfoRow label={t('workerDetail.nqn')} value={d.nqn} mono />}
                        <InfoRow label={t('workerDetail.iqn')} value={d.iqn} mono />
                        <InfoRow label={t('workerDetail.filename')} value={d.filename} mono />
                        <InfoRow label={t('workerDetail.backing')} value={d.backing} mono />
                        {d.source && (
                          <InfoRow
                            label={t('workerDetail.source')}
                            value={
                              d.source.type === 'master'
                                ? `master: ${d.source.name}`
                                : `empty: ${d.source.size}`
                            }
                          />
                        )}
                        <div className="disk-card-actions">
                          <ConfirmAction
                            trigger={
                              <Button variant="danger" disabled={deletingDisk !== null}>
                                {deletingDisk === d.os_tag ? t('workerDetail.deletingDisk') : t('workerDetail.deleteSystemDisk')}
                              </Button>
                            }
                            message={t('workerDetail.deleteDiskConfirm', { id, os: diskLabel(d) })}
                            onConfirm={(extra) => handleDeleteDisk(d.os_tag, extra)}
                            extraFields={[
                              { name: 'delete_file', label: t('workerDetail.deleteDisk') },
                              { name: 'ignore_missing', label: t('workerDetail.ignoreMissing') },
                            ]}
                          />
                        </div>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}

          {/* 建盘全局弹窗：表单不占用页面空间 */}
          {diskModalOpen && (
            <Modal
              title={t('workerDetail.createDisk')}
              width="640px"
              onClose={() => setDiskModalOpen(false)}
              footer={
                <>
                  <Button type="submit" form="worker-disk-create" disabled={creatingDisk}>
                    {creatingDisk ? t('workers.creating') : t('workers.createBtn')}
                  </Button>
                  <Button
                    variant="ghost"
                    onClick={() => setDiskModalOpen(false)}
                    disabled={creatingDisk}
                  >
                    {t('workers.cancel')}
                  </Button>
                </>
              }
            >
              <form id="worker-disk-create" onSubmit={handleCreateDisk}>
                <p className="create-hint">{t('workerDetail.createDiskHint')}</p>
                <div className="create-form-grid">
                  <Input
                    label={t('workers.os')}
                    name="os"
                    value={diskForm.os}
                    onChange={(e) => { setDiskForm((prev) => ({ ...prev, os: e.target.value })) }}
                    placeholder={t('workers.plan.osPlaceholder')}
                    required
                  />
                  <Input
                    label={t('workers.plan.osVersion')}
                    name="os_version"
                    value={diskForm.os_version}
                    onChange={(e) => { setDiskForm((prev) => ({ ...prev, os_version: e.target.value })) }}
                    placeholder={t('workers.plan.osVersionPlaceholder')}
                  />
                  <Select
                    label={t('workers.plan.type')}
                    name="type"
                    value={diskForm.type}
                    onChange={(e) => { setDiskForm((prev) => ({ ...prev, type: e.target.value })) }}
                    options={DISK_TYPE_OPTIONS}
                  />
                  {diskForm.type === 'master' ? (
                    <Select
                      label={t('workers.plan.masterName')}
                      name="disk_name"
                      value={diskForm.name}
                      onChange={(e) => {
                        // 选中已登记标签的母盘自动带出 os/os_version（标签为备注，可再改）
                        const picked = masterOptions.find((m) => m.value === e.target.value)
                        setDiskForm((prev) => ({
                          ...prev,
                          name: e.target.value,
                          os: picked?.os || prev.os,
                          os_version: picked?.os_version || '',
                        }))
                      }}
                      options={masterOptions}
                      placeholder={masterOptions.length === 0 ? t('workers.plan.noMasters') : t('workers.plan.masterPlaceholder')}
                      required
                    />
                  ) : (
                    <Input
                      label={t('workers.plan.size')}
                      name="disk_size"
                      value={diskForm.size}
                      onChange={(e) => { setDiskForm((prev) => ({ ...prev, size: e.target.value })) }}
                      placeholder={t('workers.plan.sizePlaceholder')}
                      required
                    />
                  )}
                  {agentsList.length > 0 && (
                    <Select
                      label={t('workers.diskAgent')}
                      name="disk_agent"
                      value={diskForm.disk_agent}
                      onChange={(e) => {
                        // 切换节点后母盘清单随之过滤，已选母盘不再有效则清空
                        setDiskForm((prev) => ({ ...prev, disk_agent: e.target.value, name: '' }))
                      }}
                      options={agentsList.map((a) => ({
                        value: a.id,
                        label: `${a.id}${a.storager_ip ? ` (${a.storager_ip})` : ''}`,
                      }))}
                    />
                  )}
                </div>
                {diskCreateError && <p className="create-error">{diskCreateError}</p>}
              </form>
            </Modal>
          )}
        </>
      )}

      {/* Default Boot */}
      {activeTab === 'boot' && (
        <>
      <Divider>{t('workerDetail.defaultBoot')}</Divider>
      <Card className="detail-card">
        <InfoRow label={t('workerDetail.defaultDisk')} value={worker.default_disk || '—'} mono />
        <InfoRow
          label={t('workerDetail.menuDefault')}
          value={worker.boot?.menu_default || worker.boot?.['menu-default'] || '—'}
          mono
        />
        <InfoRow
          label={t('workerDetail.menuTimeout')}
          value={worker.boot?.menu_timeout ?? worker.boot?.['menu-timeout'] ?? '—'}
          mono
        />
      </Card>
      <form className="create-form" onSubmit={handleSaveBoot}>
        <p className="create-hint">{t('workerDetail.defaultBootHint')}</p>
        <div className="create-form-grid">
          <Select
            label={t('workerDetail.defaultDisk')}
            name="boot_disk"
            value={bootForm.disk}
            onChange={(e) => { setBootForm((prev) => ({ ...prev, disk: e.target.value })) }}
            options={[CLEAR_OPTION, ...disks.map((d) => ({ value: d.os_tag, label: `${diskLabel(d)} (${d.os_tag})` }))]}
            disabled={disks.length === 0}
          />
          <Select
            label={t('workerDetail.menuDefault')}
            name="boot_menu_default"
            value={bootForm.menu_default}
            onChange={(e) => { setBootForm((prev) => ({ ...prev, menu_default: e.target.value })) }}
            options={[CLEAR_OPTION, ...MENU_OPTIONS]}
          />
          <Input
            label={t('workerDetail.menuTimeout')}
            name="boot_menu_timeout"
            type="number"
            min="0"
            value={bootForm.menu_timeout}
            onChange={(e) => { setBootForm((prev) => ({ ...prev, menu_timeout: e.target.value })) }}
            placeholder={t('workerDetail.menuTimeoutPlaceholder')}
          />
          <label className="boot-clear">
            <input
              type="checkbox"
              checked={bootForm.clear_timeout}
              onChange={(e) => { setBootForm((prev) => ({ ...prev, clear_timeout: e.target.checked })) }}
            />
            {t('workerDetail.clearTimeout')}
          </label>
        </div>
        {bootSaveError && <p className="create-error">{bootSaveError}</p>}
        <Button type="submit" disabled={savingBoot}>
          {savingBoot ? t('workerDetail.savingBoot') : t('workerDetail.saveBoot')}
        </Button>
      </form>

      {/* Boot Vars */}
      <Divider>{t('workerDetail.bootVars')}</Divider>
      <CodeBlock code={bootVarsCode} language="ipxe" />
        </>
      )}

      {/* CD */}
      {activeTab === 'cd' && cd && (
        <>
          <Divider>{t('workerDetail.cdrom')}</Divider>
          <Card className="detail-card">
            <InfoRow label={t('workerDetail.agent')} value={cd.agent} mono />
            <InfoRow label={t('workerDetail.iqn')} value={cd.iqn} mono />
            <InfoRow label={t('workerDetail.iso')} value={cd.iso} mono />
            <InfoRow label={t('workerDetail.backing')} value={cd.backing} mono />
          </Card>
        </>
      )}

      {/* Live Status */}
      {activeTab === 'status' && (
        <>
      <Divider>{t('workerDetail.liveStatus')}</Divider>
      {status ? (
        <Card className="detail-card">
          {status.actual?.dnsmasq && (
            <InfoRow
              label={t('workerDetail.dnsmasq')}
              value={`${status.actual.dnsmasq.hostname} \u2192 ${status.actual.dnsmasq.mac}`}
              mono
            />
          )}
          {(status.actual?.disks || []).map((d, i) => (
            <InfoRow
              key={d.os || `disk-${i}`}
              label={`${t('workerDetail.diskTarget')}${d.os ? ` (${d.os})` : ''}`}
              value={d?.exists ? t('workerDetail.exists') : t('workerDetail.notFound')}
            />
          ))}
          {status.actual?.cd !== null && (
            <InfoRow
              label={t('workerDetail.cdTarget')}
              value={status.actual?.cd?.exists ? t('workerDetail.exists') : t('workerDetail.notFound')}
            />
          )}
        </Card>
      ) : (
        <p className="page-muted">{t('workerDetail.statusUnavailable')}</p>
      )}
      <div style={{ marginTop: 12 }}>
        <Button variant="ghost" onClick={refreshStatus} disabled={statusLoading}>
          {statusLoading ? t('workerDetail.refreshing') : t('workerDetail.refresh')}
        </Button>
      </div>
        </>
      )}
    </div>
  )
}
