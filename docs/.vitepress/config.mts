// Kurrent 官网兼文档站（VitePress）：zh-CN 默认（root 路径），en 英文子路径。
// 品牌：Kurrent（周流）· Make bare metal flow · logo = 六虚（docs/public/logo.svg）
// 构建环境注入（GitHub Pages 等子路径部署）：VITEPRESS_BASE=/kurrent/ 设站点 base；
// VITEPRESS_NO_CLEAN=1 时产物保留 .html 扩展名（GitHub Pages 对无扩展名文件不友好）。
import { defineConfig } from 'vitepress'

// 站点 base（head 数组中的链接不自动加 base 前缀，须与 favicon 等拼接同源）
const base = process.env.VITEPRESS_BASE || '/'

export default defineConfig({
  lang: 'zh-CN',
  title: 'Kurrent（周流）',
  description: 'Make bare metal flow — 云原生无状态裸金属交付',
  base,
  cleanUrls: process.env.VITEPRESS_NO_CLEAN !== '1',
  // 首页自绘组件需要自行拼文档页链接；与 cleanUrls 同源，避免 Pages 构建下漏 .html
  vite: {
    define: {
      __KURRENT_DOCS_CLEAN__: JSON.stringify(process.env.VITEPRESS_NO_CLEAN !== '1'),
    },
  },
  head: [
    ['link', { rel: 'icon', href: `${base}favicon.ico`, sizes: '32x32' }],
    ['link', { rel: 'icon', type: 'image/svg+xml', href: `${base}logo.svg` }],
  ],
  lastUpdated: true,
  locales: {
    root: {
      label: '简体中文',
      lang: 'zh-CN',
      themeConfig: {
        logo: { light: '/logo.svg', dark: '/logo-dark.svg' },
        nav: [
          { text: '首页', link: '/' },
          { text: '指南', link: '/guide/deployment' },
          { text: '架构', link: 'https://github.com/dutyc/kurrent/blob/main/about/zh/ARCHITECTURE.md' },
          { text: 'API', link: 'https://github.com/dutyc/kurrent/blob/main/api/control-plane-api.zh-CN.md' },
          { text: 'CLI', link: 'https://github.com/dutyc/kurrent/blob/main/cli/README.md' },
        ],
        sidebar: {
          '/guide/': [
            {
              text: '部署与使用',
              items: [
                { text: '部署指南', link: '/guide/deployment' },
                { text: '使用流程', link: '/guide/usage' },
              ],
            },
          ],
        },
        outline: { label: '本页目录' },
        docFooter: { prev: '上一篇', next: '下一篇' },
        darkModeSwitchLabel: '主题',
        lightModeSwitchTitle: '切换到浅色模式',
        darkModeSwitchTitle: '切换到深色模式',
        sidebarMenuLabel: '目录',
        returnToTopLabel: '回到顶部',
        lastUpdated: { text: '最后更新于', formatOptions: { dateStyle: 'short', timeStyle: 'medium' } },
        socialLinks: [{ icon: 'github', link: 'https://github.com/dutyc/kurrent' }],
        footer: {
          message: 'Kurrent（周流）· Make bare metal flow',
          copyright: 'Apache-2.0 License · <a href="https://github.com/dutyc/kurrent">github.com/dutyc/kurrent</a>',
        },
      },
    },
    en: {
      label: 'English',
      lang: 'en',
      themeConfig: {
        logo: { light: '/logo.svg', dark: '/logo-dark.svg' },
        nav: [
          { text: 'Home', link: '/en/' },
          { text: 'Guide', link: '/en/guide/deployment' },
          { text: 'Architecture', link: 'https://github.com/dutyc/kurrent/blob/main/about/en/ARCHITECTURE.md' },
          { text: 'API', link: 'https://github.com/dutyc/kurrent/blob/main/api/control-plane-api.en.md' },
          { text: 'CLI', link: 'https://github.com/dutyc/kurrent/blob/main/cli/README.md' },
        ],
        sidebar: {
          '/en/guide/': [
            {
              text: 'Deployment & Usage',
              items: [
                { text: 'Deployment Guide', link: '/en/guide/deployment' },
                { text: 'Usage Guide', link: '/en/guide/usage' },
              ],
            },
          ],
        },
        outline: { label: 'On this page' },
        docFooter: { prev: 'Previous', next: 'Next' },
        darkModeSwitchLabel: 'Theme',
        lightModeSwitchTitle: 'Switch to light mode',
        darkModeSwitchTitle: 'Switch to dark mode',
        sidebarMenuLabel: 'Menu',
        returnToTopLabel: 'Back to top',
        lastUpdated: { text: 'Last updated', formatOptions: { dateStyle: 'short', timeStyle: 'medium' } },
        socialLinks: [{ icon: 'github', link: 'https://github.com/dutyc/kurrent' }],
        footer: {
          message: 'Kurrent · Make bare metal flow',
          copyright: 'Apache-2.0 License · <a href="https://github.com/dutyc/kurrent">github.com/dutyc/kurrent</a>',
        },
      },
    },
  },
})
