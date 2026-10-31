import { useState, useEffect, useCallback } from 'react'
import { Link } from 'react-router-dom'
import {
  getDevices,
  createDevice,
  importDevices,
  unbindDevice,
  batchBindPreview,
  batchBind,
  getRegistrationWindow,
  openRegistrationWindow,
  closeRegistrationWindow,
  getEnforcement,
  setEnforcement,
  getWorkers,
  batchCreateWorkers,
  getOperations,
} from '../api/client'
import { useI18n } from '../i18n'
import Button from '../components/Button'
import Input from '../components/Input'
import Select from '../components/Select'
import Badge from '../components/Badge'
import ConfirmAction from '../components/ConfirmAction'
import EmptyState from '../components/EmptyState'
import './Devices.css'

const STATE_OPTIONS = (t) => [
  { value: 'all', label: t('devices.stateAll') },
  { value: 'pooled', label: 'pooled' },
  { value: 'bound', label: 'bound' },
  { value: 'revoked', label: 'revoked' },
]

// 注册窗口 TTL 选项：上限来自声明 spec.registration.windowMaxTtlMinutes（随 GET 返回的 limits）
const TTL_OPTIONS = (t, max) => {
  const values = [5, 15, 30, 60].filter((v) => v <= max)
  if (!values.includes(max)) values.push(max)
  values.sort((a, b) => a - b)
  return values.map((v) => ({ value: String(v), label: t('devices.regWindow.ttlUnit', { n: v }) }))
}

// 缺省上限（limits 未加载时的兜底，与 schema 默认值一致）
const DEFAULT_MAX_DEVICES_UPPER = 1000
const DEFAULT_MAX_TTL_MINUTES = 60

// 解析配对清单：每行 mac, worker_id[, manufacturer, product, serial, uuid]（逗号/制表符分隔，# 注释行忽略）
function parseManifestLines(text) {
  const lines = text
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith('#'))
  const pairs = []
  for (const line of lines) {
    const cols = line.split(/[,\t]/).map((c) => c.trim())
    if (cols.length < 2 || !cols[0] || !cols[1]) {
      throw new Error(`line: ${line}`)
    }
    pairs.push({
      mac: cols[0],
      worker_id: cols[1],
      manufacturer: cols[2] || undefined,
      product: cols[3] || undefined,
      serial: cols[4] || undefined,
      uuid: cols[5] || undefined,
    })
  }
  return pairs
}

// 解析 MAC/WorkerID 清单：每行一个，忽略空行与 # 注释
function parseColumnLines(text) {
  return text
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith('#'))
}

function readFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(new Error('file read failed'))
    reader.readAsText(file)
  })
}

export default function Devices() {
  const { t } = useI18n()
  const [devices, setDevices] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [stateFilter, setStateFilter] = useState('all')
  const [filter, setFilter] = useState('')
  const [expanded, setExpanded] = useState(null)
  const [copiedMac, setCopiedMac] = useState(null) // 复制反馈：当前已复制的 MAC

  // ===== 绑定记录（展开详情时按 mac 拉取审计） =====
  const [bindings, setBindings] = useState(null) // null = 未加载/加载中
  const [bindingsError, setBindingsError] = useState(null)

  // ===== 注册窗口（部署期：仅窗口开启期间新设备上报可携公钥注册入池） + 设备身份验签强制开关 =====
  const [regWindow, setRegWindow] = useState(null) // null = 未加载；{ open, opened_at, ttl_minutes, closes_at, remaining_seconds, max_devices, admitted, remaining_devices, quota_exhausted }
  const [windowBusy, setWindowBusy] = useState(false)
  const [windowError, setWindowError] = useState(null)
  const [ttlChoice, setTtlChoice] = useState('30')
  const [maxChoice, setMaxChoice] = useState('') // 放行额度（台），'' = 不限
  const [countdown, setCountdown] = useState(0) // 本地逐秒递减的剩余秒数（以服务端 remaining_seconds 为基准）
  const [enforcement, setEnforcementState] = useState(null)
  const [enforcementBusy, setEnforcementBusy] = useState(false)
  const windowLimits = regWindow?.limits || {}
  const maxDevicesUpper = windowLimits.max_devices ?? DEFAULT_MAX_DEVICES_UPPER
  const maxTtlMinutes = windowLimits.ttl_minutes_max ?? DEFAULT_MAX_TTL_MINUTES

  // ===== 页面介绍弹层 =====
  const [guideOpen, setGuideOpen] = useState(false)

  // ===== 注册 / 导入 =====
  const [showRegister, setShowRegister] = useState(false)
  const [registering, setRegistering] = useState(false)
  const [registerError, setRegisterError] = useState(null)
  const [regForm, setRegForm] = useState({ mac: '', uuid: '', manufacturer: '', product: '', serial: '' })
  const [showImport, setShowImport] = useState(false)
  const [importing, setImporting] = useState(false)
  const [importText, setImportText] = useState('')
  const [importError, setImportError] = useState(null)
  const [importResult, setImportResult] = useState(null)

  // ===== 批量解绑 =====
  const [unbindMode, setUnbindMode] = useState(false)
  const [selected, setSelected] = useState([])
  const [anchor, setAnchor] = useState(null)
  const [unbinding, setUnbinding] = useState(false)
  const [unbindError, setUnbindError] = useState(null)
  const [unbindResult, setUnbindResult] = useState(null)

  // ===== 绑定向导 =====
  const [wizardOpen, setWizardOpen] = useState(false)
  const [wizardMode, setWizardMode] = useState('graphical')
  const [manifestText, setManifestText] = useState('')
  const [preview, setPreview] = useState(null)
  const [result, setResult] = useState(null)
  const [wizardBusy, setWizardBusy] = useState(false)
  const [wizardError, setWizardError] = useState(null)
  // ===== 图形化顺序分配（向导数据独立于页面状态过滤） =====
  const [wizDevices, setWizDevices] = useState([])
  const [workers, setWorkers] = useState([])
  const [allocDevSel, setAllocDevSel] = useState([])
  const [allocWkSel, setAllocWkSel] = useState([])
  const [allocPrefix, setAllocPrefix] = useState('worker-')
  const [allocDevFilter, setAllocDevFilter] = useState('')
  const [allocWkFilter, setAllocWkFilter] = useState('')
  const [pendingAlloc, setPendingAlloc] = useState(null) // 锁定分配：{ macs, worker_ids, created }

  const fetchDevices = useCallback(async () => {
    try {
      const data = await getDevices(stateFilter)
      setDevices(Array.isArray(data) ? data : [])
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [stateFilter])

  useEffect(() => {
    fetchDevices()
  }, [fetchDevices])

  const fetchRegistrationWindow = useCallback(async () => {
    try {
      const res = await getRegistrationWindow()
      setRegWindow(res)
      setCountdown(res.open ? res.remaining_seconds : 0)
    } catch (e) {
      setWindowError(e.message)
    }
  }, [])

  const fetchEnforcement = useCallback(async () => {
    try {
      const res = await getEnforcement()
      setEnforcementState(res.enabled)
    } catch (e) {
      setWindowError(e.message)
    }
  }, [])

  // 复制 MAC 到剪贴板（1.5s 内显示 ✓ 反馈；剪贴板不可用时静默忽略）
  const copyMac = async (e, mac) => {
    e.stopPropagation()
    try {
      await navigator.clipboard.writeText(mac)
      setCopiedMac(mac)
      setTimeout(() => setCopiedMac((m) => (m === mac ? null : m)), 1500)
    } catch {
      /* 剪贴板不可用（非 HTTPS/权限拒绝）：忽略 */
    }
  }

  // 绑定记录：拉取该设备审计，保留 bind/unbind 类事件，最新在前
  const fetchBindings = useCallback(async (mac) => {
    setBindings(null)
    setBindingsError(null)
    try {
      const data = await getOperations(0, 500, mac)
      const entries = (data.entries || []).filter(
        (e) => e.op === 'device.bind' || e.op === 'device.unbind'
      )
      setBindings([...entries].reverse())
    } catch (e) {
      setBindingsError(e.message)
    }
  }, [])

  useEffect(() => {
    fetchRegistrationWindow()
    fetchEnforcement()
  }, [fetchRegistrationWindow, fetchEnforcement])

  // 窗口开启期间每秒递减倒计时；归零后重新拉取（后端 TTL 到期懒计算关闭）
  useEffect(() => {
    if (!regWindow?.open) return undefined
    const iv = setInterval(() => setCountdown((c) => c - 1), 1000)
    return () => clearInterval(iv)
  }, [regWindow?.open])

  useEffect(() => {
    if (regWindow?.open && countdown <= 0) fetchRegistrationWindow()
  }, [countdown, regWindow?.open, fetchRegistrationWindow])

  // 窗口开启期间每 10s 重拉窗口状态：已放行数派生自设备台账，不轮询就看不到额度被消耗。
  // 同时刷新设备列表（多选 / 绑定向导进行中除外，避免打断选择），否则计数在涨、表格却还是旧行。
  useEffect(() => {
    if (!regWindow?.open) return undefined
    const iv = setInterval(() => {
      fetchRegistrationWindow()
      if (!unbindMode && !wizardOpen) fetchDevices()
    }, 10000)
    return () => clearInterval(iv)
  }, [regWindow?.open, fetchRegistrationWindow, fetchDevices, unbindMode, wizardOpen])

  // 开启注册窗口：窗口开启期间新设备上报可携公钥自动注册入池（TTL 到期自动关闭；额度用尽即停）
  const handleOpenWindow = async () => {
    setWindowBusy(true)
    setWindowError(null)
    const maxDevices = maxChoice === '' ? null : Number(maxChoice)
    if (maxDevices !== null && (!Number.isInteger(maxDevices) || maxDevices < 1 || maxDevices > maxDevicesUpper)) {
      setWindowError(t('devices.regWindow.maxInvalid', { max: maxDevicesUpper }))
      setWindowBusy(false)
      return
    }
    try {
      const res = await openRegistrationWindow(Number(ttlChoice), maxDevices)
      setRegWindow(res)
      setCountdown(res.remaining_seconds || 0)
    } catch (e) {
      setWindowError(e.message)
    } finally {
      setWindowBusy(false)
    }
  }

  // 提前关闭注册窗口：窗口期外上报只记指纹不入池（已入池/已绑定设备不受影响）
  const handleCloseWindow = async () => {
    setWindowBusy(true)
    setWindowError(null)
    try {
      await closeRegistrationWindow()
      await fetchRegistrationWindow()  // 保留 limits（关闭后的表单按声明上限收敛）
      setCountdown(0)
    } catch (e) {
      setWindowError(e.message)
    } finally {
      setWindowBusy(false)
    }
  }

  // 切换设备身份验签强制开关：开启后 /boot-vars 只向验签通过的已绑定设备下发
  const handleToggleEnforcement = async () => {
    setEnforcementBusy(true)
    setWindowError(null)
    try {
      const res = await setEnforcement(!enforcement)
      setEnforcementState(res.enabled)
    } catch (e) {
      setWindowError(e.message)
    } finally {
      setEnforcementBusy(false)
    }
  }

  const toggleGuide = () => setGuideOpen(!guideOpen)

  const handleCheck = (e, d) => {
    e.preventDefault()
    e.stopPropagation()
    if (e.shiftKey && anchor) {
      const idx1 = filtered.findIndex((x) => x.mac === anchor)
      const idx2 = filtered.findIndex((x) => x.mac === d.mac)
      if (idx1 >= 0 && idx2 >= 0) {
        const [lo, hi] = idx1 <= idx2 ? [idx1, idx2] : [idx2, idx1]
        const macs = filtered.slice(lo, hi + 1).map((x) => x.mac)
        setSelected((prev) => Array.from(new Set([...prev, ...macs])))
      }
    } else {
      setSelected((prev) =>
        prev.includes(d.mac) ? prev.filter((m) => m !== d.mac) : [...prev, d.mac]
      )
    }
    setAnchor(d.mac)
  }

  const toggleUnbind = () => {
    const next = !unbindMode
    setUnbindMode(next)
    setSelected([])
    setAnchor(null)
    setUnbindError(null)
    setUnbindResult(null)
  }

  const handleUnbind = async () => {
    if (selected.length === 0) return
    setUnbinding(true)
    setUnbindError(null)
    setUnbindResult(null)
    const ok = []
    const failed = []
    for (const mac of selected) {
      try {
        await unbindDevice(mac)
        ok.push(mac)
      } catch (e) {
        failed.push({ mac, error: e.message })
      }
    }
    setUnbindResult({ ok, failed })
    setSelected([])
    setAnchor(null)
    await fetchDevices()
    setUnbinding(false)
  }

  const handleRegister = async (e) => {
    e.preventDefault()
    setRegistering(true)
    setRegisterError(null)
    const body = { mac: regForm.mac.trim() }
    ;['uuid', 'manufacturer', 'product', 'serial'].forEach((k) => {
      if (regForm[k].trim()) body[k] = regForm[k].trim()
    })
    try {
      await createDevice(body)
      setRegForm({ mac: '', uuid: '', manufacturer: '', product: '', serial: '' })
      setShowRegister(false)
      await fetchDevices()
    } catch (err) {
      setRegisterError(err.message)
    } finally {
      setRegistering(false)
    }
  }

  const handleImport = async (e) => {
    e.preventDefault()
    const entries = parseColumnLines(importText).map((line) => {
      const cols = line.split(/[,\t]/).map((c) => c.trim())
      const entry = { mac: cols[0] }
      if (cols[1]) entry.uuid = cols[1]
      if (cols[2]) entry.manufacturer = cols[2]
      if (cols[3]) entry.product = cols[3]
      if (cols[4]) entry.serial = cols[4]
      return entry
    })
    if (entries.length === 0) {
      setImportError(t('devices.importEmpty'))
      return
    }
    setImporting(true)
    setImportError(null)
    setImportResult(null)
    try {
      const res = await importDevices(entries)
      setImportResult(res)
      setImportText('')
      await fetchDevices()
    } catch (err) {
      setImportError(err.message)
    } finally {
      setImporting(false)
    }
  }

  // ===== 绑定向导 =====
  // worker_id 尾部序号（补建结果恢复生成顺序用：batch 响应按 succeeded/skipped 分组）
  const numSuffix = (id) => parseInt(String(id).match(/(\d+)$/)?.[1] || '0', 10)

  // 向导数据：全量设备（不受页面状态过滤影响）+ Worker 列表
  const fetchWizardData = async () => {
    try {
      const [devs, wks] = await Promise.all([getDevices('all'), getWorkers()])
      setWizDevices(Array.isArray(devs) ? devs : [])
      setWorkers(Array.isArray(wks) ? wks : [])
    } catch (e) {
      setWizardError(e.message)
    }
  }

  const wizardBody = () => parseManifestLines(manifestText)

  const openWizard = () => {
    setWizardOpen(true)
    setWizardMode('graphical')
    setManifestText('')
    setPreview(null)
    setResult(null)
    setWizardError(null)
    setAllocDevSel([])
    setAllocWkSel([])
    setAllocPrefix('worker-')
    setAllocDevFilter('')
    setAllocWkFilter('')
    setPendingAlloc(null)
    fetchWizardData()
  }

  const closeWizard = () => {
    setWizardOpen(false)
    setPreview(null)
    setResult(null)
    setWizardError(null)
  }

  // 固定按入池时间（first_seen）升序：设备列表/绑定向导的指代顺序（“从上往下第 N 台”）；无 first_seen 排最后
  const byPoolOrder = (d) => {
    const t = Date.parse(d.first_seen)
    return Number.isFinite(t) ? t : Number.MAX_SAFE_INTEGER
  }

  // 注册窗口倒计时 mm:ss 格式化
  const fmtCountdown = (sec) => {
    const m = Math.floor(Math.max(0, sec) / 60)
    const s = Math.max(0, sec) % 60
    return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
  }

  // 图形化：池中未绑定设备（与主列表同序，按入池时间）+ 未绑定设备、可接收绑定的 Worker
  const allocDevices = wizDevices
    .filter((d) => d.state === 'pooled')
    .sort((a, b) => byPoolOrder(a) - byPoolOrder(b))
  const allocWorkers = workers.filter((w) => !w.bound_device)
  const visibleAllocDevs = allocDevices.filter((d) => {
    const term = allocDevFilter.toLowerCase()
    return !term || (d.mac || '').toLowerCase().includes(term)
  })

  const toggleAllocDev = (mac) =>
    setAllocDevSel((prev) => (prev.includes(mac) ? prev.filter((m) => m !== mac) : [...prev, mac]))

  const toggleAllocWk = (id) =>
    setAllocWkSel((prev) => (prev.includes(id) ? prev.filter((w) => w !== id) : [...prev, id]))

  const toggleAllocDevAll = () => {
    const allChecked = visibleAllocDevs.every((d) => allocDevSel.includes(d.mac))
    setAllocDevSel((prev) =>
      allChecked
        ? prev.filter((m) => !visibleAllocDevs.some((d) => d.mac === m))
        : Array.from(new Set([...prev, ...visibleAllocDevs.map((d) => d.mac)]))
    )
  }

  const handlePreview = async () => {
    setWizardBusy(true)
    setWizardError(null)
    setPreview(null)
    setResult(null)
    try {
      if (wizardMode === 'manifest') {
        const pairs = wizardBody()
        if (pairs.length === 0) {
          setWizardError(t('devices.wizard.manifestEmpty'))
          return
        }
        const res = await batchBindPreview({ mode: 'manifest', pairs })
        setPreview(res)
      } else {
        // 图形化顺序分配：设备按列表顺序，worker 按勾选顺序；worker 多则截断，少则按前缀自动补建
        const macs = allocDevSel
        if (macs.length === 0) {
          setWizardError(t('devices.wizard.allocEmptyError'))
          return
        }
        let workerIds = [...allocWkSel].slice(0, macs.length)
        let created = 0
        const need = macs.length - workerIds.length
        if (need > 0) {
          const prefix = allocPrefix.trim()
          if (!prefix) {
            setWizardError(t('devices.wizard.allocPrefixRequired'))
            return
          }
          const res = await batchCreateWorkers({ count: need, name_prefix: prefix })
          const ids = [...res.succeeded, ...res.skipped]
            .map((x) => x.worker_id)
            .sort((a, b) => numSuffix(a) - numSuffix(b))
          workerIds = workerIds.concat(ids)
          created = res.succeeded.length
        }
        setPendingAlloc({ macs, worker_ids: workerIds, created })
        const res = await batchBindPreview({ mode: 'sequential', macs, worker_ids: workerIds })
        setPreview(res)
      }
    } catch (err) {
      setWizardError(err.message)
    } finally {
      setWizardBusy(false)
    }
  }

  const handleExecute = async () => {
    setWizardBusy(true)
    setWizardError(null)
    try {
      let body
      if (wizardMode === 'manifest') {
        body = { mode: 'manifest', pairs: wizardBody() }
      } else {
        if (!pendingAlloc) {
          setWizardError(t('devices.wizard.allocPreviewFirst'))
          return
        }
        body = { mode: 'sequential', macs: pendingAlloc.macs, worker_ids: pendingAlloc.worker_ids }
      }
      const res = await batchBind(body)
      setResult(res)
      await fetchDevices()
    } catch (err) {
      setWizardError(err.message)
    } finally {
      setWizardBusy(false)
    }
  }

  // 结果页重试：graphical 模式沿用锁定的分配（不重复补建），manifest 重新解析
  const handleRetry = async () => {
    if (wizardMode === 'graphical' && pendingAlloc) {
      setWizardBusy(true)
      setWizardError(null)
      setPreview(null)
      setResult(null)
      try {
        const res = await batchBindPreview({
          mode: 'sequential',
          macs: pendingAlloc.macs,
          worker_ids: pendingAlloc.worker_ids,
        })
        setPreview(res)
      } catch (err) {
        setWizardError(err.message)
      } finally {
        setWizardBusy(false)
      }
      return
    }
    await handlePreview()
  }

  const handleExport = () => {
    try {
      const rows = [
        ['mac', 'worker_id', 'category', 'reason'],
        ...(preview?.matched || []).map((m) => [m.mac, m.worker_id, 'matched', m.fingerprint_mismatch?.reason || '']),
        ...(preview?.conflicts || []).map((c) => [c.mac, c.worker_id, 'conflict', c.reason]),
        ...(preview?.not_found || []).map((n) => [n.mac, n.worker_id, 'not_found', n.reason]),
      ]
      const tsv = rows.map((r) => r.join('\t')).join('\n')
      const blob = new Blob([tsv], { type: 'text/tab-separated-values;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `bind-preview-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.tsv`
      a.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      setWizardError(t('devices.wizard.exportFailed', { error: err.message }))
    }
  }

  const stateVariant = (d) => {
    if (d.state === 'bound') return 'ok'
    if (d.state === 'revoked') return 'error'
    return 'default'
  }

  const filtered = devices
    .filter((d) => {
      const term = filter.toLowerCase()
      return (
        (d.mac || '').toLowerCase().includes(term) ||
        (d.bound_worker_id || '').toLowerCase().includes(term) ||
        (d.fingerprint?.manufacturer || '').toLowerCase().includes(term) ||
        (d.fingerprint?.product || '').toLowerCase().includes(term) ||
        (d.fingerprint?.serial || '').toLowerCase().includes(term)
      )
    })
    .sort((a, b) => byPoolOrder(a) - byPoolOrder(b))

  if (loading) return <p className="page-loading">{t('common.loading')}</p>
  if (error) return <p className="page-error">{error}</p>

  return (
    <div>
      <div className="page-header">
        <h2 className="page-title">{t('devices.title')}</h2>
        <div className="page-actions">
          <Button variant="primary" onClick={openWizard}>
            {t('devices.bindWizard')}
          </Button>
          <Button
            variant={unbindMode ? 'ghost' : 'danger'}
            onClick={toggleUnbind}
          >
            {unbindMode ? t('devices.unbind.exit') : t('devices.unbind.enter')}
          </Button>
          <Button
            variant={showImport ? 'ghost' : 'primary'}
            onClick={() => setShowImport(!showImport)}
            title={t('devices.importTooltip')}
          >
            {showImport ? t('workers.cancel') : t('devices.import')}
          </Button>
          <Button
            variant={showRegister ? 'ghost' : 'primary'}
            onClick={() => setShowRegister(!showRegister)}
          >
            {showRegister ? t('workers.cancel') : t('devices.register')}
          </Button>
        </div>
      </div>

      {showRegister && (
        <form className="create-form" onSubmit={handleRegister}>
          <div className="create-form-title">{t('devices.registerTitle')}</div>
          <p className="create-hint">{t('devices.registerHint')}</p>
          <div className="create-form-grid">
            <Input
              label={t('devices.macLabel')}
              name="mac"
              value={regForm.mac}
              onChange={(e) => setRegForm((p) => ({ ...p, mac: e.target.value }))}
              placeholder={t('devices.macPlaceholder')}
              required
            />
            <Input
              label={`${t('devices.uuid')} ${t('devices.optional')}`}
              name="uuid"
              value={regForm.uuid}
              onChange={(e) => setRegForm((p) => ({ ...p, uuid: e.target.value }))}
              placeholder="4C4C4544-…"
            />
            <Input
              label={`${t('devices.manufacturerLabel')} ${t('devices.optional')}`}
              name="manufacturer"
              value={regForm.manufacturer}
              onChange={(e) => setRegForm((p) => ({ ...p, manufacturer: e.target.value }))}
            />
            <Input
              label={`${t('devices.productLabel')} ${t('devices.optional')}`}
              name="product"
              value={regForm.product}
              onChange={(e) => setRegForm((p) => ({ ...p, product: e.target.value }))}
            />
            <Input
              label={`${t('devices.serialLabel')} ${t('devices.optional')}`}
              name="serial"
              value={regForm.serial}
              onChange={(e) => setRegForm((p) => ({ ...p, serial: e.target.value }))}
            />
          </div>
          {registerError && <p className="create-error">{registerError}</p>}
          <Button type="submit" disabled={registering}>
            {registering ? t('devices.adding') : t('devices.addBtn')}
          </Button>
        </form>
      )}

      {showImport && (
        <form className="create-form" onSubmit={handleImport}>
          <div className="create-form-title">{t('devices.importTitle')}</div>
          <p className="create-hint">{t('devices.importHint')}</p>
          <textarea
            className="devices-textarea"
            value={importText}
            onChange={(e) => setImportText(e.target.value)}
            placeholder={t('devices.importPlaceholder')}
            rows={6}
          />
          {importError && <p className="create-error">{importError}</p>}
          {importResult && (
            <p className="create-hint">
              {t('devices.importResult', {
                created: importResult.created.length,
                skipped: importResult.skipped.length,
                failed: importResult.failed.length,
              })}
            </p>
          )}
          <Button type="submit" disabled={importing}>
            {importing ? t('devices.importing') : t('devices.importBtn')}
          </Button>
        </form>
      )}

      {wizardOpen && (
        <div className="wizard-panel">
          <div className="create-form-title">{t('devices.wizard.title')}</div>

          <div className="wizard-mode">
            <label className="wizard-mode-option">
              <input
                type="radio"
                name="wizard-mode"
                checked={wizardMode === 'manifest'}
                onChange={() => setWizardMode('manifest')}
              />
              <span>
                {t('devices.wizard.modeManifest')}
                <small>{t('devices.wizard.modeManifestHint')}</small>
              </span>
            </label>
            <label className="wizard-mode-option">
              <input
                type="radio"
                name="wizard-mode"
                checked={wizardMode === 'graphical'}
                onChange={() => setWizardMode('graphical')}
              />
              <span>
                {t('devices.wizard.modeGraphical')}
                <small>{t('devices.wizard.modeGraphicalHint')}</small>
              </span>
            </label>
          </div>

          {wizardMode === 'manifest' ? (
            <div className="wizard-input-block">
              <div className="wizard-input-head">
                <label className="wizard-input-label">{t('devices.wizard.pasteLabel')}</label>
                <label className="wizard-upload">
                  <input
                    type="file"
                    accept=".txt,.csv,text/plain"
                    onChange={async (e) => {
                      const f = e.target.files?.[0]
                      if (f) setManifestText(await readFile(f))
                      e.target.value = ''
                    }}
                  />
                  {t('devices.wizard.upload')}
                </label>
              </div>
              <textarea
                className="devices-textarea devices-textarea-mono"
                value={manifestText}
                onChange={(e) => setManifestText(e.target.value)}
                placeholder={t('devices.wizard.pastePlaceholder')}
                rows={8}
              />
            </div>
          ) : (
            <div className="alloc-layout">
              <div className="alloc-panel">
                <div className="alloc-panel-head">
                  <span>{t('devices.wizard.allocDevicesTitle')}</span>
                  <span className="alloc-count">{allocDevices.length}</span>
                </div>
                <input
                  className="workers-filter alloc-search"
                  placeholder={t('devices.filter')}
                  value={allocDevFilter}
                  onChange={(e) => setAllocDevFilter(e.target.value)}
                />
                <div className="alloc-list">
                  {visibleAllocDevs.length === 0 ? (
                    <div className="alloc-empty">
                      {allocDevices.length === 0
                        ? t('devices.wizard.allocDevicesEmpty')
                        : t('devices.noMatch')}
                    </div>
                  ) : (
                    visibleAllocDevs.map((d) => (
                      <label className="alloc-row" key={d.mac}>
                        <input
                          type="checkbox"
                          checked={allocDevSel.includes(d.mac)}
                          onChange={() => toggleAllocDev(d.mac)}
                        />
                        <span className="alloc-mac td-mono">{d.mac}</span>
                        <span className="alloc-fp">
                          {[d.fingerprint?.manufacturer, d.fingerprint?.product, d.fingerprint?.serial]
                            .filter(Boolean)
                            .join(' · ') || '—'}
                        </span>
                      </label>
                    ))
                  )}
                </div>
                {visibleAllocDevs.length > 0 && (
                  <div className="alloc-panel-foot">
                    <label className="alloc-row alloc-row-all">
                      <input
                        type="checkbox"
                        checked={visibleAllocDevs.every((d) => allocDevSel.includes(d.mac))}
                        onChange={toggleAllocDevAll}
                      />
                      {t('devices.wizard.allocDevicesAll')}
                    </label>
                  </div>
                )}
              </div>
              <div className="alloc-panel">
                <div className="alloc-panel-head">
                  <span>{t('devices.wizard.allocWorkersTitle')}</span>
                  <span className="alloc-count">{allocWorkers.length}</span>
                </div>
                <input
                  className="workers-filter alloc-search"
                  placeholder={t('workers.filter')}
                  value={allocWkFilter}
                  onChange={(e) => setAllocWkFilter(e.target.value)}
                />
                <div className="alloc-list">
                  {allocWorkers.length === 0 ? (
                    <div className="alloc-empty">{t('devices.wizard.allocWorkersEmpty')}</div>
                  ) : (
                    allocWorkers
                      .filter((w) => {
                        const term = allocWkFilter.toLowerCase()
                        return !term || (w.worker_id || '').toLowerCase().includes(term)
                      })
                      .map((w) => (
                        <label className="alloc-row" key={w.worker_id}>
                          <input
                            type="checkbox"
                            checked={allocWkSel.includes(w.worker_id)}
                            onChange={() => toggleAllocWk(w.worker_id)}
                          />
                          <span className="alloc-mac td-mono">{w.worker_id}</span>
                          <Badge>{w.readiness || 'idle'}</Badge>
                        </label>
                      ))
                  )}
                </div>
              </div>
            </div>
          )}

          <div className="alloc-bar">
            {wizardMode === 'graphical' && (
              <>
                <span className="alloc-summary">
                  {t('devices.wizard.allocSummary', {
                    d: allocDevSel.length,
                    w: allocWkSel.length,
                    n: Math.max(0, allocDevSel.length - allocWkSel.length),
                  })}
                </span>
                {allocDevSel.length > allocWkSel.length && (
                  <span className="alloc-prefix">
                    <label>{t('devices.wizard.allocPrefixLabel')}</label>
                    <Input
                      value={allocPrefix}
                      onChange={(e) => setAllocPrefix(e.target.value)}
                      style={{ width: 120 }}
                    />
                  </span>
                )}
              </>
            )}
          </div>

          {wizardError && <p className="create-error">{wizardError}</p>}

          {!preview && (
            <div className="wizard-actions">
              <Button variant="primary" onClick={handlePreview} disabled={wizardBusy}>
                {wizardBusy ? t('devices.wizard.previewing') : t('devices.wizard.preview')}
              </Button>
              <Button variant="ghost" onClick={closeWizard}>
                {t('devices.wizard.close')}
              </Button>
            </div>
          )}

          {preview && !result && (
            <div className="wizard-preview">
              {wizardMode === 'graphical' && pendingAlloc?.created > 0 && (
                <p className="create-hint">
                  {t('devices.wizard.allocCreated', { n: pendingAlloc.created })}
                </p>
              )}
              <div className="wizard-summary">
                <span className="ws-matched">{t('devices.wizard.matched')}: {preview.summary.ok}</span>
                <span className="ws-conflict">{t('devices.wizard.conflicts')}: {preview.summary.conflict}</span>
                <span className="ws-notfound">{t('devices.wizard.notFound')}: {preview.summary.not_found}</span>
                <span className="ws-total">{t('devices.wizard.total', { n: preview.summary.total })}</span>
                <Button variant="ghost" className="wizard-export" onClick={handleExport}>
                  {t('devices.wizard.export')}
                </Button>
              </div>
              <table className="wizard-table">
                <thead>
                  <tr>
                    <th>mac</th>
                    <th>worker_id</th>
                    <th>{t('devices.state')}</th>
                    <th>reason</th>
                  </tr>
                </thead>
                <tbody>
                  {(preview.matched || []).map((m) => (
                    <tr key={`m-${m.mac}`} className="row-matched">
                      <td className="td-mono">{m.mac}</td>
                      <td className="td-mono">{m.worker_id}</td>
                      <td>{t('devices.wizard.matched')}</td>
                      <td>
                        {m.fingerprint_mismatch && (
                          <span className="mismatch-tag" title={m.fingerprint_mismatch.reason}>
                            {t('devices.wizard.mismatchTag')}
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                  {(preview.conflicts || []).map((c) => (
                    <tr key={`c-${c.mac}`} className="row-conflict">
                      <td className="td-mono">{c.mac}</td>
                      <td className="td-mono">{c.worker_id}</td>
                      <td>{t('devices.wizard.conflicts')}</td>
                      <td>{c.reason}</td>
                    </tr>
                  ))}
                  {(preview.not_found || []).map((n) => (
                    <tr key={`n-${n.mac}`} className="row-notfound">
                      <td className="td-mono">{n.mac}</td>
                      <td className="td-mono">{n.worker_id}</td>
                      <td>{t('devices.wizard.notFound')}</td>
                      <td>{n.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="wizard-actions">
                <ConfirmAction
                  trigger={
                    <Button variant="danger" disabled={wizardBusy || preview.summary.ok === 0}>
                      {wizardBusy ? t('devices.wizard.executing') : t('devices.wizard.execute')}
                    </Button>
                  }
                  message={t('devices.wizard.confirmExecute', { n: preview.summary.ok })}
                  onConfirm={handleExecute}
                />
                <Button variant="ghost" onClick={closeWizard}>
                  {t('devices.wizard.close')}
                </Button>
              </div>
            </div>
          )}

          {result && (
            <div className="wizard-result">
              <div className="batch-result-stats">
                <span className="br-ok">{t('devices.wizard.okCount', { n: result.succeeded.length })}</span>
                <span className="br-skip">{t('devices.wizard.skipCount', { n: result.skipped.length })}</span>
                <span className="br-fail">{t('devices.wizard.failCount', { n: result.failed.length })}</span>
              </div>
              {result.succeeded.length > 0 && (
                <ul className="batch-result-list">
                  {result.succeeded.map((s) => (
                    <li key={`s-${s.mac}`}>
                      <b>{s.mac}</b> → {s.worker_id}
                      {s.fingerprint_mismatch && (
                        <span className="mismatch-tag" title={s.fingerprint_mismatch.reason}>
                          {t('devices.wizard.mismatchTag')}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
              {result.skipped.length > 0 && (
                <ul className="batch-result-list">
                  {result.skipped.map((s) => (
                    <li key={`sk-${s.mac}`}>
                      <b>{s.mac}</b> → {s.worker_id}: {s.reason}
                    </li>
                  ))}
                </ul>
              )}
              {result.failed.length > 0 && (
                <ul className="batch-result-list">
                  {result.failed.map((f) => (
                    <li key={`f-${f.mac}`}>
                      <b>{f.mac}</b> → {f.worker_id}: {f.reason}
                    </li>
                  ))}
                </ul>
              )}
              <div className="wizard-actions">
                <Button variant="primary" onClick={handleRetry} disabled={wizardBusy}>
                  {t('devices.wizard.retry')}
                </Button>
                <Button variant="ghost" onClick={closeWizard}>
                  {t('devices.wizard.done')}
                </Button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* 注册窗口面板：部署期开启窗口供新设备携公钥注册入池；窗口关闭/额度用尽后上报只记指纹不入池 */}
      <div className="reg-window-panel">
        <div className="reg-window-head">
          <span className="reg-window-title">{t('devices.regWindow.title')}</span>
          <Badge variant={regWindow?.quota_exhausted ? 'partial' : regWindow?.open ? 'ok' : 'default'}>
            {regWindow?.quota_exhausted
              ? t('devices.regWindow.quotaFull')
              : regWindow?.open
                ? t('devices.regWindow.open')
                : t('devices.regWindow.closed')}
          </Badge>
        </div>
        <div className="reg-window-body">
          {regWindow?.open ? (
            <>
              <span className="reg-window-countdown" title={t('devices.regWindow.countdownHint')}>
                {t('devices.regWindow.remaining', { time: fmtCountdown(countdown) })}
              </span>
              <span className="reg-window-quota" title={t('devices.regWindow.quotaHint')}>
                {regWindow.max_devices === null || regWindow.max_devices === undefined
                  ? t('devices.regWindow.admittedNoLimit', { n: regWindow.admitted ?? 0 })
                  : t('devices.regWindow.admitted', { n: regWindow.admitted ?? 0, max: regWindow.max_devices })}
              </span>
              <Button variant="danger" onClick={handleCloseWindow} disabled={windowBusy}>
                {t('devices.regWindow.closeBtn')}
              </Button>
            </>
          ) : (
            <>
              <Select
                label=""
                name="reg-ttl"
                value={ttlChoice}
                onChange={(e) => setTtlChoice(e.target.value)}
                options={TTL_OPTIONS(t, maxTtlMinutes)}
                style={{ minWidth: 110 }}
              />
              <Input
                label=""
                name="reg-max"
                type="number"
                min="1"
                max={maxDevicesUpper}
                value={maxChoice}
                onChange={(e) => setMaxChoice(e.target.value.replace(/\D/g, '').slice(0, 4))}
                placeholder={t('devices.regWindow.maxPlaceholder')}
                style={{ width: 140 }}
              />
              <span className="reg-window-unit">{t('devices.regWindow.maxUnit')}</span>
              <Button variant="primary" onClick={handleOpenWindow} disabled={windowBusy}>
                {t('devices.regWindow.openBtn')}
              </Button>
            </>
          )}
        </div>
        <label
          className={`reg-window-enforce${enforcement ? ' rwe-on' : ''}`}
          title={t('devices.regWindow.enforceHint')}
        >
          <input
            type="checkbox"
            checked={enforcement === true}
            onChange={handleToggleEnforcement}
            disabled={enforcement === null || enforcementBusy}
          />
          <span>{t('devices.regWindow.enforce')}</span>
        </label>
        {windowError && <span className="workers-toolbar-error">{windowError}</span>}
      </div>

      <div className="workers-toolbar">
        <Select
          label=""
          name="state"
          value={stateFilter}
          onChange={(e) => setStateFilter(e.target.value)}
          options={STATE_OPTIONS(t)}
          style={{ minWidth: 140 }}
        />
        <input
          className="workers-filter"
          placeholder={t('devices.filter')}
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <span className="workers-count">{t('devices.count', { count: filtered.length })}</span>
        <Button variant="ghost" onClick={toggleGuide}>
          {t('devices.guide.btn')}
        </Button>
      </div>

      {unbindMode && (
        <div className="unbind-bar">
          <div className="unbind-bar-title">{t('devices.unbind.selected', { count: selected.length })}</div>
          <p className="batch-hint">{t('devices.unbind.hint')}</p>
          <ConfirmAction
            trigger={
              <Button variant="danger" disabled={selected.length === 0 || unbinding}>
                {unbinding ? t('devices.unbind.unbinding') : t('devices.unbind.unbindBtn')}
              </Button>
            }
            message={t('devices.unbind.confirm', { count: selected.length })}
            onConfirm={handleUnbind}
          />
          {unbindError && <p className="batch-error">{unbindError}</p>}
          {unbindResult && (
            <div className="batch-result">
              <div className="batch-result-title">{t('devices.unbind.result')}</div>
              <div className="batch-result-stats">
                <span className="br-ok">{t('devices.unbind.okCount', { n: unbindResult.ok.length })}</span>
                <span className="br-fail">{t('devices.unbind.failCount', { n: unbindResult.failed.length })}</span>
              </div>
              {unbindResult.failed.length > 0 && (
                <ul className="batch-result-list">
                  {unbindResult.failed.map((f) => (
                    <li key={f.mac}>
                      <b>{f.mac}</b>: {f.error}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      )}

      {filtered.length === 0 ? (
        <EmptyState message={filter ? t('devices.noMatch') : t('devices.noDevices')} />
      ) : (
        <div className="workers-list">
          <div className={`devices-header${unbindMode ? ' devices-header-unbind' : ''}`}>
            {unbindMode && <span className="wh-check" />}
            <span className="dh-mac">{t('devices.mac')}</span>
            <span className="dh-state">{t('devices.state')}</span>
            <span className="dh-worker">{t('devices.boundWorker')}</span>
            <span className="dh-fp">{t('devices.fingerprint')}</span>
            <span className="dh-source">{t('devices.source')}</span>
            <span className="dh-time">{t('devices.firstSeen')}</span>
          </div>
          {filtered.map((d) => (
            <div key={d.mac}>
              <div
                className={`devices-row${unbindMode ? ' dr-unbind' : ''}`}
                onClick={() => {
                  const next = expanded === d.mac ? null : d.mac
                  setExpanded(next)
                  if (next) fetchBindings(next)
                }}
                title={t('devices.expandHint')}
              >
                {unbindMode && (
                  <span
                    className="wr-check"
                    onClick={(e) => handleCheck(e, d)}
                  >
                    <input type="checkbox" checked={selected.includes(d.mac)} readOnly />
                  </span>
                )}
                <span className="dr-mac-wrap">
                  <span className="td-mono dr-mac">{d.mac}</span>
                  <button
                    type="button"
                    className="mac-copy-btn"
                    title={t('devices.copyMac')}
                    onClick={(e) => copyMac(e, d.mac)}
                  >
                    {copiedMac === d.mac ? '✓' : '⧉'}
                  </button>
                </span>
                <span className="dr-state">
                  <Badge variant={stateVariant(d)}>{d.state}</Badge>
                </span>
                <span className="dr-worker">
                  {d.bound_worker_id ? (
                    <Link
                      className="dr-worker-link"
                      to={`/workers/${d.bound_worker_id}`}
                      onClick={(e) => e.stopPropagation()}
                    >
                      {d.bound_worker_id}
                    </Link>
                  ) : (
                    '—'
                  )}
                </span>
                <span className="dr-fp">
                  {[d.fingerprint?.manufacturer, d.fingerprint?.product, d.fingerprint?.serial]
                    .filter(Boolean)
                    .join(' · ') || '—'}
                </span>
                <span className="dr-source">{d.source || '—'}</span>
                <span className="dr-time">{d.first_seen ? d.first_seen.replace('T', ' ').slice(0, 19) : '—'}</span>
              </div>
              {expanded === d.mac && (
                <div className="devices-detail">
                  <div className="devices-detail-item">
                    <span className="dd-label">uuid</span>
                    <span className="td-mono">{d.uuid || '—'}</span>
                  </div>
                  <div className="devices-detail-item">
                    <span className="dd-label">{t('devices.manufacturerLabel')}</span>
                    <span>{d.fingerprint?.manufacturer || '—'}</span>
                  </div>
                  <div className="devices-detail-item">
                    <span className="dd-label">{t('devices.productLabel')}</span>
                    <span>{d.fingerprint?.product || '—'}</span>
                  </div>
                  <div className="devices-detail-item">
                    <span className="dd-label">{t('devices.serialLabel')}</span>
                    <span>{d.fingerprint?.serial || '—'}</span>
                  </div>
                  <div className="devices-detail-item">
                    <span className="dd-label">{t('devices.lastSeen')}</span>
                    <span>{d.last_seen ? d.last_seen.replace('T', ' ').slice(0, 19) : '—'}</span>
                  </div>
                  <div className="devices-detail bind-history-section">
                    <span className="dd-label">{t('devices.bindHistory')}</span>
                    {bindingsError ? (
                      <span className="bind-empty">{bindingsError}</span>
                    ) : bindings === null ? (
                      <span className="bind-empty">{t('common.loading')}</span>
                    ) : bindings.length === 0 ? (
                      <span className="bind-empty">{t('devices.bindEmpty')}</span>
                    ) : (
                      <div className="bind-history">
                        {bindings.map((b) => (
                          <div key={b.id} className="bind-entry">
                            <span className="bind-ts">{b.ts ? b.ts.replace('T', ' ').slice(0, 19) : '—'}</span>
                            <Badge variant={b.op === 'device.bind' ? 'ok' : 'default'}>
                              {b.op === 'device.bind' ? t('devices.bindOpBind') : t('devices.bindOpUnbind')}
                            </Badge>
                            <span className="bind-worker">
                              {b.op === 'device.unbind'
                                ? b.worker_id
                                : b.old_worker_id
                                  ? `${b.old_worker_id} → ${b.worker_id}`
                                  : b.worker_id}
                            </span>
                            <span className="bind-status">{b.status}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {guideOpen && (
        <div className="guide-overlay" onClick={toggleGuide}>
          <div className="guide-panel" onClick={(e) => e.stopPropagation()}>
            <div className="guide-panel-title">{t('devices.guide.title')}</div>
            {[
              ['regWindowTitle', 'regWindowBody'],
              ['filterTitle', 'filterBody'],
              ['actionsTitle', 'actionsBody'],
              ['columnsTitle', 'columnsBody'],
              ['expandTitle', 'expandBody'],
            ].map(([titleKey, bodyKey]) => (
              <div className="guide-section" key={titleKey}>
                <div className="guide-section-title">{t(`devices.guide.${titleKey}`)}</div>
                <p className="guide-section-body">{t(`devices.guide.${bodyKey}`)}</p>
              </div>
            ))}
            <div className="guide-actions">
              <Button variant="primary" onClick={toggleGuide}>
                {t('devices.guide.close')}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
