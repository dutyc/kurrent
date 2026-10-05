import { useState, useEffect, useCallback, useMemo } from 'react'
import { useParams, Link } from 'react-router-dom'
import {
  getAgentLuns,
  getAgents,
  getWorkers,
  getMasters,
  createAgentDiskLun,
  createAgentCdLun,
  deleteAgentLun,
  scanAgentLuns,
  setMasterTag,
  clearMasterTag,
} from '../api/client'
import { useI18n } from '../i18n'
import Button from '../components/Button'
import Badge from '../components/Badge'
import Input from '../components/Input'
import Select from '../components/Select'
import Divider from '../components/Divider'
import Card from '../components/Card'
import Modal from '../components/Modal'
import ConfirmAction from '../components/ConfirmAction'
import EmptyState from '../components/EmptyState'
import './AgentLuns.css'

function flattenTargets(targets) {
  const rows = []
  for (const target of targets || []) {
    const luns =
      target.luns && target.luns.length ? target.luns : [{ lun: 1, backing: null }]
    for (const lun of luns) {
      rows.push({ iqn: target.iqn, backing: lun.backing || '', lun: lun.lun })
    }
  }
  return rows
}

function isCd(backing) {
  return /\.iso$/i.test(backing)
}

// 母盘文件格式类型（按扩展名；blank 无法从元数据可靠判断，raw = 裸镜像）
function masterType(name) {
  if (/\.qcow2$/i.test(name)) return 'qcow2'
  if (/\.iso$/i.test(name)) return 'iso'
  return 'raw'
}

// 字节数 → 人类可读容量
function formatSize(bytes) {
  if (bytes == null) return '—'
  const gb = bytes / 1024 ** 3
  if (gb >= 1) return `${gb.toFixed(1)} GB`
  const mb = bytes / 1024 ** 2
  if (mb >= 1) return `${mb.toFixed(0)} MB`
  return `${bytes} B`
}

export default function AgentLuns() {
  const { t } = useI18n()
  const { id } = useParams()
  const [agent, setAgent] = useState(null)
  const [luns, setLuns] = useState([])
  const [workers, setWorkers] = useState([])
  const [masters, setMasters] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [showDiskForm, setShowDiskForm] = useState(false)
  const [showCdForm, setShowCdForm] = useState(false)
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState(null)
  const [scanning, setScanning] = useState(false)
  const [diskForm, setDiskForm] = useState({ iqn: '', kind: 'empty', master: '', size: '40G' })
  const [cdForm, setCdForm] = useState({ iso: '', iqn: '' })
  // 母盘标签登记（控制面台账，备注性质）：编辑中母盘名 + 表单 + 保存中状态
  const [masterTagEdit, setMasterTagEdit] = useState(null)
  const [masterTagForm, setMasterTagForm] = useState({ os: '', os_version: '', remark: '' })
  const [savingTag, setSavingTag] = useState(false)
  // 分类页：targets（LUN 列表）/ masters（母盘编辑）
  const [activeTab, setActiveTab] = useState('targets')

  const refresh = useCallback(async () => {
    const data = await getAgentLuns(id)
    setLuns(Array.isArray(data) ? data : [])
  }, [id])

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const [agents, lunsData, workersData, mastersData] = await Promise.all([
          getAgents(false).catch(() => []),
          getAgentLuns(id),
          getWorkers().catch(() => []),
          getMasters().catch(() => null), // 聚合端点失败时母盘标签区块置空即可
        ])
        if (cancelled) return
        setAgent((Array.isArray(agents) ? agents : []).find((a) => a.id === id) || null)
        setLuns(Array.isArray(lunsData) ? lunsData : [])
        setWorkers(Array.isArray(workersData) ? workersData : [])
        setMasters(
          (mastersData?.agents || []).find((entry) => entry.agent === id)?.masters || []
        )
      } catch (e) {
        if (!cancelled) setError(e.message)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [id])

  // iqn -> 绑定的 Worker（系统盘 disk / 光驱 cd）
  // 兼容两种标识：worker 盘上报 iqn（iqn. 前缀）与 nqn（nqn. 前缀），
  // 而 agent target 列表以 nqn 呈现，必须同时建索引才能匹配上
  const bindingByIqn = useMemo(() => {
    const map = {}
    for (const w of workers) {
      for (const d of w.disks || (w.disk ? [w.disk] : [])) {
        if (d.iqn) map[d.iqn] = { workerId: w.worker_id, kind: 'disk' }
        if (d.nqn && d.nqn !== d.iqn) map[d.nqn] = { workerId: w.worker_id, kind: 'disk' }
      }
      if (w.cd?.iqn) map[w.cd.iqn] = { workerId: w.worker_id, kind: 'cd' }
      if (w.cd?.nqn && w.cd.nqn !== w.cd.iqn) {
        map[w.cd.nqn] = { workerId: w.worker_id, kind: 'cd' }
      }
    }
    return map
  }, [workers])

  // iqn -> 盘的系统标签（os/os_version，从 worker 盘上报数据匹配）
  const diskInfoByIqn = useMemo(() => {
    const map = {}
    for (const w of workers) {
      for (const d of w.disks || (w.disk ? [w.disk] : [])) {
        const info = { os: d.os, os_version: d.os_version, os_tag: d.os_tag, remark: d.remark }
        if (d.iqn) map[d.iqn] = info
        if (d.nqn && d.nqn !== d.iqn) map[d.nqn] = info
      }
    }
    return map
  }, [workers])

  const rows = flattenTargets(luns)
  const boundCount = rows.filter((r) => bindingByIqn[r.iqn]).length

  // 母盘 NQN：正常为空（scan 跳过母盘）；历史存量或误建过 target 时显示其 NQN，便于发现与清理
  const nqnByMaster = useMemo(() => {
    const map = {}
    for (const target of luns || []) {
      for (const lun of target.luns || []) {
        if (!lun.backing) continue
        const base = lun.backing.split('/').pop()
        if (base) map[base] = target.nqn || target.iqn
      }
    }
    return map
  }, [luns])

  // 绑定统计：worker 盘 source.type=master 且 source.name=母盘名
  const boundByMaster = useMemo(() => {
    const map = {}
    for (const w of workers) {
      for (const d of w.disks || (w.disk ? [w.disk] : [])) {
        const src = d.source
        if (src?.type === 'master' && src.name) {
          map[src.name] = [...(map[src.name] || []), w.worker_id]
        }
      }
    }
    return map
  }, [workers])

  const update = (setter) => (key) => (e) =>
    setter((prev) => ({ ...prev, [key]: e.target.value }))

  const handleCreateDisk = async (e) => {
    e.preventDefault()
    setCreating(true)
    setCreateError(null)
    const body = { iqn: diskForm.iqn.trim() }
    if (diskForm.kind === 'master') {
      body.master = diskForm.master.trim()
    } else {
      body.size = diskForm.size.trim()
    }
    try {
      await createAgentDiskLun(id, body)
      setShowDiskForm(false)
      setDiskForm({ iqn: '', kind: 'empty', master: '', size: '40G' })
      await refresh()
    } catch (err) {
      setCreateError(err.message)
    } finally {
      setCreating(false)
    }
  }

  const handleCreateCd = async (e) => {
    e.preventDefault()
    setCreating(true)
    setCreateError(null)
    const body = { iso: cdForm.iso.trim() }
    if (cdForm.iqn.trim()) body.iqn = cdForm.iqn.trim()
    try {
      await createAgentCdLun(id, body)
      setShowCdForm(false)
      setCdForm({ iso: '', iqn: '' })
      await refresh()
    } catch (err) {
      setCreateError(err.message)
    } finally {
      setCreating(false)
    }
  }

  const refreshMasters = useCallback(async () => {
    const data = await getMasters().catch(() => null)
    setMasters((data?.agents || []).find((entry) => entry.agent === id)?.masters || [])
  }, [id])

  // 登记母盘标签：保存后刷新台账（备注性质，不校验母盘存在性）
  const handleSaveMasterTag = async (name) => {
    setSavingTag(true)
    try {
      await setMasterTag(
        id,
        name,
        masterTagForm.os.trim(),
        masterTagForm.os_version.trim(),
        masterTagForm.remark.trim()
      )
      setMasterTagEdit(null)
      setMasterTagForm({ os: '', os_version: '', remark: '' })
      await refreshMasters()
    } catch (err) {
      alert(err.message)
    } finally {
      setSavingTag(false)
    }
  }

  const handleClearMasterTag = async (name) => {
    setSavingTag(true)
    try {
      await clearMasterTag(id, name)
      setMasterTagEdit(null)
      setMasterTagForm({ os: '', os_version: '', remark: '' })
      await refreshMasters()
    } catch (err) {
      alert(err.message)
    } finally {
      setSavingTag(false)
    }
  }

  const handleScan = async () => {
    setScanning(true)
    try {
      const result = await scanAgentLuns(id)
      alert(
        t('agentLuns.scanResult', {
          created: result.created?.length || 0,
          skipped: result.skipped?.length || 0,
        })
      )
      await refresh()
    } catch (err) {
      alert(err.message)
    } finally {
      setScanning(false)
    }
  }

  const handleDelete = (iqn) => async (extra) => {
    try {
      await deleteAgentLun(id, iqn, extra.delete_file)
      await refresh()
    } catch (err) {
      alert(err.message)
    }
  }

  // 删除母盘：摘除 target + 删除镜像文件（克隆产物独立，不受影响）
  const handleDeleteMaster = async (nqn) => {
    try {
      await deleteAgentLun(id, nqn, true)
      await Promise.all([refresh(), refreshMasters()])
    } catch (err) {
      alert(err.message)
    }
  }

  if (loading) return <p className="page-loading">{t('common.loading')}</p>
  if (error) return <p className="page-error">{error}</p>
  if (!agent) return <EmptyState message={t('agentLuns.notFound')} />

  // 角色能力：role.cd 为 false 时隐藏创建光驱入口（不支持的后端不显示）；
  // 无 role 配置时兼容旧数据默认允许
  const role = agent.role || {}
  const cdSupported = role.cd !== false
  const diskSupported = role.disk !== false
  const caps = agent.capabilities || {}

  return (
    <div>
      <div className="detail-nav">
        <Link to="/agents" className="back-link">
          {t('agentLuns.back')}
        </Link>
      </div>

      {/* 顶部状态栏：Agent 身份 + 健康 + 能力摘要 */}
      <div className="detail-header">
        <div className="detail-title-row">
          <h2 className="page-title" style={{ marginBottom: 0 }}>
            {agent.id}
          </h2>
          <Badge>{agent.health}</Badge>
        </div>
        <div className="agent-status-meta">
          <span>{t('agentLuns.statusIp', { ip: agent.storager_ip || '—' })}</span>
          <span>
            {t('agentLuns.statusRole', {
              disk: role.disk ? '✓' : '✗',
            })}
            {role.cd && ` · ${t('agentLuns.statusRoleCd')}`}
          </span>
          {caps.backend && <span>{t('agentLuns.statusBackend', { name: caps.backend })}</span>}
          {caps.port?.trsvcid && (
            <span>{t('agentLuns.statusPort', { port: caps.port.trsvcid })}</span>
          )}
          {caps.base_nqn && (
            <span>{t('agentLuns.statusBaseNqn', { nqn: caps.base_nqn })}</span>
          )}
        </div>
      </div>

      {/* 分类页：Target / 母盘编辑 */}
      <div className="agent-tabs">
        <button
          className={`agent-tab${activeTab === 'targets' ? ' active' : ''}`}
          onClick={() => setActiveTab('targets')}
        >
          {t('agentLuns.tabTargets')}
        </button>
        <button
          className={`agent-tab${activeTab === 'masters' ? ' active' : ''}`}
          onClick={() => setActiveTab('masters')}
        >
          {t('agentLuns.tabMasters')}
        </button>
      </div>

      {activeTab === 'masters' && (
        <>
          {/* Master Tags（控制面登记台账，备注性质：os / os_version 供建盘自动带出） */}
          <Divider>{t('agentLuns.masterTags')}</Divider>
          <p className="master-hint">{t('agentLuns.masterTagsHint')}</p>
          <Card className="detail-card">
            {masters.length === 0 ? (
              <p className="page-muted">{t('agentLuns.noMasters')}</p>
            ) : (
              <div className="master-table-wrap">
                <div className="master-table">
                  <div className="master-table-header">
                    <span className="mth-name">{t('agentLuns.masterColName')}</span>
                    <span className="mth-size">{t('agentLuns.masterColSize')}</span>
                    <span className="mth-type">{t('agentLuns.type')}</span>
                    <span className="mth-nqn">NQN</span>
                    <span className="mth-bound">{t('agentLuns.bound')}</span>
                    <span className="mth-remark">{t('agentLuns.masterColRemark')}</span>
                    <span className="mth-action" />
                  </div>
                  {masters.map((m) => {
                    const editing = masterTagEdit === m.name
                    const hasTag = Boolean(m.os)
                    const bound = boundByMaster[m.name] || []
                    const nqn = nqnByMaster[m.name] || '—'
                    return (
                      <div key={m.name} className="master-table-row">
                        <span className="mtr-name">
                          <span className="mtr-name-text">{m.name}</span>
                          {hasTag && (
                            <Badge>{[m.os, m.os_version].filter(Boolean).join(' ')}</Badge>
                          )}
                          {m.gpt === '512' && (
                            <Badge variant="degraded">{t('agentLuns.masterGpt512')}</Badge>
                          )}
                        </span>
                        <span className="mtr-size">{formatSize(m.size)}</span>
                        <span className="mtr-type">
                          <Badge variant="ready">{masterType(m.name)}</Badge>
                        </span>
                        <span className="mtr-nqn">{nqn}</span>
                        <span className="mtr-bound">
                          {bound.length > 0 ? (
                            <Badge variant="installing">
                              {t('agentLuns.boundWorkers', { count: bound.length })}
                            </Badge>
                          ) : (
                            <span className="lr-unbound">{t('agentLuns.unbound')}</span>
                          )}
                        </span>
                        <span className="mtr-remark" title={m.remark || ''}>
                          {m.remark || '—'}
                        </span>
                        <span className="mtr-action">
                          <Button
                            variant="ghost"
                            onClick={() => {
                              setMasterTagEdit(m.name)
                              setMasterTagForm({
                                os: m.os || '',
                                os_version: m.os_version || '',
                                remark: m.remark || '',
                              })
                            }}
                          >
                            {hasTag ? t('agentLuns.editTag') : t('agentLuns.tag')}
                          </Button>
                          <ConfirmAction
                            trigger={<Button variant="danger">{t('agentLuns.delete')}</Button>}
                            message={t('agentLuns.deleteMasterConfirm', { name: m.name })}
                            details={
                              <>
                                <div>{t('agentLuns.detailNqn')}: {nqn}</div>
                                {hasTag && (
                                  <div>
                                    {t('agentLuns.detailOs')}: {m.os}
                                    {m.os_version ? ' ' + m.os_version : ''}
                                  </div>
                                )}
                                {m.remark && (
                                  <div>{t('agentLuns.detailRemark')}: {m.remark}</div>
                                )}
                              </>
                            }
                            onConfirm={() => handleDeleteMaster(nqn)}
                          />
                        </span>
                      </div>
                    )
                  })}
                </div>
              </div>
            )}
          </Card>
          {masterTagEdit && (() => {
            const editMaster = masters.find((m) => m.name === masterTagEdit)
            return (
              <Modal
                title={t('agentLuns.editTagTitle', { name: masterTagEdit })}
                onClose={() => setMasterTagEdit(null)}
                footer={
                  <>
                    <Button
                      onClick={() => handleSaveMasterTag(masterTagEdit)}
                      disabled={savingTag}
                    >
                      {savingTag ? t('agentLuns.tagSaving') : t('agentLuns.saveTag')}
                    </Button>
                    <Button
                      variant="ghost"
                      onClick={() => setMasterTagEdit(null)}
                      disabled={savingTag}
                    >
                      {t('agentLuns.cancel')}
                    </Button>
                    {editMaster?.os && (
                      <Button
                        variant="danger"
                        onClick={() => handleClearMasterTag(masterTagEdit)}
                        disabled={savingTag}
                      >
                        {t('agentLuns.clearTag')}
                      </Button>
                    )}
                  </>
                }
              >
                <div className="master-tag-form-row">
                  <Input
                    name="tag_os"
                    label={t('workers.os')}
                    value={masterTagForm.os}
                    onChange={(e) =>
                      setMasterTagForm((prev) => ({ ...prev, os: e.target.value }))
                    }
                    placeholder={t('workers.os')}
                    required
                  />
                  <Input
                    name="tag_os_version"
                    label={t('agentLuns.masterTagVersionLabel')}
                    value={masterTagForm.os_version}
                    onChange={(e) =>
                      setMasterTagForm((prev) => ({ ...prev, os_version: e.target.value }))
                    }
                    placeholder={t('agentLuns.masterTagVersionPlaceholder')}
                  />
                </div>
                <textarea
                  className="field-input master-remark-input"
                  rows={3}
                  value={masterTagForm.remark}
                  onChange={(e) =>
                    setMasterTagForm((prev) => ({ ...prev, remark: e.target.value }))
                  }
                  placeholder={t('agentLuns.masterRemarkPlaceholder')}
                />
              </Modal>
            )
          })()}
        </>
      )}

      {activeTab === 'targets' && (
        <>
          <div className="luns-toolbar">
            <span className="luns-meta">
              {t('agentLuns.count', { count: rows.length })}
              {boundCount > 0 && ` · ${t('agentLuns.boundCount', { bound: boundCount })}`}
            </span>
            <Button variant="ghost" onClick={handleScan} disabled={scanning}>
              {scanning ? t('agentLuns.scanning') : t('agentLuns.scan')}
            </Button>
            {cdSupported && (
              <Button
                variant={showCdForm ? 'ghost' : 'secondary'}
                onClick={() => {
                  setShowCdForm(!showCdForm)
                  setShowDiskForm(false)
                  setCreateError(null)
                }}
              >
                {showCdForm ? t('agentLuns.cancel') : t('agentLuns.createCd')}
              </Button>
            )}
            <Button
              variant={showDiskForm ? 'ghost' : 'primary'}
              disabled={!diskSupported}
              title={diskSupported ? undefined : t('agentLuns.diskUnsupported')}
              onClick={() => {
                setShowDiskForm(!showDiskForm)
                setShowCdForm(false)
                setCreateError(null)
              }}
            >
              {showDiskForm ? t('agentLuns.cancel') : t('agentLuns.createDisk')}
            </Button>
            {!diskSupported && (
              <span className="luns-hint">{t('agentLuns.diskUnsupported')}</span>
            )}
          </div>

          {diskSupported && showDiskForm && (
            <form className="create-form" onSubmit={handleCreateDisk}>
              <div className="create-form-title">{t('agentLuns.newDisk')}</div>
              <div className="create-form-grid">
                <Input
                  label={t('agentLuns.iqnLabel')}
                  name="iqn"
                  value={diskForm.iqn}
                  onChange={update(setDiskForm)('iqn')}
                  placeholder={t('agentLuns.iqnPlaceholder')}
                  required
                />
                <Select
                  label={t('agentLuns.type')}
                  name="kind"
                  value={diskForm.kind}
                  onChange={update(setDiskForm)('kind')}
                  options={[
                    { value: 'empty', label: t('agentLuns.empty') },
                    { value: 'master', label: t('agentLuns.master') },
                  ]}
                />
                {diskForm.kind === 'master' ? (
                  <Input
                    label={t('agentLuns.masterName')}
                    name="master"
                    value={diskForm.master}
                    onChange={update(setDiskForm)('master')}
                    placeholder={t('agentLuns.masterPlaceholder')}
                    required
                  />
                ) : (
                  <Input
                    label={t('agentLuns.diskSize')}
                    name="size"
                    value={diskForm.size}
                    onChange={update(setDiskForm)('size')}
                    placeholder={t('agentLuns.diskSizePlaceholder')}
                    required
                  />
                )}
              </div>
              {createError && <p className="create-error">{createError}</p>}
              <Button type="submit" disabled={creating}>
                {creating ? t('agentLuns.creating') : t('agentLuns.createBtn')}
              </Button>
            </form>
          )}

          {cdSupported && showCdForm && (
            <form className="create-form" onSubmit={handleCreateCd}>
              <div className="create-form-title">{t('agentLuns.newCd')}</div>
              <div className="create-form-grid">
                <Input
                  label={t('agentLuns.iso')}
                  name="iso"
                  value={cdForm.iso}
                  onChange={update(setCdForm)('iso')}
                  placeholder={t('agentLuns.isoPlaceholder')}
                  required
                />
                <Input
                  label={t('agentLuns.iqnOptional')}
                  name="cd_iqn"
                  value={cdForm.iqn}
                  onChange={update(setCdForm)('iqn')}
                  placeholder={t('agentLuns.iqnPlaceholder')}
                />
              </div>
              {createError && <p className="create-error">{createError}</p>}
              <Button type="submit" disabled={creating}>
                {creating ? t('agentLuns.creating') : t('agentLuns.createBtn')}
              </Button>
            </form>
          )}

          {rows.length === 0 ? (
            <EmptyState message={t('agentLuns.noLuns')} />
          ) : (
            <div className="luns-list">
              <div className="luns-header">
                <span className="lh-iqn">{t('agentLuns.iqnLabel')}</span>
                <span className="lh-type">{t('agentLuns.type')}</span>
                <span className="lh-os">{t('agentLuns.colOs')}</span>
                <span className="lh-backing">{t('agentLuns.backing')}</span>
                <span className="lh-bound">{t('agentLuns.bound')}</span>
                <span className="lh-remark">{t('agentLuns.colRemark')}</span>
                <span className="lh-action" />
              </div>
              {rows.map((row) => {
                const binding = bindingByIqn[row.iqn]
                const cd = isCd(row.backing)
                const diskInfo = diskInfoByIqn[row.iqn]
                const delMsg = binding
                  ? t('agentLuns.deleteBoundConfirm', { iqn: row.iqn, worker: binding.workerId })
                  : t('agentLuns.deleteConfirm', { iqn: row.iqn })
                return (
                  <div key={`${row.iqn}-${row.lun}`} className="luns-row">
                    <span className="lr-iqn">{row.iqn}</span>
                    <span className="lr-type">
                      <Badge variant={cd ? 'installing' : 'ready'}>
                        {cd ? t('agentLuns.typeCd') : t('agentLuns.typeDisk')}
                      </Badge>
                    </span>
                    <span
                      className="lr-os"
                      title={
                        diskInfo?.os_tag
                          ? `${diskInfo.os} ${diskInfo.os_version || ''} · ${diskInfo.os_tag}`.trim()
                          : undefined
                      }
                    >
                      {diskInfo?.os
                        ? `${diskInfo.os}${diskInfo.os_version ? ' ' + diskInfo.os_version : ''}`
                        : '—'}
                    </span>
                    <span className="lr-backing">{row.backing || '—'}</span>
                    <span className="lr-bound">
                      {binding ? (
                        <Badge variant="installing">
                          {t('agentLuns.boundWorker', { worker: binding.workerId })}
                        </Badge>
                      ) : (
                        <span className="lr-unbound">{t('agentLuns.unbound')}</span>
                      )}
                    </span>
                    <span className="lr-remark" title={diskInfo?.remark || undefined}>
                      {diskInfo?.remark || '—'}
                    </span>
                    <span className="lr-action">
                      <ConfirmAction
                        trigger={<Button variant="danger">{t('agentLuns.delete')}</Button>}
                        message={delMsg}
                        details={
                          <>
                            <div>
                              {t('agentLuns.detailType')}:{' '}
                              {cd ? t('agentLuns.typeCd') : t('agentLuns.typeDisk')}
                            </div>
                            {row.backing && (
                              <div>
                                {t('agentLuns.detailBacking')}: {row.backing}
                              </div>
                            )}
                            {diskInfo?.os && (
                              <div>
                                {t('agentLuns.detailOs')}: {diskInfo.os}
                                {diskInfo.os_version ? ' ' + diskInfo.os_version : ''}
                              </div>
                            )}
                            {binding && (
                              <div>
                                {t('agentLuns.detailBound')}: {binding.workerId}
                              </div>
                            )}
                          </>
                        }
                        onConfirm={handleDelete(row.iqn)}
                        extraFields={[{ name: 'delete_file', label: t('agentLuns.deleteFile') }]}
                      />
                    </span>
                  </div>
                )
              })}
            </div>
          )}
        </>
      )}
    </div>
  )
}
