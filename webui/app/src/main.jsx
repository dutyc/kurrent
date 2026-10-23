import React from 'react'
import ReactDOM from 'react-dom/client'
import { HashRouter } from 'react-router-dom'
import { I18nProvider } from './i18n'
import App from './App'
import './styles/global.css'

// 畸形百分号编码（如 #/%E0%A4%A）会让 React Router 解不出路径并渲染空树；
// 启动前退回根路由，避免刷新后白屏。
try {
  decodeURIComponent(window.location.hash.slice(1))
} catch {
  window.history.replaceState(
    null,
    '',
    `${window.location.pathname}${window.location.search}#/`
  )
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <I18nProvider>
      <HashRouter>
        <App />
      </HashRouter>
    </I18nProvider>
  </React.StrictMode>
)
