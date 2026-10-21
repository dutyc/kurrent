// Kurrent 站主题：默认主题 + 黑白（monochrome）品牌色覆盖。
// 首页使用自定义 KurrentHome 组件（纸面 / 六虚版式），文档正文沿用默认主题。
import DefaultTheme from 'vitepress/theme'
import './custom.css'
import KurrentHome from './components/KurrentHome.vue'

export default {
  extends: DefaultTheme,
  enhanceApp({ app }) {
    app.component('KurrentHome', KurrentHome)
  },
}
