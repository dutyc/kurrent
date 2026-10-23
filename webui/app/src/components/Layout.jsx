import { NavLink, Outlet } from 'react-router-dom'
import { useState } from 'react'
import { useAuth } from '../auth'
import { useI18n } from '../i18n'
import './Layout.css'

/* 菜单图标（内联 SVG，stroke 线条风格，与 mono 极简调性一致） */
const ICON_DASHBOARD = (
  <>
    <rect x="2" y="2" width="5" height="5" />
    <rect x="9" y="2" width="5" height="5" />
    <rect x="2" y="9" width="5" height="5" />
    <rect x="9" y="9" width="5" height="5" />
  </>
)
const ICON_WORKERS = (
  <>
    <rect x="2" y="2" width="12" height="5" />
    <rect x="2" y="9" width="12" height="5" />
    <path d="M4.5 4.5h1M4.5 11.5h1" />
  </>
)
const ICON_DEVICES = (
  <>
    <rect x="1.5" y="2.5" width="13" height="9" />
    <path d="M5.5 14.5h5M8 11.5v3" />
  </>
)
const ICON_FILES = (
  <>
    <rect x="3" y="3" width="10" height="10" />
    <path d="M6 1v2M10 1v2M6 13v2M10 13v2M1 6h2M1 10h2M13 6h2M13 10h2" />
  </>
)
const ICON_AGENTS = (
  <>
    <circle cx="3.5" cy="8" r="1.5" />
    <circle cx="12.5" cy="4" r="1.5" />
    <circle cx="12.5" cy="12" r="1.5" />
    <path d="M5 8h6M5 8l5.5-4M5 8l5.5 4" />
  </>
)
const ICON_CREDENTIALS = (
  <>
    <circle cx="5" cy="8" r="2.5" />
    <path d="M7.5 8H14M11 8v2.5M14 8v2" />
  </>
)
const ICON_OPERATIONS = (
  <>
    <circle cx="8" cy="8" r="5.5" />
    <path d="M8 5v3l2 2" />
  </>
)

function NavIcon({ children }) {
  return (
    <svg
      className="nav-icon"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.3"
      strokeLinecap="square"
      aria-hidden="true"
    >
      {children}
    </svg>
  )
}

export default function Layout() {
  const { t, locale, setLocale } = useI18n()
  const { signOut } = useAuth()
  const [collapsed, setCollapsed] = useState(false)

  const NAV_ITEMS = [
    { to: '/', label: t('nav.dashboard'), exact: true, icon: ICON_DASHBOARD },
    { to: '/workers', label: t('nav.workers'), icon: ICON_WORKERS },
    { to: '/devices', label: t('nav.devices'), icon: ICON_DEVICES },
    { to: '/boot', label: t('nav.assets'), icon: ICON_FILES },
    { to: '/agents', label: t('nav.agents'), icon: ICON_AGENTS },
    { to: '/credentials', label: t('nav.credentials'), icon: ICON_CREDENTIALS },
    { to: '/operations', label: t('nav.operations'), icon: ICON_OPERATIONS },
  ]

  return (
    <div className="layout">
      <aside className={`sidebar${collapsed ? ' collapsed' : ''}`}>
        <div className="sidebar-brand">
          <span className="brand-name">{t('nav.brand')}</span>
          <button
            className="sidebar-toggle"
            onClick={() => setCollapsed(!collapsed)}
            title={collapsed ? t('nav.expand') : t('nav.collapse')}
            aria-label={collapsed ? t('nav.expand') : t('nav.collapse')}
          >
            {collapsed ? '›' : '‹'}
          </button>
        </div>
        <nav className="sidebar-nav">
          {NAV_ITEMS.map(({ to, label, exact, icon }) => (
            <NavLink
              key={to}
              to={to}
              end={exact}
              className={({ isActive }) =>
                `nav-link${isActive ? ' active' : ''}`
              }
            >
              <NavIcon>{icon}</NavIcon>
              <span className="nav-label">{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer">
          <button className="logout-btn" onClick={signOut} title={t('auth.signOut')}>
            <NavIcon>
              <path d="M6 2.5H3.5v11H6" />
              <path d="M13 8H6.5" />
              <path d="M10.5 5.5 13 8l-2.5 2.5" />
            </NavIcon>
            <span className="nav-label">{t('auth.signOut')}</span>
          </button>
          <div className="lang-switch">
            <button
              className={`lang-btn${locale === 'zh-CN' ? ' lang-active' : ''}`}
              onClick={() => setLocale('zh-CN')}
            >
              中
            </button>
            <span className="lang-sep">/</span>
            <button
              className={`lang-btn${locale === 'en-US' ? ' lang-active' : ''}`}
              onClick={() => setLocale('en-US')}
            >
              EN
            </button>
          </div>
        </div>
      </aside>
      <main className="main">
        <Outlet />
      </main>
    </div>
  )
}
