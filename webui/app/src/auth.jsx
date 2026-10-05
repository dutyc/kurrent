/* 认证上下文：
   - 启动探测 GET /whoami：浏览器携带管理证书（mTLS）时直接进入（自动认证）；
     无证书 → 401 → 展示登录页（管理 token 换会话 Cookie）
   - 会话失活（任何请求 401）→ 自动回登录页（setUnauthorizedHandler 全局钩子）
   - 登录态为 HttpOnly 签名会话 Cookie：前端不可读、JS 不落 token */
import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { sessionLogin, sessionLogout, setUnauthorizedHandler, whoami } from './api/client'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [status, setStatus] = useState('probing') // probing | authed | unauthed
  const [identity, setIdentity] = useState(null)

  useEffect(() => {
    let alive = true
    whoami()
      .then((id) => {
        if (!alive) return
        setIdentity(id)
        setStatus('authed')
      })
      .catch(() => {
        if (alive) setStatus('unauthed')
      })
    setUnauthorizedHandler(() => setStatus('unauthed'))
    return () => {
      alive = false
      setUnauthorizedHandler(null)
    }
  }, [])

  const signIn = useCallback(async (token) => {
    const id = await sessionLogin(token) // 失败抛错，由登录页展示
    setIdentity(id)
    setStatus('authed')
  }, [])

  const signOut = useCallback(async () => {
    try {
      await sessionLogout()
    } catch {
      // 会话已失效/网络异常也视为登出（本地状态清空）
    }
    setIdentity(null)
    setStatus('unauthed')
  }, [])

  return (
    <AuthContext.Provider value={{ status, identity, signIn, signOut }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  return useContext(AuthContext)
}
