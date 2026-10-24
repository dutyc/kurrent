import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  getAgents,
  getComponentCerts,
  getControlCerts,
  getSessions,
  getTokens,
  getUserCerts,
  issueAdminToken,
  issueBootstrapToken,
  revokeComponentCert,
  revokeSession,
  revokeToken,
  revokeUserCert,
} from '../api/client'
import {
  groupComponentCerts,
  groupControlCerts,
  groupUserCerts,
  revokedCertCount,
} from '../api/certs'
import {
  agentLabels,
  newNodeId,
  nodeName,
  splitSessions,
  splitTokens,
  tokenPurpose,
} from '../api/identity'
import { useAuth } from '../auth'
import { useI18n } from '../i18n'
import Badge from '../components/Badge'
import Button from '../components/Button'
import CodeBlock from '../components/CodeBlock'
import EmptyState from '../components/EmptyState'
import Input from '../components/Input'
import Modal from '../components/Modal'
import Select from '../components/Select'
import Toast, { ToastStack } from '../components/Toast'
import './Identity.css'

const TABS = ['users', 'nodes', 'control', 'automation']

const STATUS_KEY = {
  ok: 'identity.statusOk',
  partial: 'identity.statusPartial',
  revoked: 'identity.statusRevoked',
  expired: 'identity.statusExpired',
}

const STATUS_VARIANT = {
  ok: 'ok',
  partial: 'partial',
  revoked: 'error',
  expired: 'default',
}

const REASONS = [
  { value: 'unspecified', key: 'identity.reasonUnspecified' },
  { value: 'key_compromise', key: 'identity.reasonKeyCompromise' },
  { value: 'superseded', key: 'identity.reasonSuperseded' },
  { value: 'cessation_of_operation', key: 'identity.reasonCessation' },
]

const SHORT_REASON_KEY = {
  key_compromise: 'identity.reasonShortKeyCompromise',
  superseded: 'identity.reasonShortSuperseded',
  cessation_of_operation: 'identity.reasonShortCessation',
  unspecified: 'identity.reasonShortUnspecified',
}

const PURPOSE_KEY = {
  admin: 'identity.tokenPurposeAdmin',
  readonly: 'identity.tokenPurposeReadonly',
  enroll: 'identity.tokenPurposeEnroll',
  bootstrap: 'identity.tokenBootstrap',
  derived: 'identity.tokenDerived',
}

const formatDate = (ts) => {
  if (!ts) return '—'
  const date = new Date(ts)
  return Number.isNaN(date.getTime()) ? '—' : date.toISOString().slice(0, 10)
}

const formatTs = (ts) => {
  if (!ts) return '—'
  const date = new Date(ts)
  return Number.isNaN(date.getTime()) ? String(ts) : date.toLocaleString()
}

const joinCommand = (cpUrl, token, agentId) =>
  `kurrent join ${String(cpUrl || '').trim().replace(/\/+$/, '')} --token ${token} --agent-id ${agentId}`

/* 身份与访问：任务入口 + 按主体分栏的证书/令牌/会话台账。
   节点以声明里的 remark 显示，机器身份（agent_id）与令牌 ID 只作次级信息；
   接入新节点时身份由系统生成，join 命令直接携带，不让人手工输入名称。 */
export default function Identity() {
  const { t } = useI18n()
  const { identity } = useAuth()

  const [tab, setTab] = useState('users')
  const [users, setUsers] = useState([])
  const [nodes, setNodes] = useState([])
  const [control, setControl] = useState([])
  const [tokens, setTokens] = useState([])
  const [sessions, setSessions] = useState([])
  const [agents, setAgents] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [toast, setToast] = useState(null)
  const [copiedKey, setCopiedKey] = useState('')

  const [revokeTarget, setRevokeTarget] = useState(null)
  const [reason, setReason] = useState('unspecified')
  const [revoking, setRevoking] = useState(false)

  const [recoverTarget, setRecoverTarget] = useState(null)
  const [recoverCpUrl, setRecoverCpUrl] = useState('')
  const [recoverIssued, setRecoverIssued] = useState(null)
  const [recoverIssuing, setRecoverIssuing] = useState(false)

  const [addNodeOpen, setAddNodeOpen] = useState(false)
  const [addNodeId, setAddNodeId] = useState('')
  const [addNodeCpUrl, setAddNodeCpUrl] = useState('')
  const [addNodeIssued, setAddNodeIssued] = useState(null)
  const [addNodeIssuing, setAddNodeIssuing] = useState(false)

  const [tokenTarget, setTokenTarget] = useState(null)
  const [revokingToken, setRevokingToken] = useState(false)
  const [sessionTarget, setSessionTarget] = useState(null)
  const [revokingSession, setRevokingSession] = useState(false)

  const [mgmtOpen, setMgmtOpen] = useState(false)
  const [mgmtIssued, setMgmtIssued] = useState(null)
  const [mgmtIssuing, setMgmtIssuing] = useState(false)

  const tabsRef = useRef(null)
  const firstTabRef = useRef(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [userCerts, componentCerts, controlCerts, tokenList] = await Promise.all([
        getUserCerts(),
        getComponentCerts(),
        getControlCerts(),
        getTokens(),
      ])
      setUsers(Array.isArray(userCerts) ? userCerts : [])
      setNodes(Array.isArray(componentCerts) ? componentCerts : [])
      setControl(Array.isArray(controlCerts) ? controlCerts : [])
      setTokens(Array.isArray(tokenList) ? tokenList : [])
      setSessions(await getSessions().catch(() => []))
      setAgents(await getAgents(false).catch(() => []))
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const userRows = useMemo(() => groupUserCerts(users), [users])
  const nodeRows = useMemo(() => groupComponentCerts(nodes), [nodes])
  const controlRows = useMemo(() => groupControlCerts(control), [control])
  const labels = useMemo(() => agentLabels(agents), [agents])
  const { management: managementTokens, node: nodeTokens } = useMemo(
    () => splitTokens(tokens),
    [tokens]
  )
  const { user: userSessions, automation: automationSessions } = useMemo(
    () => splitSessions(sessions),
    [sessions]
  )
  const counts = useMemo(
    () => ({
      accounts: userRows.filter((row) => row.activeCount > 0).length,
      nodes: nodeRows.filter((row) => row.activeCount > 0 && labels.has(row.agentId)).length,
      tokens: managementTokens.length + nodeTokens.length,
      revoked: revokedCertCount(users, nodes, control),
    }),
    [userRows, nodeRows, labels, managementTokens, nodeTokens, users, nodes, control]
  )

  const refresh = async () => {
    await load()
    setToast({ tone: 'ok', message: t('identity.refreshed') })
  }

  const copy = async (key, text) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopiedKey(key)
      window.setTimeout(() => {
        setCopiedKey((current) => (current === key ? '' : current))
      }, 2000)
    } catch {
      setToast({ tone: 'error', message: t('identity.copyFailed') })
    }
  }

  const accessLabel = (groups) =>
    groups.includes('admin') ? t('identity.accessFull') : t('identity.accessReadonly')

  const reasonLabel = (reason) =>
    SHORT_REASON_KEY[reason] ? t(SHORT_REASON_KEY[reason]) : reason

  const countLabel = (n, key) => t(`identity.${key}${n === 1 ? 'One' : ''}`)

  const statusBadge = (row) => (
    <span className="ident-status">
      <Badge variant={STATUS_VARIANT[row.status]}>{t(STATUS_KEY[row.status])}</Badge>
      {row.reason && (
        <span className="ident-status-sub">
          {row.status === 'partial'
            ? t('identity.partialReason', {
                n: row.total - row.activeCount,
                reason: reasonLabel(row.reason),
              })
            : reasonLabel(row.reason)}
        </span>
      )}
    </span>
  )

  const selectTab = (next) => {
    setTab(next)
    window.requestAnimationFrame(() => {
      document.getElementById(`ident-tab-${next}`)?.focus()
    })
  }

  const onTabKeyDown = (event, index) => {
    if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return
    event.preventDefault()
    const delta = event.key === 'ArrowRight' ? 1 : -1
    selectTab(TABS[(index + delta + TABS.length) % TABS.length])
  }

  const scrollToLedger = () => {
    tabsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    firstTabRef.current?.focus()
  }

  const openUserRevoke = (row) => {
    setReason('unspecified')
    setRevokeTarget({
      domain: 'users',
      name: row.cn,
      display: row.cn,
      row,
    })
  }

  const openNodeRevoke = (row) => {
    setReason('unspecified')
    const display = nodeName(row.agentId, labels)
    setRevokeTarget({
      domain: 'nodes',
      name: row.agentId,
      display,
      row,
    })
  }

  const confirmRevoke = async () => {
    if (!revokeTarget) return
    setRevoking(true)
    try {
      if (revokeTarget.domain === 'users') {
        await revokeUserCert(revokeTarget.name, reason)
      } else {
        await revokeComponentCert(revokeTarget.name, reason)
      }
      setToast({
        tone: 'ok',
        message: t('identity.revokedToast', { name: revokeTarget.display }),
      })
      setRevokeTarget(null)
      await load()
    } catch (e) {
      setToast({ tone: 'error', message: e.message })
    } finally {
      setRevoking(false)
    }
  }

  const openRecovery = (row) => {
    setRecoverTarget(row)
    setRecoverCpUrl(window.location.origin)
    setRecoverIssued(null)
    setCopiedKey('')
  }

  const issueRecoveryToken = async () => {
    if (!recoverTarget) return
    setRecoverIssuing(true)
    try {
      setRecoverIssued(await issueBootstrapToken(recoverTarget.agentId))
      await load()
    } catch (e) {
      setToast({ tone: 'error', message: e.message })
    } finally {
      setRecoverIssuing(false)
    }
  }

  const finishRecovery = async () => {
    setRecoverTarget(null)
    setRecoverIssued(null)
    await refresh()
  }

  const openAddNode = () => {
    let id
    try {
      id = newNodeId()
    } catch {
      setToast({ tone: 'error', message: t('identity.identityGenerationFailed') })
      return
    }
    setAddNodeId(id)
    setAddNodeCpUrl(window.location.origin)
    setAddNodeIssued(null)
    setCopiedKey('')
    setAddNodeOpen(true)
  }

  const issueAddNodeToken = async () => {
    if (!addNodeCpUrl.trim()) {
      setToast({ tone: 'error', message: t('identity.cpUrlRequired') })
      return
    }
    setAddNodeIssuing(true)
    try {
      setAddNodeIssued(await issueBootstrapToken(addNodeId))
      await load()
    } catch (e) {
      setToast({ tone: 'error', message: e.message })
    } finally {
      setAddNodeIssuing(false)
    }
  }

  const openTokenRevoke = (token, label) => {
    setTokenTarget({ token, label })
  }

  const confirmTokenRevoke = async () => {
    if (!tokenTarget) return
    setRevokingToken(true)
    try {
      await revokeToken(tokenTarget.token.token_id)
      setToast({ tone: 'ok', message: t('identity.tokenRevokedToast', { name: tokenTarget.label }) })
      setTokenTarget(null)
      await load()
    } catch (e) {
      setToast({ tone: 'error', message: e.message })
    } finally {
      setRevokingToken(false)
    }
  }

  const openSessionRevoke = (session) => {
    setSessionTarget(session)
  }

  const confirmSessionRevoke = async () => {
    if (!sessionTarget) return
    setRevokingSession(true)
    try {
      await revokeSession(sessionTarget.session_id)
      setToast({ tone: 'ok', message: t('identity.sessionRevokedToast', { name: sessionTarget.user }) })
      setSessionTarget(null)
      await load()
    } catch (e) {
      setToast({ tone: 'error', message: e.message })
    } finally {
      setRevokingSession(false)
    }
  }

  const openManagement = () => {
    setMgmtIssued(null)
    setCopiedKey('')
    setMgmtOpen(true)
  }

  const issueManagementToken = async () => {
    setMgmtIssuing(true)
    try {
      setMgmtIssued(await issueAdminToken())
      await load()
    } catch (e) {
      setToast({ tone: 'error', message: e.message })
    } finally {
      setMgmtIssuing(false)
    }
  }

  const recoverCommand =
    recoverTarget && recoverIssued
      ? joinCommand(recoverCpUrl, recoverIssued.token, recoverTarget.agentId)
      : ''
  const addNodeCommand = addNodeIssued
    ? joinCommand(addNodeCpUrl, addNodeIssued.token, addNodeId)
    : ''

  const showInitialLoading =
    loading && users.length === 0 && nodes.length === 0 && control.length === 0 && tokens.length === 0

  return (
    <div>
      <div className="ident-header">
        <h2 className="page-title">{t('identity.title')}</h2>
        <Button variant="ghost" onClick={refresh} disabled={loading}>
          {t('identity.refresh')}
        </Button>
      </div>
      <p className="ident-subtitle">{t('identity.subtitle')}</p>

      <div className="ident-tasks">
        <button type="button" className="ident-task" onClick={openAddNode}>
          <span className="ident-task-title">{t('identity.taskAddNode')}</span>
          <span className="ident-task-desc">{t('identity.taskAddNodeDesc')}</span>
          <span className="ident-task-arrow" aria-hidden="true">→</span>
        </button>
        <button type="button" className="ident-task" onClick={openManagement}>
          <span className="ident-task-title">{t('identity.taskIssueToken')}</span>
          <span className="ident-task-desc">{t('identity.taskIssueTokenDesc')}</span>
          <span className="ident-task-arrow" aria-hidden="true">→</span>
        </button>
        <button type="button" className="ident-task" onClick={scrollToLedger}>
          <span className="ident-task-title">{t('identity.taskRevoke')}</span>
          <span className="ident-task-desc">{t('identity.taskRevokeDesc')}</span>
          <span className="ident-task-arrow down" aria-hidden="true">↓</span>
        </button>
      </div>

      <div className="ident-statusline">
        <span><b>{counts.accounts}</b> {countLabel(counts.accounts, 'accountsSuffix')}</span>
        <span className="ident-sep">·</span>
        <span><b>{counts.nodes}</b> {countLabel(counts.nodes, 'nodesSuffix')}</span>
        <span className="ident-sep">·</span>
        <span><b>{counts.tokens}</b> {countLabel(counts.tokens, 'tokensSuffix')}</span>
        <span className="ident-sep">·</span>
        <span><b>{counts.revoked}</b> {countLabel(counts.revoked, 'revokedSuffix')}</span>
      </div>

      {error && <p className="page-error">{error}</p>}

      {showInitialLoading ? (
        <p className="page-loading">{t('common.loading')}</p>
      ) : (
        <>
          <div className="ident-tabs" role="tablist" ref={tabsRef}>
            {TABS.map((key, index) => {
              const count = {
                users: userRows.length,
                nodes: nodeRows.length,
                control: controlRows.length,
                automation: managementTokens.length,
              }[key]
              return (
                <button
                  key={key}
                  id={`ident-tab-${key}`}
                  ref={key === 'users' ? firstTabRef : undefined}
                  type="button"
                  role="tab"
                  aria-selected={tab === key}
                  aria-controls={`ident-panel-${key}`}
                  tabIndex={tab === key ? 0 : -1}
                  className={`ident-tab${tab === key ? ' active' : ''}`}
                  onClick={() => setTab(key)}
                  onKeyDown={(event) => onTabKeyDown(event, index)}
                >
                  {t(`identity.tab${key[0].toUpperCase()}${key.slice(1)}`)}
                  <span className="ident-tab-count">{count}</span>
                </button>
              )
            })}
          </div>

          <section
            id="ident-panel-users"
            role="tabpanel"
            aria-labelledby="ident-tab-users"
            hidden={tab !== 'users'}
          >
            <div className="ident-group">
              <div className="ident-group-head">
                <span className="ident-group-title">{t('identity.usersCertsTitle')}</span>
                <span className="ident-group-count">{userRows.length}</span>
                <span className="ident-group-note">{t('identity.usersCertsNote')}</span>
              </div>
              {userRows.length === 0 ? (
                <EmptyState message={t('identity.emptyUsers')} />
              ) : (
                <div className="ident-table">
                  <div className="ident-row ident-row-users ident-row-head">
                    <span>{t('identity.colUser')}</span>
                    <span>{t('identity.colGroups')}</span>
                    <span>{t('identity.colAccess')}</span>
                    <span>{t('identity.colExpires')}</span>
                    <span>{t('identity.colStatus')}</span>
                    <span>{t('identity.colActions')}</span>
                  </div>
                  {userRows.map((row) => {
                    const self = identity?.name === row.cn
                    return (
                      <div key={row.cn} className="ident-row ident-row-users">
                        <span className="ident-name-block">
                          <span className="ident-name">{row.cn}</span>
                          <span className="ident-cell-sub">
                            {self
                              ? t('identity.currentLogin')
                              : t('identity.issuedAt', { date: formatDate(row.certs[0]?.created_at) })}
                          </span>
                        </span>
                        <span>{row.groups.join(', ') || '—'}</span>
                        <span>{accessLabel(row.groups)}</span>
                        <span className="ident-date">{formatDate(row.expiresAt)}</span>
                        {statusBadge(row)}
                        <span className="ident-actions">
                          {self ? (
                            <span className="ident-muted" title={t('identity.currentCertTitle')}>
                              {t('identity.currentCert')}
                            </span>
                          ) : row.activeCount > 0 ? (
                            <Button variant="danger" onClick={() => openUserRevoke(row)}>
                              {t('identity.revoke')}
                            </Button>
                          ) : (
                            <span className="ident-muted">—</span>
                          )}
                        </span>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>

            <div className="ident-group">
              <div className="ident-group-head">
                <span className="ident-group-title">{t('identity.sessionsTitle')}</span>
                <span className="ident-group-count">{userSessions.length}</span>
                <span className="ident-group-note">{t('identity.sessionsNote')}</span>
              </div>
              {userSessions.length === 0 ? (
                <EmptyState message={t('identity.emptySessions')} />
              ) : (
                <div className="ident-table">
                  <div className="ident-row ident-row-sessions ident-row-head">
                    <span>{t('identity.colUser')}</span>
                    <span>{t('identity.colLogin')}</span>
                    <span>{t('identity.colLastSeen')}</span>
                    <span>{t('identity.colExpires')}</span>
                    <span>{t('identity.colActions')}</span>
                  </div>
                  {userSessions.map((session) => (
                    <div key={session.session_id} className="ident-row ident-row-sessions">
                      <span className="ident-name-block">
                        <span className="ident-name">{session.user}</span>
                        <span className="ident-cell-sub">{t('identity.sessionWebUi')}</span>
                      </span>
                      <span className="ident-date">{formatTs(session.created_at)}</span>
                      <span className="ident-date">{formatTs(session.last_seen)}</span>
                      <span className="ident-date">{formatTs(session.expires_at)}</span>
                      <span className="ident-actions">
                        <Button variant="danger" onClick={() => openSessionRevoke(session)}>
                          {t('identity.revoke')}
                        </Button>
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </section>

          <section
            id="ident-panel-nodes"
            role="tabpanel"
            aria-labelledby="ident-tab-nodes"
            hidden={tab !== 'nodes'}
          >
            <div className="ident-group">
              <div className="ident-group-head">
                <span className="ident-group-title">{t('identity.nodeCertsTitle')}</span>
                <span className="ident-group-count">{nodeRows.length}</span>
                <span className="ident-group-note">{t('identity.nodeCertsNote')}</span>
              </div>
              {nodeRows.length === 0 ? (
                <EmptyState message={t('identity.emptyNodes')} />
              ) : (
                <div className="ident-table">
                  <div className="ident-row ident-row-nodes ident-row-head">
                    <span>{t('identity.colNode')}</span>
                    <span>{t('identity.colPurpose')}</span>
                    <span>{t('identity.colExpires')}</span>
                    <span>{t('identity.colStatus')}</span>
                    <span>{t('identity.colActions')}</span>
                  </div>
                  {nodeRows.map((row) => {
                    const label = nodeName(row.agentId, labels)
                    const registered = labels.has(row.agentId)
                    const sub = [
                      label === row.agentId ? null : row.agentId,
                      t('identity.nodeSubtitle', { n: row.total }),
                      registered ? null : t('identity.unregisteredNode'),
                    ].filter(Boolean).join(' · ')
                    return (
                      <div key={row.agentId} className="ident-row ident-row-nodes">
                        <span className="ident-name-block">
                          <span className={label === row.agentId ? 'ident-name ident-id' : 'ident-name'}>
                            {label}
                          </span>
                          <span className="ident-cell-sub">{sub}</span>
                        </span>
                        <span>{t('identity.nodePurpose')}</span>
                        <span className="ident-date">{formatDate(row.expiresAt)}</span>
                        {statusBadge(row)}
                        <span className="ident-actions">
                          {row.activeCount > 0 && (
                            <Button variant="danger" onClick={() => openNodeRevoke(row)}>
                              {t('identity.revoke')}
                            </Button>
                          )}
                          {row.status !== 'ok' && (
                            <Button variant="ghost" onClick={() => openRecovery(row)}>
                              {t('identity.recover')}
                            </Button>
                          )}
                        </span>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>

            <div className="ident-group">
              <div className="ident-group-head">
                <span className="ident-group-title">{t('identity.nodeTokensTitle')}</span>
                <span className="ident-group-count">{nodeTokens.length}</span>
                <span className="ident-group-note">{t('identity.nodeTokensNote')}</span>
              </div>
              {nodeTokens.length === 0 ? (
                <EmptyState message={t('identity.emptyNodeTokens')} />
              ) : (
                <div className="ident-table">
                  <div className="ident-row ident-row-node-tokens ident-row-head">
                    <span>{t('identity.colNode')}</span>
                    <span>{t('identity.colPurpose')}</span>
                    <span>{t('identity.colExpires')}</span>
                    <span>{t('identity.colLastUsed')}</span>
                    <span>{t('identity.colActions')}</span>
                  </div>
                  {nodeTokens.map((token) => {
                    const purpose = tokenPurpose(token)
                    const name = nodeName(token.agent_id, labels)
                    const sub = [
                      name === token.agent_id ? null : token.agent_id,
                      token.token_id,
                    ].filter(Boolean).join(' · ')
                    return (
                      <div key={token.token_id} className="ident-row ident-row-node-tokens">
                        <span className="ident-name-block">
                          <span className={name === token.agent_id ? 'ident-name ident-id' : 'ident-name'}>
                            {name}
                          </span>
                          <span className="ident-cell-sub">{sub}</span>
                        </span>
                        <span>{t(PURPOSE_KEY[purpose])}</span>
                        <span className="ident-date">{formatTs(token.expires_at)}</span>
                        <span className="ident-date">{formatTs(token.last_used_at)}</span>
                        <span className="ident-actions">
                          <Button
                            variant="danger"
                            onClick={() => openTokenRevoke(token, `${name} · ${t(PURPOSE_KEY[purpose])}`)}
                          >
                            {t('identity.revoke')}
                          </Button>
                        </span>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          </section>

          <section
            id="ident-panel-control"
            role="tabpanel"
            aria-labelledby="ident-tab-control"
            hidden={tab !== 'control'}
          >
            <div className="ident-group">
              <div className="ident-group-head">
                <span className="ident-group-title">{t('identity.controlTitle')}</span>
                <span className="ident-group-count">{controlRows.length}</span>
                <span className="ident-group-note">{t('identity.controlNote')}</span>
              </div>
              {controlRows.length === 0 ? (
                <EmptyState message={t('identity.emptyControl')} />
              ) : (
                <div className="ident-table">
                  <div className="ident-row ident-row-control ident-row-head">
                    <span>{t('identity.colIdentity')}</span>
                    <span>{t('identity.colPurpose')}</span>
                    <span>{t('identity.colExpires')}</span>
                    <span>{t('identity.colStatus')}</span>
                    <span>{t('identity.colActions')}</span>
                  </div>
                  {controlRows.map((row) => (
                    <div key={row.cn} className="ident-row ident-row-control">
                      <span className="ident-name-block">
                        <span className="ident-name">{row.cn}</span>
                        {row.status === 'ok' && (
                          <span className="ident-cell-sub">{t('identity.currentCert')}</span>
                        )}
                      </span>
                      <span>{t('identity.controlPurpose')}</span>
                      <span className="ident-date">{formatDate(row.expiresAt)}</span>
                      {statusBadge(row)}
                      <span>
                        <span className="ident-cli" title={t('identity.cliOnlyTitle')}>
                          {t('identity.cliOnly')}
                        </span>
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </section>

          <section
            id="ident-panel-automation"
            role="tabpanel"
            aria-labelledby="ident-tab-automation"
            hidden={tab !== 'automation'}
          >
            <div className="ident-group">
              <div className="ident-group-head">
                <span className="ident-group-title">{t('identity.autoTokensTitle')}</span>
                <span className="ident-group-count">{managementTokens.length}</span>
                <span className="ident-group-note">{t('identity.autoTokensNote')}</span>
              </div>
              {managementTokens.length === 0 ? (
                <EmptyState message={t('identity.emptyAutoTokens')} />
              ) : (
                <div className="ident-table">
                  <div className="ident-row ident-row-auto-tokens ident-row-head">
                    <span>{t('identity.colToken')}</span>
                    <span>{t('identity.colPurpose')}</span>
                    <span>{t('identity.colExpires')}</span>
                    <span>{t('identity.colLastUsed')}</span>
                    <span>{t('identity.colActions')}</span>
                  </div>
                  {managementTokens.map((token) => {
                    const purpose = tokenPurpose(token)
                    return (
                      <div key={token.token_id} className="ident-row ident-row-auto-tokens">
                        <span className="ident-name-block">
                          <span className="ident-name">{token.token_id}</span>
                          <span className="ident-cell-sub">
                            token:{token.token_id} · {(token.groups || []).join(', ') || '—'}
                          </span>
                        </span>
                        <span>{t(PURPOSE_KEY[purpose])}</span>
                        <span className="ident-date">{formatTs(token.expires_at)}</span>
                        <span className="ident-date">{formatTs(token.last_used_at)}</span>
                        <span className="ident-actions">
                          <Button
                            variant="danger"
                            onClick={() => openTokenRevoke(token, `token:${token.token_id}`)}
                          >
                            {t('identity.revoke')}
                          </Button>
                        </span>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>

            <div className="ident-group">
              <div className="ident-group-head">
                <span className="ident-group-title">{t('identity.autoSessionsTitle')}</span>
                <span className="ident-group-count">{automationSessions.length}</span>
                <span className="ident-group-note">{t('identity.autoSessionsNote')}</span>
              </div>
              {automationSessions.length === 0 ? (
                <EmptyState message={t('identity.emptyAutoSessions')} />
              ) : (
                <div className="ident-table">
                  <div className="ident-row ident-row-sessions ident-row-head">
                    <span>{t('identity.colSubject')}</span>
                    <span>{t('identity.colLogin')}</span>
                    <span>{t('identity.colLastSeen')}</span>
                    <span>{t('identity.colExpires')}</span>
                    <span>{t('identity.colActions')}</span>
                  </div>
                  {automationSessions.map((session) => (
                    <div key={session.session_id} className="ident-row ident-row-sessions">
                      <span className="ident-name-block">
                        <span className="ident-name">{session.user}</span>
                        <span className="ident-cell-sub">{t('identity.sessionAutomation')}</span>
                      </span>
                      <span className="ident-date">{formatTs(session.created_at)}</span>
                      <span className="ident-date">{formatTs(session.last_seen)}</span>
                      <span className="ident-date">{formatTs(session.expires_at)}</span>
                      <span className="ident-actions">
                        <Button variant="danger" onClick={() => openSessionRevoke(session)}>
                          {t('identity.revoke')}
                        </Button>
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </section>
        </>
      )}

      {revokeTarget && (
        <Modal
          title={t('identity.revokeCertTitle')}
          onClose={() => setRevokeTarget(null)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setRevokeTarget(null)} disabled={revoking}>
                {t('identity.cancel')}
              </Button>
              <Button variant="danger" onClick={confirmRevoke} disabled={revoking}>
                {revoking ? t('identity.revoking') : t('identity.confirmRevoke')}
              </Button>
            </>
          }
        >
          <dl className="ident-target">
            <dt>{revokeTarget.domain === 'users' ? t('identity.targetUser') : t('identity.targetNode')}</dt>
            <dd>{revokeTarget.display}</dd>
            <dt>{t('identity.targetImpact')}</dt>
            <dd>
              {revokeTarget.domain === 'users'
                ? t('identity.targetAccessUsers')
                : t('identity.targetAccessNodes')}
            </dd>
            <dt>{t('identity.targetSerials')}</dt>
            <dd>{revokeTarget.row.serials.join(' · ') || '—'}</dd>
            <dt>{t('identity.targetIssuer')}</dt>
            <dd>
              {revokeTarget.domain === 'users'
                ? t('identity.issuerUsers')
                : t('identity.issuerNodes')}
            </dd>
          </dl>
          <p className="ident-consequence">
            {revokeTarget.domain === 'users'
              ? t('identity.impactUsers')
              : t('identity.impactNodes')}
          </p>
          <Select
            label={t('identity.reason')}
            name="ident-revoke-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            options={REASONS.map((item) => ({ value: item.value, label: t(item.key) }))}
          />
        </Modal>
      )}

      {recoverTarget && (
        <Modal
          title={t('identity.recoverTitle')}
          width="580px"
          onClose={() => setRecoverTarget(null)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setRecoverTarget(null)}>
                {t('identity.close')}
              </Button>
              <Button
                variant="primary"
                onClick={finishRecovery}
                disabled={!recoverIssued || loading}
              >
                {t('identity.refreshState')}
              </Button>
            </>
          }
        >
          <dl className="ident-target">
            <dt>{t('identity.targetNode')}</dt>
            <dd>
              {nodeName(recoverTarget.agentId, labels) === recoverTarget.agentId
                ? recoverTarget.agentId
                : `${nodeName(recoverTarget.agentId, labels)} · ${recoverTarget.agentId}`}
            </dd>
          </dl>
          <Input
            label={t('identity.cpUrl')}
            name="ident-recover-cp-url"
            value={recoverCpUrl}
            onChange={(event) => setRecoverCpUrl(event.target.value)}
          />
          <div className="ident-modal-action">
            <Button variant="primary" onClick={issueRecoveryToken} disabled={recoverIssuing}>
              {recoverIssuing
                ? t('identity.issuingToken')
                : recoverIssued
                  ? t('identity.reissueNodeToken')
                  : t('identity.issueNodeToken')}
            </Button>
          </div>
          {recoverIssued && (
            <>
              <div className="ident-recover-block">
                <div className="ident-recover-label">{t('identity.tokenLabel')}</div>
                <code className="ident-token">{recoverIssued.token}</code>
                <p className="ident-hint">
                  {t('identity.tokenExpires', { expires: formatTs(recoverIssued.expires_at) })}
                </p>
              </div>
              <div className="ident-recover-block">
                <div className="ident-recover-label">{t('identity.commandLabel')}</div>
                <CodeBlock code={recoverCommand} />
                <div className="ident-recover-actions">
                  <Button variant="ghost" onClick={() => copy('recover', recoverCommand)}>
                    {copiedKey === 'recover' ? t('identity.copied') : t('identity.copy')}
                  </Button>
                </div>
                <p className="ident-hint">{t('identity.recoverHint')}</p>
              </div>
            </>
          )}
        </Modal>
      )}

      {addNodeOpen && (
        <Modal
          title={t('identity.addNodeTitle')}
          width="580px"
          onClose={() => setAddNodeOpen(false)}
          footer={
            <Button variant="ghost" onClick={() => setAddNodeOpen(false)}>
              {t('identity.close')}
            </Button>
          }
        >
          <p className="ident-hint">{t('identity.addNodeIdentityHint')}</p>
          <Input
            label={t('identity.cpUrl')}
            name="ident-add-node-cp-url"
            value={addNodeCpUrl}
            onChange={(event) => setAddNodeCpUrl(event.target.value)}
          />
          <div className="ident-modal-action">
            <Button variant="primary" onClick={issueAddNodeToken} disabled={addNodeIssuing}>
              {addNodeIssuing ? t('identity.issuingToken') : t('identity.issueNodeToken')}
            </Button>
          </div>
          {addNodeIssued && (
            <>
              <div className="ident-recover-block">
                <div className="ident-recover-label">{t('identity.tokenLabel')}</div>
                <code className="ident-token">{addNodeIssued.token}</code>
                <p className="ident-hint">
                  {t('identity.tokenExpires', { expires: formatTs(addNodeIssued.expires_at) })}
                </p>
              </div>
              <div className="ident-recover-block">
                <div className="ident-recover-label">{t('identity.commandLabel')}</div>
                <CodeBlock code={addNodeCommand} />
                <div className="ident-recover-actions">
                  <Button variant="ghost" onClick={() => copy('add-node', addNodeCommand)}>
                    {copiedKey === 'add-node' ? t('identity.copied') : t('identity.copy')}
                  </Button>
                </div>
                <p className="ident-hint">{t('identity.addNodeHint')}</p>
              </div>
            </>
          )}
        </Modal>
      )}

      {tokenTarget && (
        <Modal
          title={t('identity.tokenRevokeTitle')}
          onClose={() => setTokenTarget(null)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setTokenTarget(null)} disabled={revokingToken}>
                {t('identity.cancel')}
              </Button>
              <Button variant="danger" onClick={confirmTokenRevoke} disabled={revokingToken}>
                {revokingToken ? t('identity.revoking') : t('identity.confirmRevoke')}
              </Button>
            </>
          }
        >
          <p className="ident-consequence">
            {t('identity.tokenRevokeMessage', { name: tokenTarget.label })}
          </p>
        </Modal>
      )}

      {sessionTarget && (
        <Modal
          title={t('identity.sessionRevokeTitle')}
          onClose={() => setSessionTarget(null)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setSessionTarget(null)} disabled={revokingSession}>
                {t('identity.cancel')}
              </Button>
              <Button variant="danger" onClick={confirmSessionRevoke} disabled={revokingSession}>
                {revokingSession ? t('identity.revoking') : t('identity.confirmRevoke')}
              </Button>
            </>
          }
        >
          <p className="ident-consequence">
            {t('identity.sessionRevokeMessage', { user: sessionTarget.user })}
          </p>
        </Modal>
      )}

      {mgmtOpen && (
        <Modal
          title={t('identity.managementTitle')}
          width="560px"
          onClose={() => setMgmtOpen(false)}
          footer={
            mgmtIssued ? (
              <>
                <Button variant="ghost" onClick={() => copy('mgmt', mgmtIssued.token)}>
                  {copiedKey === 'mgmt' ? t('identity.copied') : t('identity.copy')}
                </Button>
                <Button variant="primary" onClick={() => setMgmtOpen(false)}>
                  {t('identity.saved')}
                </Button>
              </>
            ) : (
              <>
                <Button variant="ghost" onClick={() => setMgmtOpen(false)} disabled={mgmtIssuing}>
                  {t('identity.cancel')}
                </Button>
                <Button variant="primary" onClick={issueManagementToken} disabled={mgmtIssuing}>
                  {mgmtIssuing ? t('identity.generating') : t('identity.generate')}
                </Button>
              </>
            )
          }
        >
          {mgmtIssued ? (
            <div className="ident-recover-block">
              <div className="ident-recover-label">{t('identity.issuedTitle')}</div>
              <code className="ident-token">{mgmtIssued.token}</code>
              <p className="ident-hint">
                {t('identity.issuedHint', { expires: formatTs(mgmtIssued.expires_at) })}
              </p>
            </div>
          ) : (
            <p className="ident-consequence">{t('identity.managementIntro')}</p>
          )}
        </Modal>
      )}

      <ToastStack>
        {toast && (
          <Toast tone={toast.tone} message={toast.message} onClose={() => setToast(null)} />
        )}
      </ToastStack>
    </div>
  )
}
