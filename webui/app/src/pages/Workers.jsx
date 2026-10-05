import { useState, useEffect, useCallback } from 'react'
import { Link } from 'react-router-dom'
import {
  getWorkers,
  getAgents,
  getMasters,
  createWorker,
  batchCreateWorkers,
  batchCreateWorkerDisks,
  batchDeleteWorkers,
} from '../api/client'
import { useI18n } from '../i18n'
import Button from '../components/Button'
import Input from '../components/Input'
import Badge from '../components/Badge'
import ConfirmAction from '../components/ConfirmAction'
import EmptyState from '../components/EmptyState'
import Modal from '../components/Modal'
import Toast, { ToastStack } from '../components/Toast'
import './Workers.css'

const splitLines = (text) =>
  text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)

export default function Workers() {
  const { t } = useI18n()

  const [workers, setWorkers] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [filter, setFilter] = useState('')

  // 勾选常驻：没有「模式」，选中即浮出操作条
  const [selected, setSelected] = useState([])
  const [anchor, setAnchor] = useState(null)

  // 存储节点（磁盘角色且启用）与母盘清单：两个独立失败面，空态要能说清是哪种
  const [storageAgents, setStorageAgents] = useState([])
  const [nodesError, setNodesError] = useState(null)
  const [mastersData, setMastersData] = useState(null)
  const [mastersError, setMastersError] = useState(null)

  // 建盘：参数 + 高级约束 + 逐行人工覆盖；位置由控制面算，这里只声明意图
  const [planOpen, setPlanOpen] = useState(false)
  const [diskType, setDiskType] = useState('master')
  const [diskOs, setDiskOs] = useState('ubuntu')
  const [diskOsVersion, setDiskOsVersion] = useState('')
  const [diskMaster, setDiskMaster] = useState('')
  const [diskSize, setDiskSize] = useState('40G')
  const [remark, setRemark] = useState('')
  const [advOpen, setAdvOpen] = useState(false)
  const [blocked, setBlocked] = useState([]) // 明确排除的节点（默认全允许）
  const [pins, setPins] = useState({})
  const [plan, setPlan] = useState(null)
  const [planning, setPlanning] = useState(false)
  const [planError, setPlanError] = useState(null)
  const [planExcluded, setPlanExcluded] = useState([])
  const [submitting, setSubmitting] = useState(false)
  const [diskError, setDiskError] = useState(null)
  const [diskResult, setDiskResult] = useState(null)

  // 创建 Worker 对话框
  const [createOpen, setCreateOpen] = useState(false)
  const [createForm, setCreateForm] = useState({ count: '1', name: '', macs: '', windows_iso: '' })
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState(null)
  const [createResult, setCreateResult] = useState(null)

  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState(null)
  const [deleteResult, setDeleteResult] = useState(null)

  const [guideOpen, setGuideOpen] = useState(false)

  const fetchWorkers = useCallback(async () => {
    try {
      const data = await getWorkers()
      setWorkers(Array.isArray(data) ? data : [])
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchWorkers()
  }, [fetchWorkers])

  // 节点列表与母盘清单独立取数：/masters 是跨节点聚合端点，任一节点不可达即整批失败，
  // 若与节点列表耦合成 Promise.all，会让「建空白盘」（不需要母盘）也拿不到节点
  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const agents = await getAgents(false)
        if (!alive) return
        setStorageAgents(
          (Array.isArray(agents) ? agents : []).filter((a) => a.enabled && a.role?.disk)
        )
      } catch (e) {
        if (!alive) return
        setStorageAgents([])
        setNodesError(e.message)
      }
      try {
        const masters = await getMasters()
        if (alive) setMastersData(masters)
      } catch (e) {
        if (alive) setMastersError(e.message)
      }
    })()
    return () => {
      alive = false
    }
  }, [])

  const masterOptions = (mastersData?.agents || [])
    .flatMap((entry) => (entry.masters || []).map((m) => m.name))
    .filter((name, idx, arr) => arr.indexOf(name) === idx)
    .map((name) => ({ value: name, label: name }))
  const masterOptionsKey = masterOptions.map((o) => o.value).join(',')

  // 母盘清单未取到时不知道节点有没有该母盘，此时不做本地标注（由控制面判定）
  const masterKnown = !mastersError && mastersData !== null
  const nodeHasMaster = (agentId, name) => {
    const entry = (mastersData?.agents || []).find((e) => e.agent === agentId)
    return !!(entry?.masters || []).some((m) => m.name === name)
  }

  // 首次拿到母盘清单时补默认选中（用户手选过就不覆盖）
  useEffect(() => {
    if (diskType !== 'master') return
    if (diskMaster && masterOptions.some((o) => o.value === diskMaster)) return
    const first = masterOptions[0]?.value || ''
    if (diskMaster !== first) setDiskMaster(first)
  }, [diskType, diskMaster, masterOptionsKey]) // eslint-disable-line react-hooks/exhaustive-deps

  const selectionActive = selected.length > 0
  const planMode = selectionActive && planOpen
  const selectedKey = selected.join(',')
  const blockedKey = blocked.join(',')
  const pinsKey = JSON.stringify(pins)

  // 计划档位不齐时不请求：dry_run 与执行共用同一份放置实现，缺参数只会换来 400
  const canPlan =
    selectionActive &&
    diskOs.trim() !== '' &&
    (diskType === 'empty' ? diskSize.trim() !== '' : diskMaster !== '')

  // 不能算计划时要说清缺什么，否则摘要位停在「计算中…」像是卡住
  const blockedReason = !diskOs.trim()
    ? t('workers.plan.needOs')
    : diskType === 'master' && diskMaster === ''
      ? mastersError
        ? t('workers.plan.mastersUnavailable')
        : t('workers.plan.noMasters')
      : diskType === 'empty' && diskSize.trim() === ''
        ? t('workers.plan.needSize')
        : null

  const planBody = (dryRun) => {
    const body = {
      type: diskType,
      os: diskOs.trim(),
      os_version: diskOsVersion.trim(),
      remark: remark.trim(),
      workers: selected,
      dry_run: dryRun,
    }
    if (diskType === 'master') body.name = diskMaster
    else body.size = diskSize.trim()

    const placement = {}
    if (blocked.length > 0) {
      placement.candidates = storageAgents
        .filter((a) => !blocked.includes(a.id))
        .map((a) => a.id)
    }
    const scoped = Object.fromEntries(
      Object.entries(pins).filter(([id]) => selected.includes(id))
    )
    if (Object.keys(scoped).length > 0) placement.pins = scoped
    if (placement.candidates || placement.pins) body.placement = placement
    return body
  }

  // 选中 / 约束 / 参数任一变化就重算计划（250ms 防抖），预览即执行
  useEffect(() => {
    if (!selectionActive) {
      setPlanOpen(false)
      setPlan(null)
      setPlanError(null)
      setPlanExcluded([])
      return
    }
    if (!planOpen || !canPlan) {
      setPlan(null)
      setPlanError(null)
      setPlanExcluded([])
      setPlanning(false)
      return
    }
    let alive = true
    setPlanning(true)
    const timer = setTimeout(async () => {
      try {
        const res = await batchCreateWorkerDisks(planBody(true))
        if (!alive) return
        setPlan(res)
        setPlanError(null)
        setPlanExcluded([])
      } catch (e) {
        if (!alive) return
        setPlan(null)
        setPlanError(e.message)
        setPlanExcluded(e.payload?.detail?.excluded || [])
      } finally {
        if (alive) setPlanning(false)
      }
    }, 250)
    return () => {
      alive = false
      clearTimeout(timer)
    }
  }, [ // eslint-disable-line react-hooks/exhaustive-deps
    selectionActive,
    planOpen,
    canPlan,
    selectedKey,
    blockedKey,
    pinsKey,
    diskType,
    diskOs,
    diskOsVersion,
    diskMaster,
    diskSize,
  ])

  const exitPlan = () => {
    setSelected([])
    setAnchor(null)
    setPins({})
    setPlanOpen(false)
    setPlan(null)
    setPlanError(null)
    setPlanExcluded([])
    setDiskError(null)
    setDiskResult(null)
  }

  const toggleOne = (workerId, shiftKey, visible) => {
    if (shiftKey && anchor) {
      const from = visible.findIndex((w) => w.worker_id === anchor)
      const to = visible.findIndex((w) => w.worker_id === workerId)
      if (from >= 0 && to >= 0) {
        const [lo, hi] = from <= to ? [from, to] : [to, from]
        const ids = visible.slice(lo, hi + 1).map((w) => w.worker_id)
        setSelected((prev) => Array.from(new Set([...prev, ...ids])))
        setAnchor(workerId)
        return
      }
    }
    setSelected((prev) =>
      prev.includes(workerId) ? prev.filter((id) => id !== workerId) : [...prev, workerId]
    )
    setAnchor(workerId)
  }

  // 全部退出用 Esc：先关贴层，再退计划
  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== 'Escape') return
      if (guideOpen) {
        setGuideOpen(false)
        return
      }
      if (createOpen) {
        setCreateOpen(false)
        return
      }
      if (selectionActive) exitPlan()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [guideOpen, createOpen, selectionActive]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleConfirmPlan = async () => {
    setSubmitting(true)
    setDiskError(null)
    setDiskResult(null)
    try {
      const res = await batchCreateWorkerDisks(planBody(false))
      setDiskResult(res)
      setSelected([])
      setAnchor(null)
      setPins({})
      setPlanOpen(false)
      setPlan(null)
      setPlanExcluded([])
      await fetchWorkers()
    } catch (e) {
      setDiskError(e.message)
    } finally {
      setSubmitting(false)
    }
  }

  const handleBatchDelete = async (extra = {}) => {
    if (selected.length === 0) return
    setDeleting(true)
    setDeleteError(null)
    setDeleteResult(null)
    try {
      const res = await batchDeleteWorkers(selected, extra.delete_disk, extra.ignore_missing)
      setDeleteResult(res)
      setSelected([])
      setAnchor(null)
      await fetchWorkers()
    } catch (e) {
      setDeleteError(e.message)
    } finally {
      setDeleting(false)
    }
  }

  const handleCreate = async (e) => {
    e.preventDefault()
    const count = parseInt(createForm.count, 10)
    if (!Number.isInteger(count) || count < 1 || count > 100) {
      setCreateError(t('workers.countInvalid'))
      return
    }
    const macs = splitLines(createForm.macs)
    if (macs.length > 0 && macs.length !== count) {
      setCreateError(t('workers.macsCountMismatch'))
      return
    }
    setCreating(true)
    setCreateError(null)
    setCreateResult(null)
    try {
      if (count === 1) {
        const body = { worker_id: createForm.name.trim() }
        if (macs.length === 1) body.mac = macs[0]
        if (createForm.windows_iso.trim()) body.windows_iso = createForm.windows_iso.trim()
        const created = await createWorker(body)
        closeCreate()
        setCreateResult({ succeeded: [{ worker_id: created?.worker_id || body.worker_id }] })
      } else {
        const body = { count, name_prefix: createForm.name.trim() }
        if (macs.length > 0) body.macs = macs
        const res = await batchCreateWorkers(body)
        setCreateResult(res)
        setCreateOpen(false)
        // 有失败就留着表单值：改掉个别 MAC 直接重提，已建成的按幂等跳过
        if ((res.failed || []).length === 0) {
          setCreateForm({ count: '1', name: '', macs: '', windows_iso: '' })
        }
      }
      await fetchWorkers()
    } catch (err) {
      setCreateError(err.message)
    } finally {
      setCreating(false)
    }
  }

  const closeCreate = () => {
    setCreateOpen(false)
    setCreateError(null)
    setCreateResult(null)
    setCreateForm({ count: '1', name: '', macs: '', windows_iso: '' })
  }

  const filtered = workers.filter((w) => {
    const term = filter.toLowerCase()
    return (
      (w.worker_id || '').toLowerCase().includes(term) ||
      (w.hostname || '').toLowerCase().includes(term) ||
      (w.bound_device || '').toLowerCase().includes(term) ||
      (w.disks || []).some((d) => (d.os || '').toLowerCase().includes(term))
    )
  })

  if (loading) return <p className="page-loading">{t('common.loading')}</p>
  if (error) return <p className="page-error">{error}</p>

  // 表头全选 / 清空：作用域 = 当前筛选后的可见行（不是全量），与「所见即所选」一致
  const allChecked =
    filtered.length > 0 && filtered.every((w) => selected.includes(w.worker_id))
  const someChecked = !allChecked && filtered.some((w) => selected.includes(w.worker_id))

  const handleToggleAll = () => {
    const ids = filtered.map((w) => w.worker_id)
    setSelected((prev) =>
      allChecked ? prev.filter((id) => !ids.includes(id)) : Array.from(new Set([...prev, ...ids]))
    )
    setAnchor(null)
  }

  const planMap = {}
  ;(plan?.plan || []).forEach((row) => {
    planMap[row.worker_id] = row
  })
  const planned = (plan?.plan || []).filter((row) => row.action === 'create')
  const skippedRows = (plan?.plan || []).filter((row) => row.action === 'skip')
  const tally = planned.reduce((acc, row) => {
    acc[row.agent] = (acc[row.agent] || 0) + 1
    return acc
  }, {})
  const shortAgent = (id) => id.replace(/^storage-/, '')
  const tallyShort =
    Object.entries(tally)
      .map(([agent, n]) => `${shortAgent(agent)} ×${n}`)
      .join(' / ') || '—'
  // 成功计划给逐节点告警（被排除的原因）；放置整体失败时后端在 503 里给同一份逐节点原因
  const excluded = plan?.warnings?.length ? plan.warnings : planExcluded

  // pin 必须自身可用：不在允许范围或缺母盘的节点不给选，避免提交后才发现
  const pinUsable = (agentId) =>
    !blocked.includes(agentId) &&
    (diskType !== 'master' || !masterKnown || nodeHasMaster(agentId, diskMaster))

  const diskCell = (w) => {
    if (!planMode) {
      const disks = w.disks || []
      if (disks.length === 0) return <span className="wr-dim">—</span>
      return (
        <span className="wr-dim">
          {disks
            .map((d) => `${d.os || '?'}${d.os_version ? ` ${d.os_version}` : ''} → ${d.agent}`)
            .join(' / ')}
        </span>
      )
    }
    const row = planMap[w.worker_id]
    if (!row) return <span className="wr-dim">—</span>
    if (row.action === 'skip') {
      return <span className="plan-skip">{t('workers.plan.skipHasDisk')}</span>
    }
    if (row.action === 'error') {
      return <span className="plan-err">{row.reason}</span>
    }
    if (storageAgents.length === 0) {
      return <span className="plan-skip">{t('workers.plan.noNodes')}</span>
    }
    const current = pins[w.worker_id] || row.agent
    return (
      <span className="plan-cell">
        <span className="plan-arrow">→</span>
        <select
          className="plan-node"
          value={current}
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => setPins((prev) => ({ ...prev, [w.worker_id]: e.target.value }))}
        >
          {storageAgents.map((a) => (
            <option key={a.id} value={a.id} disabled={!pinUsable(a.id)}>
              {a.id}
            </option>
          ))}
        </select>
        {pins[w.worker_id] && (
          <>
            <span className="plan-manual">{t('workers.plan.manual')}</span>
            <button
              type="button"
              className="plan-reset"
              title={t('workers.plan.resetAuto')}
              onClick={(e) => {
                e.preventDefault()
                e.stopPropagation()
                setPins((prev) => {
                  const next = { ...prev }
                  delete next[w.worker_id]
                  return next
                })
              }}
            >
              ×
            </button>
          </>
        )}
      </span>
    )
  }

  // 结果 → 通知：成功/跳过自动消失，带失败则常驻（逐条列失败与跳过，不再复述成功项）
  const resultToast = (result) => {
    if (!result) return null
    const ok = result.succeeded || []
    const skipped = result.skipped || []
    const failed = result.failed || []
    const parts = [t('workers.okCount', { n: ok.length })]
    if (skipped.length > 0) parts.push(t('workers.skipCount', { n: skipped.length }))
    if (failed.length > 0) parts.push(t('workers.failCount', { n: failed.length }))
    const items = [
      ...failed.map((f) => ({ label: f.worker_id, detail: f.error || '' })),
      ...skipped.map((s) => ({ label: s.worker_id, detail: s.reason || '' })),
    ]
    return { tone: failed.length > 0 ? 'error' : skipped.length > 0 ? 'warn' : 'ok', message: parts.join(' · '), items }
  }

  const createCount = parseInt(createForm.count, 10)
  const isBatch = Number.isInteger(createCount) && createCount > 1

  return (
    <div>
      <div className="page-header">
        <h2 className="page-title">{t('workers.title')}</h2>
        <Button onClick={() => setCreateOpen(true)}>{t('workers.create')}</Button>
      </div>

      <div className="workers-toolbar">
        <input
          className="workers-filter"
          placeholder={t('workers.filter')}
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <span className="workers-count">{t('workers.countAll', { count: filtered.length })}</span>
        <Button variant="ghost" onClick={() => setGuideOpen(true)}>
          {t('workers.guide.btn')}
        </Button>
      </div>

      {selectionActive && !planMode && (
        <div className="actionbar">
          <span className="ab-label">{t('workers.sel.label', { n: selected.length })}</span>
          <span className="ab-sep" />
          <Button className="btn-invert" onClick={() => setPlanOpen(true)}>
            {t('workers.sel.createDisks')}
          </Button>
          <ConfirmAction
            trigger={
              <Button variant="danger" className="btn-invert" disabled={deleting}>
                {t('workers.sel.delete')}
              </Button>
            }
            message={t('workers.bulkDelete.confirm', { count: selected.length })}
            details={<span>{t('workers.bulkDelete.hint')}</span>}
            onConfirm={(extra) => handleBatchDelete(extra)}
            extraFields={[
              { name: 'delete_disk', label: t('workerDetail.deleteDisk') },
              { name: 'ignore_missing', label: t('workerDetail.ignoreMissing') },
            ]}
          />
          <span className="ab-spacer" />
          <button type="button" className="ab-clear" onClick={exitPlan}>
            {t('workers.sel.clear')}
          </button>
        </div>
      )}

      {planMode && (
        <div className="actionbar">
          <span className="ab-label">{t('workers.plan.label')}</span>
          <span className="ab-sep" />
          <span className="ab-params">
            <label>
              {t('workers.plan.type')}
              <select
                className="ab-select"
                value={diskType}
                onChange={(e) => setDiskType(e.target.value)}
              >
                <option value="master">{t('workers.plan.master')}</option>
                <option value="empty">{t('workers.plan.empty')}</option>
              </select>
            </label>
            <label>
              {t('workers.plan.osLabel')}
              <input
                className="ab-input ab-input-os"
                value={diskOs}
                onChange={(e) => setDiskOs(e.target.value)}
                placeholder="ubuntu"
              />
            </label>
            {diskType === 'master' ? (
              <label>
                {t('workers.plan.masterName')}
                <select
                  className="ab-select"
                  value={diskMaster}
                  onChange={(e) => setDiskMaster(e.target.value)}
                >
                  {masterOptions.length === 0 && (
                    <option value="">
                      {mastersError ? t('workers.plan.mastersUnavailable') : t('workers.plan.noMasters')}
                    </option>
                  )}
                  {masterOptions.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </label>
            ) : (
              <label>
                {t('workers.plan.size')}
                <input
                  className="ab-input"
                  value={diskSize}
                  onChange={(e) => setDiskSize(e.target.value)}
                  placeholder={t('workers.plan.sizePlaceholder')}
                />
              </label>
            )}
            <button type="button" className="ab-toggle" onClick={() => setAdvOpen(!advOpen)}>
              {advOpen ? '▾' : '▸'} {t('workers.plan.advanced')}
            </button>
          </span>
          <span className="ab-spacer" />
          <span className="ab-plan">
            {blockedReason ? (
              <span className="ab-warn">{blockedReason}</span>
            ) : planning ? (
              t('workers.plan.computing')
            ) : planError ? (
              <span className="ab-warn">{t('workers.plan.planFailed', { error: planError })}</span>
            ) : (
              <>
                {planned.length > 0 &&
                  t('workers.plan.summary', { n: planned.length, tally: tallyShort })}
                {skippedRows.length > 0 && (
                  <span className="ab-warn">
                    {planned.length > 0 ? ' ' : ''}
                    {t('workers.plan.skipNote', { n: skippedRows.length })}
                  </span>
                )}
              </>
            )}
          </span>
          <Button
            className="btn-invert"
            disabled={submitting || planned.length === 0}
            onClick={handleConfirmPlan}
          >
            {submitting ? t('workers.creating') : t('workers.plan.confirm')}
          </Button>
          <button type="button" className="ab-clear" onClick={exitPlan}>
            {t('workers.cancel')}
          </button>
        </div>
      )}

      {planMode && advOpen && (
        <div className="adv-panel">
          <span className="adv-group">
            <span className="adv-label">{t('workers.plan.limitNodes')}</span>
            {nodesError ? (
              <span className="batch-error">
                {t('workers.plan.nodesError', { error: nodesError })}
              </span>
            ) : storageAgents.length === 0 ? (
              <span className="adv-label">{t('workers.plan.noNodes')}</span>
            ) : (
              storageAgents.map((a) => (
                <label className="checkline" key={a.id} title={a.storager_ip || a.base_url || ''}>
                  <input
                    type="checkbox"
                    checked={!blocked.includes(a.id)}
                    onChange={() =>
                      setBlocked((prev) =>
                        prev.includes(a.id) ? prev.filter((id) => id !== a.id) : [...prev, a.id]
                      )
                    }
                  />
                  {a.id}
                  {diskType === 'master' && masterKnown && !nodeHasMaster(a.id, diskMaster) && (
                    <span className="adv-note">{t('workers.plan.nodeLacksMaster')}</span>
                  )}
                </label>
              ))
            )}
          </span>
          <span className="adv-group">
            <span className="adv-label">{t('workers.plan.osVersion')}</span>
            <input
              className="adv-input"
              value={diskOsVersion}
              onChange={(e) => setDiskOsVersion(e.target.value)}
              placeholder={t('workers.plan.osVersionPlaceholder')}
            />
          </span>
          <span className="adv-group">
            <span className="adv-label">{t('workers.plan.remark')}</span>
            <input
              className="adv-input"
              value={remark}
              onChange={(e) => setRemark(e.target.value)}
              placeholder={t('workers.plan.remarkPlaceholder')}
            />
          </span>
          {excluded.length > 0 && (
            <span className="adv-group">
              <span className="adv-label">{t('workers.plan.excluded')}</span>
              <span className="adv-note">
                {excluded.map((w) => `${w.agent}（${w.reason}）`).join('、')}
              </span>
            </span>
          )}
        </div>
      )}

      {filtered.length === 0 ? (
        <EmptyState message={filter ? t('workers.noMatch') : t('workers.noWorkers')} />
      ) : (
        <div className="workers-list">
          <div className="workers-header">
            <span className="wh-check">
              <input
                type="checkbox"
                ref={(el) => {
                  if (el) el.indeterminate = someChecked
                }}
                checked={allChecked}
                onChange={handleToggleAll}
                title={t('workers.sel.selectAll')}
                aria-label={t('workers.sel.selectAll')}
              />
            </span>
            <span>{t('workers.id')}</span>
            <span>{t('workers.hostname')}</span>
            <span>{t('workers.bound')}</span>
            <span>{t('workers.diskCol')}</span>
            <span>{t('workers.state')}</span>
          </div>
          {filtered.map((w) => {
            const checked = selected.includes(w.worker_id)
            const cells = (
              <>
                <span
                  className="wr-check"
                  title={t('workers.sel.selectAll')}
                  onClick={(e) => {
                    e.preventDefault()
                    e.stopPropagation()
                    toggleOne(w.worker_id, e.shiftKey, filtered)
                  }}
                >
                  <input type="checkbox" checked={checked} readOnly tabIndex={-1} />
                </span>
                <span className="wr-id">{w.worker_id}</span>
                <span className="wr-dim">{w.hostname}</span>
                <span className="wr-dim">{w.bound_device || '—'}</span>
                <span>{diskCell(w)}</span>
                <span>
                  <Badge>{w.readiness || 'idle'}</Badge>
                </span>
              </>
            )
            // 有选中时整行 = 勾选（选到一半误点行跳详情会丢上下文）；无选中时整行进详情
            return selectionActive ? (
              <div
                key={w.worker_id}
                className={`workers-row wr-selectable ${checked ? 'wr-checked' : ''}`}
                onClick={(e) => toggleOne(w.worker_id, e.shiftKey, filtered)}
              >
                {cells}
              </div>
            ) : (
              <Link key={w.worker_id} to={`/workers/${w.worker_id}`} className="workers-row">
                {cells}
              </Link>
            )
          })}
        </div>
      )}

      <p className="behavior-hint">
        {planMode
          ? t('workers.sel.hintPlan')
          : selectionActive
            ? t('workers.sel.hintActive')
            : t('workers.sel.hintEmpty')}
      </p>

      {createOpen && (
        <Modal
          title={t('workers.newWorker')}
          width="560px"
          onClose={closeCreate}
          footer={
            <>
              <Button type="submit" form="workers-create" disabled={creating}>
                {creating ? t('workers.creating') : t('workers.createBtn')}
              </Button>
              <Button variant="ghost" onClick={closeCreate} disabled={creating}>
                {t('workers.cancel')}
              </Button>
            </>
          }
        >
          <form id="workers-create" onSubmit={handleCreate}>
            <p className="create-hint">{t('workers.registerHint')}</p>
            <div className="create-form-grid">
              <Input
                label={t('workers.count')}
                name="count"
                value={createForm.count}
                onChange={(e) => setCreateForm((p) => ({ ...p, count: e.target.value }))}
                placeholder={t('workers.countPlaceholder')}
                required
              />
              <Input
                label={isBatch ? t('workers.namePrefix') : t('workers.workerId')}
                name="name"
                value={createForm.name}
                onChange={(e) => setCreateForm((p) => ({ ...p, name: e.target.value }))}
                placeholder={
                  isBatch ? t('workers.namePrefixPlaceholder') : t('workers.workerIdPlaceholder')
                }
                required
              />
            </div>
            <div className="create-form-grid">
              <div className="create-form-wide">
                <label className="field-label" htmlFor="workers-create-macs">
                  {t('workers.macs')}
                </label>
                <textarea
                  id="workers-create-macs"
                  className="create-macs"
                  rows={3}
                  value={createForm.macs}
                  onChange={(e) => setCreateForm((p) => ({ ...p, macs: e.target.value }))}
                  placeholder={t('workers.macsPlaceholder')}
                />
                <p className="create-hint">{t('workers.macsHint')}</p>
              </div>
            </div>
            {!isBatch && (
              <details className="adv-details">
                <summary>{t('workers.advanced')}</summary>
                <div className="adv-details-body">
                  <Input
                    label={t('workers.windowsIso')}
                    name="windows_iso"
                    value={createForm.windows_iso}
                    onChange={(e) => setCreateForm((p) => ({ ...p, windows_iso: e.target.value }))}
                    placeholder={t('workers.windowsIsoPlaceholder')}
                  />
                </div>
              </details>
            )}
            {createError && <p className="create-error">{createError}</p>}
          </form>
        </Modal>
      )}

      {guideOpen && (
        <div className="guide-overlay" onClick={() => setGuideOpen(false)}>
          <div className="guide-panel" onClick={(e) => e.stopPropagation()}>
            <div className="guide-panel-title">{t('workers.guide.title')}</div>
            {[
              ['headerTitle', 'headerBody'],
              ['filterTitle', 'filterBody'],
              ['columnsTitle', 'columnsBody'],
              ['rowTitle', 'rowBody'],
            ].map(([titleKey, bodyKey]) => (
              <div className="guide-section" key={titleKey}>
                <div className="guide-section-title">{t(`workers.guide.${titleKey}`)}</div>
                <p className="guide-section-body">{t(`workers.guide.${bodyKey}`)}</p>
              </div>
            ))}
            <div className="guide-actions">
              <Button onClick={() => setGuideOpen(false)}>{t('workers.guide.close')}</Button>
            </div>
          </div>
        </div>
      )}

      <ToastStack>
        {resultToast(diskResult) && (
          <Toast {...resultToast(diskResult)} onClose={() => setDiskResult(null)} />
        )}
        {resultToast(deleteResult) && (
          <Toast {...resultToast(deleteResult)} onClose={() => setDeleteResult(null)} />
        )}
        {resultToast(createResult) && (
          <Toast {...resultToast(createResult)} onClose={() => setCreateResult(null)} />
        )}
        {diskError && (
          <Toast tone="error" message={diskError} onClose={() => setDiskError(null)} />
        )}
        {deleteError && (
          <Toast tone="error" message={deleteError} onClose={() => setDeleteError(null)} />
        )}
      </ToastStack>
    </div>
  )
}
