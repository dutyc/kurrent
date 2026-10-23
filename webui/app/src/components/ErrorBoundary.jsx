/* 页面渲染兜底：任何未捕获的渲染异常只在内容区显示错误，侧边栏保持可点。
   刷新后若同一路由继续抛错，也不会得到白屏；「返回概览」重挂载并回到根路由。 */
import { Component } from 'react'
import { useI18n } from '../i18n'
import './ErrorBoundary.css'

class Boundary extends Component {
  state = { error: null }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    console.error('page render failed', error, info)
  }

  reset = () => {
    this.setState({ error: null })
    if (window.location.hash !== '#/') window.location.hash = '#/'
  }

  render() {
    const { t, children } = this.props
    if (!this.state.error) return children
    return (
      <div className="page-crash">
        <p className="page-crash-title">{t('common.pageCrash')}</p>
        <p className="page-crash-detail">
          {this.state.error.message || String(this.state.error)}
        </p>
        <button type="button" className="page-crash-action" onClick={this.reset}>
          {t('common.backHome')}
        </button>
      </div>
    )
  }
}

export default function ErrorBoundary({ children }) {
  const { t } = useI18n()
  return <Boundary t={t}>{children}</Boundary>
}
