import { useState, useEffect, useCallback } from 'react'
import {
  getSessions, getTokens, issueAdminToken, revokeAllSessions, revokeSession, revokeToken,
} from '../api/client'
import { useI18n } from '../i18n'
import Button from '../components/Button'
import EmptyState from '../components/EmptyState'
import ConfirmAction from '../components/ConfirmAction'
import './Credentials.css'

/* 凭据页：token 台账（管理 / bootstrap / nvmet-host 派生）列表 + 吊销 + 签发管理 token。
   吊销 = 删条目（立即失效）；明文只在新签时展示一次（登记只存 hash，事后不可查）。 */
export default function Credentials() {
  const { t } = useI18n()
  const [tokens, setTokens] = useState([])
  const [sessions, setSessions] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [issued, setIssued] = useState(null)
  const [issuing, setIssuing] = useState(false)
  const [copied, setCopied] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setTokens(await getTokens())
      try {
        setSessions(await getSessions())
      } catch {
        setSessions([]) // 非管理员：会话台账不可见
      }
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const handleIssue = async () => {
    setIssuing(true)
    setActionError(null)
    setCopied(false)
    try {
      setIssued(await issueAdminToken())
      await load()
    } catch (e) {
      setActionError(e.message)
    } finally {
      setIssuing(false)
    }
  }

  const handleRevoke = async (tokenId) => {
    setActionError(null)
    try {
      await revokeToken(tokenId)
      if (issued && issued.token.startsWith(`${tokenId}.`)) setIssued(null)
      await load()
    } catch (e) {
      setActionError(e.message)
    }
  }

  const handleRevokeSession = async (sessionId) => {
    setActionError(null)
    try {
      await revokeSession(sessionId)
      await load()
    } catch (e) {
      setActionError(e.message)
    }
  }

  const handleRevokeAllSessions = async () => {
    setActionError(null)
    try {
      await revokeAllSessions()
      await load()
    } catch (e) {
      setActionError(e.message)
    }
  }

  const handleCopy = async (text) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
    } catch {
      setCopied(false) // 剪贴板不可用（非安全上下文等）：保留明文供手动复制
    }
  }

  // ISO → 本地时区展示；旧条目 created_at 为空串显示占位
  const formatTs = (ts) => {
    if (!ts) return t('credentials.unknown')
    const d = new Date(ts)
    return isNaN(d.getTime()) ? String(ts) : d.toLocaleString()
  }

  return (
    <div>
      <div className="cred-header-bar">
        <h2 className="page-title">{t('credentials.title')}</h2>
        <div className="cred-actions">
          <Button variant="ghost" onClick={load} disabled={loading}>
            {t('credentials.refresh')}
          </Button>
          <Button variant="primary" onClick={handleIssue} disabled={issuing}>
            {issuing ? t('credentials.issuing') : t('credentials.issueAdmin')}
          </Button>
        </div>
      </div>
      <p className="cred-subtitle">{t('credentials.subtitle')}</p>

      {error && <p className="page-error">{error}</p>}
      {actionError && <p className="page-error">{actionError}</p>}

      {issued && (
        <div className="cred-issued">
          <div className="cred-issued-title">{t('credentials.issuedTitle')}</div>
          <div className="cred-issued-token">
            <code>{issued.token}</code>
            <Button variant="ghost" onClick={() => handleCopy(issued.token)}>
              {copied ? t('credentials.copied') : t('credentials.copy')}
            </Button>
          </div>
          <p className="cred-issued-hint">
            {t('credentials.issuedHint', { expires: formatTs(issued.expires_at) })}
          </p>
          <Button variant="ghost" onClick={() => setIssued(null)}>
            {t('credentials.issuedDismiss')}
          </Button>
        </div>
      )}

      {loading && tokens.length === 0 ? (
        <p className="page-loading">{t('common.loading')}</p>
      ) : tokens.length === 0 ? (
        <EmptyState message={t('credentials.empty')} />
      ) : (
        <div className="cred-table">
          <div className="cred-row cred-row-head">
            <span>{t('credentials.colTokenId')}</span>
            <span>{t('credentials.colUsage')}</span>
            <span>{t('credentials.colScopes')}</span>
            <span>{t('credentials.colOwner')}</span>
            <span>{t('credentials.colName')}</span>
            <span>{t('credentials.colExpires')}</span>
            <span>{t('credentials.colLastUsed')}</span>
            <span>{t('credentials.colCreated')}</span>
            <span>{t('credentials.colActions')}</span>
          </div>
          {tokens.map((tok) => (
            <div key={tok.token_id} className="cred-row">
              <span className="td-mono">{tok.token_id}</span>
              <span>{(tok.usage || []).join(', ')}</span>
              <span>{(tok.scopes || []).join(', ') || '—'}</span>
              <span className="td-mono">
                {tok.component ? (tok.agent_id ? `${tok.component}/${tok.agent_id}` : tok.component) : '—'}
              </span>
              <span>{tok.name || '—'}</span>
              <span className="cred-ts">{formatTs(tok.expires_at)}</span>
              <span className="cred-ts">{formatTs(tok.last_used_at)}</span>
              <span className="cred-ts">{formatTs(tok.created_at)}</span>
              <span>
                <ConfirmAction
                  message={t('credentials.revokeConfirm', { id: tok.token_id })}
                  onConfirm={() => handleRevoke(tok.token_id)}
                  trigger={
                    <Button variant="danger">
                      {t('credentials.revokeBtn')}
                    </Button>
                  }
                />
              </span>
            </div>
          ))}
        </div>
      )}

      <div className="cred-section-head">
        <h3 className="cred-section-title">{t('credentials.sessionsTitle')}</h3>
        <ConfirmAction
          message={t('credentials.revokeAllSessionsConfirm', { count: sessions.length })}
          onConfirm={handleRevokeAllSessions}
          trigger={
            <Button variant="ghost" disabled={sessions.length === 0}>
              {t('credentials.revokeAllSessions')}
            </Button>
          }
        />
      </div>
      <p className="cred-subtitle">{t('credentials.sessionsSubtitle')}</p>

      {sessions.length === 0 ? (
        <EmptyState message={t('credentials.sessionsEmpty')} />
      ) : (
        <div className="cred-table">
          <div className="cred-row cred-session-row cred-row-head">
            <span>{t('credentials.colSessionUser')}</span>
            <span>{t('credentials.colSessionGroups')}</span>
            <span>{t('credentials.colSessionCreated')}</span>
            <span>{t('credentials.colSessionLastSeen')}</span>
            <span>{t('credentials.colSessionExpires')}</span>
            <span>{t('credentials.colActions')}</span>
          </div>
          {sessions.map((s) => (
            <div key={s.session_id} className="cred-row cred-session-row">
              <span className="td-mono" title={s.session_id}>{s.user}</span>
              <span>{(s.groups || []).join(', ') || '—'}</span>
              <span className="cred-ts">{formatTs(s.created_at)}</span>
              <span className="cred-ts">{formatTs(s.last_seen)}</span>
              <span className="cred-ts">{formatTs(s.expires_at)}</span>
              <span>
                <ConfirmAction
                  message={t('credentials.revokeSessionConfirm', { user: s.user })}
                  onConfirm={() => handleRevokeSession(s.session_id)}
                  trigger={
                    <Button variant="danger">
                      {t('credentials.revokeBtn')}
                    </Button>
                  }
                />
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
