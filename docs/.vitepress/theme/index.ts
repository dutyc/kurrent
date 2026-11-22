// Kurrent 站主题：默认主题 + 黑白（monochrome）品牌色覆盖。
// 首页使用自定义 KurrentHome 组件（纸面 / 六虚版式），文档正文沿用默认主题。
import { h } from 'vue'
import { inBrowser } from 'vitepress'
import DefaultTheme from 'vitepress/theme'
import './custom.css'
import KurrentHome from './components/KurrentHome.vue'
import DiagramZoom from './components/DiagramZoom.vue'

// 复制按钮的反馈兜底：默认实现只在 navigator.clipboard 成功时标 .copied，
// 剪贴板被拒时没有任何反馈。这里在默认实现未生效时用 execCommand 回退补一次真实复制，
// 成功才显示「已复制」，失败不假装。
if (inBrowser) {
  window.addEventListener('click', (e) => {
    const el = e.target
    if (!(el instanceof HTMLElement) || !el.matches('div[class*="language-"] > button.copy')) return
    window.setTimeout(() => {
      if (el.classList.contains('copied')) return
      const sibling = el.nextElementSibling?.nextElementSibling
      if (!sibling) return
      const isShell = /language-(shellscript|shell|bash|sh|zsh)/.test(el.parentElement?.className || '')
      const clone = sibling.cloneNode(true) as HTMLElement
      clone.querySelectorAll('.vp-copy-ignore, .diff.remove').forEach((n) => n.remove())
      let text = clone.textContent || ''
      if (isShell) text = text.replace(/^ *(\$|>) /gm, '').trim()
      copyText(text).then((ok) => {
        if (!ok) return
        el.classList.add('copied')
        window.setTimeout(() => {
          el.classList.remove('copied')
          el.blur()
        }, 2000)
      })
    }, 150)
  })
}

async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {}
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.setAttribute('readonly', '')
    ta.style.position = 'absolute'
    ta.style.left = '-9999px'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch {
    return false
  }
}

export default {
  extends: DefaultTheme,
  Layout() {
    return h(DefaultTheme.Layout, null, {
      'layout-bottom': () => h(DiagramZoom),
    })
  },
  enhanceApp({ app }) {
    app.component('KurrentHome', KurrentHome)
  },
}
