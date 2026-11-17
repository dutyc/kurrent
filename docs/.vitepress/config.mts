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
    collapsed: false,
    items: [
      { text: '项目概述', link: '/guide/overview' },
      { text: '架构总览', link: '/architecture/' },
    ],
  },
  {
    text: '概念',
    collapsed: false,
    items: [
      {
        text: '实体模型',
        collapsed: true,
        items: [
          { text: '概览', link: '/concepts/entities' },
          { text: '设备', link: '/concepts/entities/device' },
          { text: 'Worker', link: '/concepts/entities/worker' },
          { text: '系统盘', link: '/concepts/entities/system-disk' },
          { text: '绑定与放置', link: '/concepts/entities/binding' },
        ],
      },
      {
        text: '引导链',
        collapsed: true,
        items: [
          { text: '概览', link: '/concepts/boot-chain' },
          { text: '首跳', link: '/concepts/boot-chain/first-hop' },
          { text: '设备身份', link: '/concepts/boot-chain/device-identity' },
          { text: '数据面', link: '/concepts/boot-chain/data-plane' },
          { text: '交接给内核', link: '/concepts/boot-chain/handover' },
        ],
      },
      {
        text: '存储与数据面',
        collapsed: true,
        items: [
          { text: '概览', link: '/concepts/storage' },
          { text: '母盘', link: '/concepts/storage/master-images' },
          { text: '凭据与 TLS PSK', link: '/concepts/storage/credentials' },
          { text: '数据面与就绪', link: '/concepts/storage/data-plane' },
        ],
      },
      {
        text: '身份与访问',
        collapsed: true,
        items: [
          { text: '概览', link: '/concepts/identity' },
          { text: '人员与证书', link: '/concepts/identity/people' },
          { text: '管理 token 与会话', link: '/concepts/identity/tokens' },
          { text: '组件身份', link: '/concepts/identity/components' },
          { text: '审计与告警', link: '/concepts/identity/audit' },
        ],
      },
      {
        text: '声明式配置',
        collapsed: true,
        items: [
          { text: '概览', link: '/concepts/configuration' },
          { text: '控制面声明', link: '/concepts/configuration/control-plane' },
          { text: '节点声明', link: '/concepts/configuration/node' },
          { text: '校验与生效', link: '/concepts/configuration/validation' },
          { text: '漂移与覆盖', link: '/concepts/configuration/drift' },
        ],
      },
      { text: '安全模型', link: '/architecture/security' },
    ],
  },
  {
    text: '部署',
    collapsed: false,
    items: [
      { text: '部署前置', link: '/reference/host-prerequisites' },
      { text: '部署与首次启动', link: '/guide/deployment' },
      { text: '系统盘定制', link: '/guide/system-disk' },
    ],
  },
  {
    text: '运维',
    collapsed: false,
    items: [
      { text: '日常运维', link: '/guide/operations' },
      { text: '故障排查', link: '/guide/troubleshooting' },
    ],
  },
  {
    text: '固件',
    collapsed: false,
    items: [
      { text: '固件概述', link: '/firmware/' },
      { text: '固件组成', link: '/firmware/composition' },
      { text: '引导信任', link: '/firmware/trust' },
      { text: '构建', link: '/firmware/build' },
      { text: '部署与预置', link: '/firmware/deploy' },
      { text: 'Secure Boot 预置', link: '/firmware/secure-boot' },
    ],
  },
  {
    text: 'API',
    collapsed: false,
    items: [
      { text: 'API 参考', link: '/reference/api/' },
      { text: '认证与授权', link: '/reference/api/auth' },
      { text: '基础与状态', link: '/reference/api/foundation' },
      { text: '引导与设备准入', link: '/reference/api/boot' },
      { text: '设备池', link: '/reference/api/devices' },
      { text: 'Worker', link: '/reference/api/workers' },
      { text: '系统盘与凭据', link: '/reference/api/disks' },
      { text: 'Agent 与存储', link: '/reference/api/agents' },
      { text: '身份与 PKI', link: '/reference/api/identity' },
      { text: '运维与验证', link: '/reference/api/operations' },
    ],
  },
  {
    text: '参考',
    collapsed: false,
    items: [
      { text: '配置参考', link: '/reference/configuration' },
      { text: 'CLI 参考', link: '/reference/cli' },
      { text: '固件平台参考', link: '/reference/firmware-platform' },
      { text: '固件契约', link: '/reference/firmware-contract' },
      { text: '发行版矩阵', link: '/reference/distributions' },
      { text: '仓库与分支', link: '/reference/branches' },
      { text: '术语表', link: '/reference/glossary' },
    ],
  },
]

const enSidebar = [
  {
    text: 'Getting Started',
    collapsed: false,
    items: [
      { text: 'Overview', link: '/en/guide/overview' },
      { text: 'Architecture', link: '/en/architecture/' },
    ],
  },
  {
    text: 'Concepts',
    collapsed: false,
    items: [
      {
        text: 'Entity Model',
        collapsed: true,
        items: [
          { text: 'Overview', link: '/en/concepts/entities' },
          { text: 'Devices', link: '/en/concepts/entities/device' },
          { text: 'Workers', link: '/en/concepts/entities/worker' },
          { text: 'System Disks', link: '/en/concepts/entities/system-disk' },
          { text: 'Binding and Placement', link: '/en/concepts/entities/binding' },
        ],
      },
      {
        text: 'Boot Chain',
        collapsed: true,
        items: [
          { text: 'Overview', link: '/en/concepts/boot-chain' },
          { text: 'First Hop', link: '/en/concepts/boot-chain/first-hop' },
          { text: 'Device Identity', link: '/en/concepts/boot-chain/device-identity' },
          { text: 'Data Plane', link: '/en/concepts/boot-chain/data-plane' },
          { text: 'Hand-over to the Kernel', link: '/en/concepts/boot-chain/handover' },
        ],
      },
      {
        text: 'Storage and Data Plane',
        collapsed: true,
        items: [
          { text: 'Overview', link: '/en/concepts/storage' },
          { text: 'Master Images', link: '/en/concepts/storage/master-images' },
          { text: 'Credentials and TLS PSK', link: '/en/concepts/storage/credentials' },
          { text: 'Data Plane and Readiness', link: '/en/concepts/storage/data-plane' },
        ],
      },
      {
        text: 'Identity and Access',
        collapsed: true,
        items: [
          { text: 'Overview', link: '/en/concepts/identity' },
          { text: 'People and Certificates', link: '/en/concepts/identity/people' },
          { text: 'Management Tokens and Sessions', link: '/en/concepts/identity/tokens' },
          { text: 'Component Identity', link: '/en/concepts/identity/components' },
          { text: 'Audit and Alarms', link: '/en/concepts/identity/audit' },
        ],
      },
      {
        text: 'Declarative Configuration',
        collapsed: true,
        items: [
          { text: 'Overview', link: '/en/concepts/configuration' },
          { text: 'Control-Plane Declaration', link: '/en/concepts/configuration/control-plane' },
          { text: 'Node Declaration', link: '/en/concepts/configuration/node' },
          { text: 'Validation and Application', link: '/en/concepts/configuration/validation' },
          { text: 'Drift and Overrides', link: '/en/concepts/configuration/drift' },
        ],
      },
      { text: 'Security Model', link: '/en/architecture/security' },
    ],
  },
  {
    text: 'Deployment',
    collapsed: false,
    items: [
      { text: 'Deployment Prerequisites', link: '/en/reference/host-prerequisites' },
      { text: 'Deployment and First Boot', link: '/en/guide/deployment' },
      { text: 'System Disk Customization', link: '/en/guide/system-disk' },
    ],
  },
  {
    text: 'Operations',
    collapsed: false,
    items: [
      { text: 'Operations', link: '/en/guide/operations' },
      { text: 'Troubleshooting', link: '/en/guide/troubleshooting' },
    ],
  },
  {
    text: 'Firmware',
    collapsed: false,
    items: [
      { text: 'Firmware Overview', link: '/en/firmware/' },
      { text: 'Firmware Composition', link: '/en/firmware/composition' },
      { text: 'Boot Trust', link: '/en/firmware/trust' },
      { text: 'Build', link: '/en/firmware/build' },
      { text: 'Deployment and Provisioning', link: '/en/firmware/deploy' },
      { text: 'Secure Boot Provisioning', link: '/en/firmware/secure-boot' },
    ],
  },
  {
    text: 'API',
    collapsed: false,
    items: [
      { text: 'API Reference', link: '/en/reference/api/' },
      { text: 'Authentication and Authorization', link: '/en/reference/api/auth' },
      { text: 'Basics and State', link: '/en/reference/api/foundation' },
      { text: 'Boot and Device Admission', link: '/en/reference/api/boot' },
      { text: 'Device Pool', link: '/en/reference/api/devices' },
      { text: 'Workers', link: '/en/reference/api/workers' },
      { text: 'System Disks and Credentials', link: '/en/reference/api/disks' },
      { text: 'Agents and Storage', link: '/en/reference/api/agents' },
      { text: 'Identity and PKI', link: '/en/reference/api/identity' },
      { text: 'Operations and Verification', link: '/en/reference/api/operations' },
    ],
  },
  {
    text: 'Reference',
    collapsed: false,
    items: [
      { text: 'Configuration Reference', link: '/en/reference/configuration' },
      { text: 'CLI', link: '/en/reference/cli' },
      { text: 'Firmware Platform Reference', link: '/en/reference/firmware-platform' },
      { text: 'Firmware Contract', link: '/en/reference/firmware-contract' },
      { text: 'Distribution Matrix', link: '/en/reference/distributions' },
      { text: 'Repositories and Branches', link: '/en/reference/branches' },
      { text: 'Glossary', link: '/en/reference/glossary' },
    ],
  },
]

export default defineConfig({
  lang: 'zh-CN',
  title: 'Kurrent（周流）',
  description: 'Make bare metal flow — 云原生无状态裸金属交付',
  base,
  cleanUrls: process.env.VITEPRESS_NO_CLEAN !== '1',
  // Shiki 无 iPXE 语法，按 bash 着色以保留变量与命令高亮
  markdown: { languageAlias: { ipxe: 'bash' } },
  // 首页自绘组件需要自行拼文档页链接；与 cleanUrls 同源，避免 Pages 构建下漏 .html
  vite: {
    // 完整 API 参考页本身体积超过默认 500 kB 告警阈值，属预期
    build: { chunkSizeWarningLimit: 800 },
    define: {
      __KURRENT_DOCS_CLEAN__: JSON.stringify(process.env.VITEPRESS_NO_CLEAN !== '1'),
    },
  },
  head: [
    ['link', { rel: 'icon', href: `${base}favicon.ico`, sizes: '32x32' }],
    ['link', { rel: 'icon', type: 'image/svg+xml', href: `${base}logo.svg` }],
  ],
  lastUpdated: true,
  themeConfig: {
    search: {
      provider: 'local',
      options: {
        locales: {
          root: {
            translations: {
              button: { buttonText: '搜索', buttonAriaLabel: '搜索文档' },
              modal: {
                displayDetails: '显示详细列表',
                resetButtonTitle: '清除查询条件',
                backButtonTitle: '返回',
                noResultsText: '没有找到结果',
                footer: {
                  selectText: '选择',
                  selectKeyAriaLabel: '回车',
                  navigateText: '切换',
                  navigateUpKeyAriaLabel: '向上箭头',
                  navigateDownKeyAriaLabel: '向下箭头',
                  closeText: '关闭',
                  closeKeyAriaLabel: 'Esc',
                },
              },
            },
          },
          en: {
            translations: {
              button: { buttonText: 'Search', buttonAriaLabel: 'Search documentation' },
              modal: {
                displayDetails: 'Display detailed list',
                resetButtonTitle: 'Reset search criteria',
                backButtonTitle: 'Back',
                noResultsText: 'No results found',
                footer: {
                  selectText: 'to select',
                  selectKeyAriaLabel: 'Enter',
                  navigateText: 'to navigate',
                  navigateUpKeyAriaLabel: 'Up arrow',
                  navigateDownKeyAriaLabel: 'Down arrow',
                  closeText: 'to close',
                  closeKeyAriaLabel: 'Escape',
                },
              },
            },
          },
        },
      },
    },
  },
  locales: {
    root: {
      label: '简体中文',
      lang: 'zh-CN',
      themeConfig: {
        logo: { light: '/logo.svg', dark: '/logo-dark.svg' },
        nav: [
          { text: '首页', link: '/' },
          { text: '项目概述', link: '/guide/overview' },
          { text: '路线图', link: '/about/roadmap' },
          { text: '参与贡献', link: '/about/contributing' },
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
          { text: 'Roadmap', link: '/en/about/roadmap' },
          { text: 'Contributing', link: '/en/about/contributing' },
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
