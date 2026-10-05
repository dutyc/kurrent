import { Routes, Route } from 'react-router-dom'
import { AuthProvider, useAuth } from './auth'
import { useI18n } from './i18n'
import Layout from './components/Layout'
import Login from './components/Login'
import Dashboard from './pages/Dashboard'
import Workers from './pages/Workers'
import WorkerDetail from './pages/WorkerDetail'
import Devices from './pages/Devices'
import Agents from './pages/Agents'
import AgentLuns from './pages/AgentLuns'
import Credentials from './pages/Credentials'
import Operations from './pages/Operations'

function BootScreen() {
  const { t } = useI18n()
  return (
    <div className="login-page">
      <div className="login-card login-boot">{t('auth.checking')}</div>
    </div>
  )
}

/* 认证门：探测 → 已认证渲染应用；未认证渲染登录页。
   浏览器携带管理证书（mTLS）时探测即通过（自动认证，无登录页）。 */
function Gate() {
  const { status } = useAuth()
  if (status === 'probing') return <BootScreen />
  if (status === 'unauthed') return <Login />
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="workers" element={<Workers />} />
        <Route path="workers/:id" element={<WorkerDetail />} />
        <Route path="devices" element={<Devices />} />
        <Route path="agents" element={<Agents />} />
        <Route path="agents/:id" element={<AgentLuns />} />
        <Route path="credentials" element={<Credentials />} />
        <Route path="operations" element={<Operations />} />
      </Route>
    </Routes>
  )
}

export default function App() {
  return (
    <AuthProvider>
      <Gate />
    </AuthProvider>
  )
}
