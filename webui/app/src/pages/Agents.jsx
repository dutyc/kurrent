import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { getAgents, issueBootstrapToken, probeAgent, updateAgent } from '../api/client'
import { newNodeId } from '../api/identity'
import { useI18n } from '../i18n'
import Card from '../components/Card'
import Badge from '../components/Badge'
import Button from '../components/Button'
import CodeBlock from '../components/CodeBlock'
import EmptyState from '../components/EmptyState'
import Input from '../components/Input'
import Modal from '../components/Modal'
import './Agents.css'

function AgentCard({ agent, t, onEdit }) {
  const capLabel = (key) => {
    const val = agent.capabilities?.[key]
    if (!val) return t('agents.unknown')
    const labels = t('agents.capLabels')
    // 前缀匹配：动态文案（如 full copy only (reflink unsupported on ext4)）归并到静态条目
    const hit = Object.keys(labels).find((k) => val.startsWith(k))
    return hit ? labels[hit] : val
  }
  return (
    <Card className="agent-card">
      <div className="agent-card-header">
        <div className="agent-card-title">
          <span className="agent-name">{agent.id}</span>
          {agent.remark && <span className="agent-remark">{agent.remark}</span>}
        </div>
        <div className="agent-card-actions">
          {!agent.enabled && <Badge>{t('agents.disabled')}</Badge>}
          {agent.health && (
            <span title={agent.error || undefined}>
              <Badge>{agent.health}</Badge>
            </span>
          )}
          <Button
            variant="ghost"
            className="agent-edit-btn"
            title={t('agents.edit')}
            onClick={(e) => {
              e.preventDefault()
              e.stopPropagation()
              onEdit(agent)
            }}
          >
            {t('agents.edit')}
          </Button>
        </div>
      </div>
      <div className="agent-props">
        <div className="agent-prop">
          <span className="ap-label">{t('agents.backend')}</span>
          <span className="ap-value">{agent.capabilities?.backend || t('agents.unknown')}</span>
        </div>
        {/* 数据面端口（仅 nvmet）：与 health Badge 分开表达——health=degraded 是存储后端不可达，
            port.ready=false 是数据面 TLS 端口没起来，二者相互独立（见 ENCRYPTION-POSTURE.md R2） */}
        {agent.capabilities?.backend === 'nvmet' && (
          <div className="agent-prop">
            <span className="ap-label">{t('agents.dataPlane')}</span>
            <span className="ap-value ap-inline">
              {agent.capabilities?.port ? (
                <>
                  <span className="ap-mono">
                    {agent.capabilities.port.trsvcid} · {agent.capabilities.port.tsas}
                  </span>
                  <Badge variant={agent.capabilities.port.ready ? 'ok' : 'error'}>
                    {agent.capabilities.port.ready ? t('agents.portReady') : t('agents.portNotReady')}
                  </Badge>
                </>
              ) : (
                <Badge variant="error">{t('agents.dataPlaneDisabled')}</Badge>
              )}
            </span>
          </div>
        )}
        <div className="agent-prop">
          <span className="ap-label">{t('agents.fsType')}</span>
          <span className="ap-value ap-mono">{agent.capabilities?.fs_type || t('agents.unknown')}</span>
        </div>
        <div className="agent-prop">
          <span className="ap-label">{t('agents.baseUrl')}</span>
          <span className="ap-value ap-mono">{agent.base_url || t('agents.unknown')}</span>
        </div>
        <div className="agent-prop">
          <span className="ap-label">{t('agents.baseNqn')}</span>
          <span className="ap-value ap-mono">
            {agent.capabilities?.base_nqn
              ? `${agent.capabilities.base_nqn}${agent.storager_ip ? ` (${agent.storager_ip})` : ''}`
              : (agent.storager_ip || t('agents.unknown'))}
          </span>
        </div>
        {agent.capabilities?.cd && (
          <div className="agent-prop">
            <span className="ap-label">{t('agents.cdSupport')}</span>
            <span className="ap-value">{t('agents.yes')}</span>
          </div>
        )}
        <div className="agent-prop">
          <span className="ap-label">{t('agents.diskRole')}</span>
          <span className="ap-value">{agent.role?.disk ? t('agents.yes') : t('agents.no')}</span>
        </div>
        <div className="agent-prop">
          <span className="ap-label">{t('agents.clone')}</span>
          <span className="ap-value">{capLabel('clone')}</span>
        </div>
        <div className="agent-prop">
          <span className="ap-label">{t('agents.emptyDisk')}</span>
          <span className="ap-value">{capLabel('empty_disk')}</span>
        </div>
        {agent.tags && agent.tags.length > 0 && (
          <div className="agent-prop">
            <span className="ap-label">{t('agents.tags')}</span>
            <span className="ap-value">
              {agent.tags.map((tag) => (
                <span key={tag} className="agent-tag">{tag}</span>
              ))}
            </span>
          </div>
        )}
      </div>
    </Card>
  )
}

// 编辑 Agent 的两步表单：探测 → 确认 / 修改参数 → 保存（id 只读，走路径参数）
function AgentForm({ agentId, initial, onClose, onSaved }) {
  const { t } = useI18n()
  // 自动登记（tags 含 auto）的节点：地址由节点声明管理（renew 自动对齐），控制面只读；
  // 手工登记节点仍可改
  const declarationManaged = (initial?.tags || []).includes('auto')
  const [form, setForm] = useState(() => ({
    base_url: initial?.base_url || '',
    remark: initial?.remark || '',
    storager_ip: initial?.storager_ip || '',
    role_disk: initial?.role?.disk ?? true,
    role_cd: initial?.role?.cd ?? false,
    tags: (initial?.tags || []).join(', '),
    enabled: initial?.enabled ?? true,
  }))
  const [probe, setProbe] = useState(null)
  // storager_ip 折叠框：默认折叠（编辑模式已有值时展开），探测成功后自动展开供确认/修改
  const [iscsiOpen, setIscsiOpen] = useState(() => Boolean(initial?.storager_ip))
  const [probing, setProbing] = useState(false)
  const [probeError, setProbeError] = useState(null)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState(null)

  const setField = (name) => (e) => {
    const value = e.target.type === 'checkbox' ? e.target.checked : e.target.value
    setForm((prev) => ({ ...prev, [name]: value }))
    if (name === 'base_url') setProbe(null) // 地址变更后旧探测结果失效
  }

  const handleProbe = async (e) => {
    e.preventDefault()
    setProbing(true)
    setProbeError(null)
    setSaveError(null)
    try {
      const result = await probeAgent({
        base_url: form.base_url.trim(),
        agent_id: agentId,
      })
      setProbe(result)
      setIscsiOpen(true) // 探测推导了数据面地址（base_url 主机名），展开以便确认或改为 Worker 可达的局域网 IP
      setForm((prev) => ({
        ...prev,
        role_disk: result.role.disk,
        role_cd: result.role.cd,
        // 探测前已手填的数据面地址优先保留，未被探测推导值覆盖
        storager_ip: prev.storager_ip || result.storager_ip,
        tags: result.tags.join(', '),
      }))
    } catch (err) {
      setProbeError(err.message)
      setProbe(null)
    } finally {
      setProbing(false)
    }
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!probe) return // 须先探测成功才允许注册 / 保存
    setSaving(true)
    setSaveError(null)
    const body = {
      base_url: form.base_url.trim(),
      remark: form.remark.trim(),
      role: { disk: form.role_disk, cd: form.role_cd },
      enabled: form.enabled,
    }
    if (form.storager_ip.trim()) body.storager_ip = form.storager_ip.trim()
    const tags = form.tags.split(',').map((s) => s.trim()).filter(Boolean)
    if (tags.length) body.tags = tags
    try {
      await updateAgent(agentId, body)
      onSaved()
    } catch (err) {
      setSaveError(err.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <form className="create-form" onSubmit={handleSubmit}>
      <div className="create-form-title">{t('agents.editTitle')}</div>
      <p className="create-hint">{t('agents.probeHint')}</p>
      <div className="create-form-grid">
        <Input label={t('agents.idLabel')} value={agentId} disabled />
        <Input
          label={t('agents.baseUrl')}
          name="base_url"
          value={form.base_url}
          onChange={setField('base_url')}
          placeholder={t('agents.baseUrlPlaceholder')}
          required
          disabled={declarationManaged}
        />
        {declarationManaged && <p className="create-hint">{t('agents.baseUrlManaged')}</p>}
        <Input
          label={t('agents.remarkLabel')}
          name="remark"
          value={form.remark}
          onChange={setField('remark')}
          placeholder={t('agents.remarkPlaceholder')}
        />
      </div>
      <div className="iscsi-collapse">
        <button
          type="button"
          className={`iscsi-collapse-toggle${iscsiOpen ? ' open' : ''}`}
          onClick={() => setIscsiOpen((v) => !v)}
          aria-expanded={iscsiOpen}
        >
          <span className="iscsi-collapse-arrow">▶</span>
          {t('agents.iscsiCollapseLabel')}
        </button>
        {iscsiOpen && (
          <div className="iscsi-collapse-body">
            <Input
              label={t('agents.iscsiServer')}
              name="storager_ip"
              value={form.storager_ip}
              onChange={setField('storager_ip')}
              placeholder={t('agents.iscsiServerPlaceholder')}
            />
          </div>
        )}
      </div>
      {probeError && <p className="create-error">{probeError}</p>}
      <div className="create-actions">
        <Button
          type="button"
          variant="primary"
          onClick={handleProbe}
          disabled={probing || !form.base_url.trim()}
        >
          {probing ? t('agents.probing') : t('agents.probe')}
        </Button>
        <Button variant="ghost" type="button" onClick={onClose}>
          {t('agents.cancel')}
        </Button>
      </div>

      {probe && (
        <div className="probe-result">
          <div className="probe-result-title">{t('agents.probeResult')}</div>
          <div className="probe-meta">
            {probe.backend && (
              <span className="agent-tag">{t('agents.probeBackend')}: {probe.backend}</span>
            )}
            {probe.fs_type && (
              <span className="agent-tag">{t('agents.fsType')}: {probe.fs_type}</span>
            )}
            {probe.base_nqn && (
              <span className="agent-tag">{t('agents.probeBaseNqn')}: {probe.base_nqn}</span>
            )}
            {probe.clone && <span className="agent-tag">{probe.clone}</span>}
            {probe.empty_disk && <span className="agent-tag">{probe.empty_disk}</span>}
            {probe.persistent && <span className="agent-tag">{probe.persistent}</span>}
          </div>
          <p className="create-hint">{t('agents.probeMeta')}</p>
          <div className="create-form-grid">
            <Input
              label={t('agents.tagsInput')}
              name="tags"
              value={form.tags}
              onChange={setField('tags')}
              placeholder={t('agents.tagsPlaceholder')}
            />
          </div>
          <div className="agent-role-checks">
            <label className="agent-role-check">
              <input
                type="checkbox"
                checked={form.role_disk}
                onChange={setField('role_disk')}
              />
              {t('agents.roleDisk')}
            </label>
            <label className="agent-role-check">
              <input
                type="checkbox"
                checked={form.role_cd}
                onChange={setField('role_cd')}
              />
              {t('agents.roleCd')}
            </label>
            <label className="agent-role-check">
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={setField('enabled')}
              />
              {t('agents.enabled')}
            </label>
          </div>
          {saveError && <p className="create-error">{saveError}</p>}
          <div className="create-actions">
            <Button type="submit" disabled={saving}>
              {saving ? t('agents.saving') : t('agents.saveBtn')}
            </Button>
            <Button variant="ghost" type="button" onClick={onClose}>
              {t('agents.cancel')}
            </Button>
          </div>
        </div>
      )}
    </form>
  )
}

// 加入节点弹窗：签发绑定节点身份的 bootstrap token（agent_id = 声明 metadata.name）
// 并输出带控制面地址的 join 命令（节点粘贴即用：声明 storager/kurrent.yaml 缺失时由
// join 按命令生成；nvmet-host 凭据由 enroll 按能力自动派生）。
function JoinAgentModal({ onClose }) {
  const { t } = useI18n()
  const [cpUrl, setCpUrl] = useState(() => `https://${window.location.hostname}`)
  const [agentId] = useState(newNodeId)
  const [issuing, setIssuing] = useState(false)
  const [error, setError] = useState(null)
  const [tok, setTok] = useState(null) // { token, expires_at, agent_id }
  const [copied, setCopied] = useState(false)

  const handleIssue = async (e) => {
    e.preventDefault()
    setIssuing(true)
    setError(null)
    setTok(null)
    try {
      setTok(await issueBootstrapToken(agentId.trim()))
    } catch (err) {
      setError(err.message)
    } finally {
      setIssuing(false)
    }
  }

  const joinCmd = () =>
    tok ? `kurrent join ${cpUrl.trim()} --token ${tok.token} --agent-id ${tok.agent_id || agentId.trim()}` : ''

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(joinCmd())
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch { /* 剪贴板不可用时忽略 */ }
  }

  return (
    <Modal
      title={t('agents.joinTitle')}
      onClose={onClose}
      width="640px"
      footer={
        <Button variant="primary" onClick={onClose}>
          {t('agents.done')}
        </Button>
      }
    >
      <form onSubmit={handleIssue}>
        <p className="create-hint">{t('agents.joinHint')}</p>
        <div className="create-form-grid">
          <Input
            label={t('agents.agentIdLabel')}
            value={agentId}
            readOnly
          />
          <Input
            label={t('agents.cpUrlLabel')}
            value={cpUrl}
            onChange={(e) => setCpUrl(e.target.value)}
            placeholder="https://<control-plane-host>"
          />
        </div>
        {error && <p className="create-error">{error}</p>}
        <div className="create-actions">
          <Button
            type="submit"
            variant="primary"
            disabled={issuing}
          >
            {issuing ? t('agents.issuing') : t('agents.issueBtn')}
          </Button>
          <Button variant="ghost" type="button" onClick={onClose}>
            {t('agents.cancel')}
          </Button>
        </div>
      </form>

      {tok && (
        <div className="join-result">
          <div className="join-token-list">
            <div className="join-token-row">
              <span className="join-token-label">{t('agents.tokenLabel')}</span>
              <code className="join-token-code">{tok.token}</code>
              <span className="join-token-expires">
                {t('agents.expires')}: {tok.expires_at}
              </span>
            </div>
          </div>
          <p className="create-hint">{t('agents.burnHint')}</p>
          <p className="create-hint">{t('agents.joinCmdHint')}</p>
          <div className="join-cmd">
            <CodeBlock code={joinCmd()} />
            <Button variant="ghost" onClick={handleCopy}>
              {copied ? t('agents.copied') : t('agents.copyBtn')}
            </Button>
          </div>
          <p className="create-hint">{t('agents.joinGuide')}</p>
        </div>
      )}
    </Modal>
  )
}

export default function Agents() {
  const { t } = useI18n()
  const [agents, setAgents] = useState([])
  const [loading, setLoading] = useState(true)
  const [live, setLive] = useState(true)
  const [error, setError] = useState(null)
  const [showJoin, setShowJoin] = useState(false)
  const [editAgent, setEditAgent] = useState(null)

  // ===== 页面介绍弹层 =====
  const [guideOpen, setGuideOpen] = useState(false)
  const toggleGuide = () => setGuideOpen(!guideOpen)

  const fetch = async (liveMode) => {
    setLoading(true)
    setError(null)
    try {
      const data = await getAgents(liveMode)
      setAgents(Array.isArray(data) ? data : [])
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetch(live)
  }, [live])

  const closeForms = () => {
    setShowJoin(false)
    setEditAgent(null)
  }

  const handleSaved = async () => {
    closeForms()
    await fetch(live)
  }

  const healthy = agents.filter((a) => a.health === 'ok').length

  return (
    <div>
      <div className="page-header">
        <h2 className="page-title">{t('agents.title')}</h2>
        <div className="agents-toolbar">
          <span className="agents-meta">
            {live
              ? t('agents.count', { count: agents.length, healthy })
              : t('agents.countStatic', { count: agents.length })}
          </span>
          <Button variant="primary" onClick={() => setShowJoin(!showJoin)}>
            {t('agents.joinBtn')}
          </Button>
          <Button
            variant={live ? 'secondary' : 'ghost'}
            onClick={() => setLive(!live)}
          >
            {t('agents.live')}: {live ? t('agents.on') : t('agents.off')}
          </Button>
          <Button variant="ghost" onClick={toggleGuide}>
            {t('agents.guide.btn')}
          </Button>
        </div>
      </div>

      {showJoin && <JoinAgentModal onClose={() => setShowJoin(false)} />}
      {editAgent && (
        <div className="agent-modal-overlay" onClick={closeForms}>
          <div className="agent-modal" onClick={(e) => e.stopPropagation()}>
            <AgentForm
              agentId={editAgent.id}
              initial={editAgent}
              onClose={closeForms}
              onSaved={handleSaved}
            />
          </div>
        </div>
      )}

      {error && <p className="page-error">{error}</p>}

      {loading ? (
        <p className="page-loading">{t('common.loading')}</p>
      ) : agents.length === 0 ? (
        <EmptyState message={t('agents.noAgents')} />
      ) : (
        <div className="agents-grid">
          {agents.map((agent) => (
            <Link key={agent.id} to={`/agents/${agent.id}`} className="agent-card-link">
              <AgentCard agent={agent} t={t} onEdit={(a) => { setEditAgent(a); setShowJoin(false) }} />
            </Link>
          ))}
        </div>
      )}

      {guideOpen && (
        <div className="guide-overlay" onClick={toggleGuide}>
          <div className="guide-panel" onClick={(e) => e.stopPropagation()}>
            <div className="guide-panel-title">{t('agents.guide.title')}</div>
            {[
              ['toolbarTitle', 'toolbarBody'],
              ['cardTitle', 'cardBody'],
              ['rowTitle', 'rowBody'],
            ].map(([titleKey, bodyKey]) => (
              <div className="guide-section" key={titleKey}>
                <div className="guide-section-title">{t(`agents.guide.${titleKey}`)}</div>
                <p className="guide-section-body">{t(`agents.guide.${bodyKey}`)}</p>
              </div>
            ))}
            <div className="guide-actions">
              <Button variant="primary" onClick={toggleGuide}>
                {t('agents.guide.close')}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
