// Kurrent 官网兼文档站（VitePress）：zh-CN 默认（root 路径），en 英文子路径。
// 品牌：Kurrent（周流）· Make bare metal flow · logo = 六虚（docs/public/logo.svg）
// 构建环境注入（GitHub Pages 等子路径部署）：VITEPRESS_BASE=/kurrent/ 设站点 base；
// VITEPRESS_NO_CLEAN=1 时产物保留 .html 扩展名（GitHub Pages 对无扩展名文件不友好）。
import { defineConfig } from 'vitepress'

// 站点 base（head 数组中的链接不自动加 base 前缀，须与 favicon 等拼接同源）
const base = process.env.VITEPRESS_BASE || '/'

const zhSidebar = [
  {
    text: '入门',
    collapsed: true,
    items: [
      { text: '项目概述', link: '/guide/overview' },
      { text: '部署', link: '/guide/deployment' },
      { text: '首次交付', link: '/guide/usage' },
    ],
  },
  {
    text: '概念',
    collapsed: true,
    items: [
      { text: '架构总览', link: '/architecture/' },
      { text: '设备、Worker 与系统盘', link: '/concepts/entities' },
      { text: '引导链', link: '/concepts/boot-chain' },
      { text: '固件概述', link: '/firmware/' },
      { text: '存储与数据面', link: '/concepts/storage' },
      { text: '身份与访问', link: '/concepts/identity' },
      { text: '安全模型', link: '/architecture/security' },
      { text: '声明式配置', link: '/concepts/configuration' },
    ],
  },
  {
    text: '任务',
    collapsed: true,
    items: [
      { text: '系统盘定制', link: '/guide/system-disk' },
      { text: '固件构建与部署', link: '/firmware/build-deploy' },
      { text: '日常运维', link: '/guide/operations' },
      { text: '故障排查', link: '/guide/troubleshooting' },
    ],
  },
  {
    text: '参考',
    collapsed: true,
    items: [
      { text: '配置参考', link: '/reference/configuration' },
      { text: 'CLI 参考', link: '/reference/cli' },
      { text: 'API 参考', link: '/reference/api' },
      { text: '固件契约', link: '/reference/firmware-contract' },
      { text: '发行版矩阵', link: '/reference/distributions' },
      { text: '术语表', link: '/reference/glossary' },
    ],
  },
  {
    text: '项目',
    collapsed: true,
    items: [
      { text: '路线图', link: '/about/roadmap' },
      { text: '参与贡献', link: '/about/contributing' },
    ],
  },
]

const enSidebar = [
  {
    text: 'Getting Started',
    collapsed: true,
    items: [
      { text: 'Overview', link: '/en/guide/overview' },
      { text: 'Deployment', link: '/en/guide/deployment' },
      { text: 'First Delivery', link: '/en/guide/usage' },
    ],
  },
  {
    text: 'Concepts',
    collapsed: true,
    items: [
      { text: 'Architecture', link: '/en/architecture/' },
      { text: 'Devices, Workers and Disks', link: '/en/concepts/entities' },
      { text: 'Boot Chain', link: '/en/concepts/boot-chain' },
      { text: 'Firmware', link: '/en/firmware/' },
      { text: 'Storage and Data Plane', link: '/en/concepts/storage' },
      { text: 'Identity and Access', link: '/en/concepts/identity' },
      { text: 'Security Model', link: '/en/architecture/security' },
      { text: 'Declarative Configuration', link: '/en/concepts/configuration' },
    ],
  },
  {
    text: 'Tasks',
    collapsed: true,
    items: [
      { text: 'System Disk Customization', link: '/en/guide/system-disk' },
      { text: 'Firmware Build and Deployment', link: '/en/firmware/build-deploy' },
      { text: 'Operations', link: '/en/guide/operations' },
      { text: 'Troubleshooting', link: '/en/guide/troubleshooting' },
    ],
  },
  {
    text: 'Reference',
    collapsed: true,
    items: [
      { text: 'Configuration', link: '/en/reference/configuration' },
      { text: 'CLI', link: '/en/reference/cli' },
      { text: 'API', link: '/en/reference/api' },
      { text: 'Firmware Contract', link: '/en/reference/firmware-contract' },
      { text: 'Distribution Matrix', link: '/en/reference/distributions' },
      { text: 'Glossary', link: '/en/reference/glossary' },
    ],
  },
  {
    text: 'Project',
    collapsed: true,
    items: [
      { text: 'Roadmap', link: '/en/about/roadmap' },
      { text: 'Contributing', link: '/en/about/contributing' },
    ],
  },
]

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
          { text: '项目概述', link: '/guide/overview' },
        ],
        sidebar: zhSidebar,
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
          { text: 'Overview', link: '/en/guide/overview' },
        ],
        sidebar: enSidebar,
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
