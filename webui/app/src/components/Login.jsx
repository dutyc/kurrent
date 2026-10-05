/* 登录页：
   - 无证书浏览器：粘贴管理 token（kurrent token create --usage admin）→
     POST /session 换 HttpOnly 会话 Cookie
   - 已导入管理证书的浏览器不会看到本页（mTLS 自动认证，直接进入） */
import { useState } from 'react'
import { useAuth } from '../auth'
import { useI18n } from '../i18n'
import './Login.css'

export default function Login() {
  const { t, locale, setLocale } = useI18n()
  const { signIn } = useAuth()
  const [token, setToken] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e.preventDefault()
    if (!token.trim() || busy) return
    setBusy(true)
    setError('')
    try {
      await signIn(token.trim())
    } catch (err) {
      setError(err.status === 401 ? t('auth.invalid') : err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={submit}>
        <div className="login-brand">{t('nav.brand')}</div>
        <div className="login-subtitle">{t('auth.subtitle')}</div>

        <label className="login-label" htmlFor="admin-token">
          {t('auth.tokenLabel')}
        </label>
        <input
          id="admin-token"
          className="login-input"
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder={t('auth.tokenPlaceholder')}
          autoComplete="off"
          autoFocus
        />
        {error && <div className="login-error">{error}</div>}

        <button className="login-submit" type="submit" disabled={busy || !token.trim()}>
          {busy ? t('auth.checking') : t('auth.signIn')}
        </button>

        <p className="login-hint">{t('auth.certHint')}</p>

        <div className="login-foot">
          <button
            type="button"
            className={`login-lang${locale === 'zh-CN' ? ' login-lang-active' : ''}`}
            onClick={() => setLocale('zh-CN')}
          >
            中
          </button>
          <span className="login-lang-sep">/</span>
          <button
            type="button"
            className={`login-lang${locale === 'en-US' ? ' login-lang-active' : ''}`}
            onClick={() => setLocale('en-US')}
          >
            EN
          </button>
        </div>
      </form>
    </div>
  )
}
