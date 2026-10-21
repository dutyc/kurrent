<script setup>
import { computed } from 'vue'
import { useData } from 'vitepress'

const props = defineProps({
  lang: { type: String, default: 'zh' },
})

const { site } = useData()
// 自定义首页的链接须自行拼 base 与 .html（Pages 构建关闭 cleanUrls）
const docHref = (path) => `${site.value.base}${path}${__KURRENT_DOCS_CLEAN__ ? '' : '.html'}`
const homeHref = computed(() => (props.lang === 'en' ? `${site.value.base}en/` : site.value.base))
const linkHref = (href) => (href.startsWith('http') ? href : docHref(href.replace(/^\//, '')))
const langHref = computed(() => (props.lang === 'en' ? site.value.base : `${site.value.base}en/`))
const langLabel = props.lang === 'en' ? '中文' : 'English'
const langHreflang = props.lang === 'en' ? 'zh-CN' : 'en'

const zh = {
  nav: { proposition: '命题', unit: '单元', state: '状态', docs: '文档', github: 'GitHub ↗' },
  meta: 'Stateless compute · System as the unit',
  quote: '周流六虚，上下无常<br>刚柔相易，唯变所适<br><span>——《周易·系辞下》</span>',
  eyebrow: '云原生 · 无状态 · 裸金属',
  h1: ['计算不该', '被硬件占有。'],
  lede: 'Kurrent 把身份、系统与状态从机器上剥离。<strong>节点保持空，系统成为调度的唯一单元。</strong>机器只是系统此刻所在的位置。',
  cta: ['部署指南', '它如何成立'],
  propCap: '命题 / PROPOSITION',
  prop: [
    ['节点', '只提供位置，<b>不持有身份与状态</b>。'],
    ['状态', '不消失，<b>回归系统自身</b>。'],
    ['单元', '被调度的是<b>一整个系统</b>。'],
    ['变化', '换机器不改系统，<b>唯变所适</b>。'],
  ],
  s0: {
    side: '命题',
    h2: '无状态，是让机器重新变得可替换。',
    sub: '不是把状态藏到看不见的地方，而是让每一份状态都有明确的归属，让节点回到“位置”这个唯一角色。',
    quote: ['节点可以被拔走、替换、重新上架，而系统仍然成立。', '因为系统从来不在那台机器里。'],
    small: '身份在设备台账，系统盘在存储侧，声明在版本库，凭据由控制面签发。机器上留下的，只有一次启动所需的临时连接。',
  },
  s1: {
    side: '单元',
    h2: '调度一整个系统，而不是机器上的一段负载。',
    sub: '一个系统由四件事共同定义。它们一起移动、一起存在、一起被替换；拆开任何一件，都不是同一个系统。',
    head: ['对象', '内容', '归属'],
    rows: [
      ['身份', 'Worker 标识、主机名、绑定的设备公钥', '设备台账 / 控制面'],
      ['系统盘', '完整盘 NQN、<code>os_tag</code>、镜像版本', '存储节点'],
      ['声明', '<code>kurrent.yaml</code>：网络、后端、数据目录、NQN 命名域', '版本库 / 节点'],
      ['凭据', 'DHHC-1 认证密钥；由它派生的 TLS PSK', '控制面 / 目标端'],
    ],
    total: ['节点', '不持有以上任何一项', '空'],
  },
  s2: {
    side: '状态',
    h2: '状态回归系统本身，而不是外置成另一层基础设施。',
    sub: '系统盘承载完整系统，凭据跟着盘走，绑定关系有唯一权威。没有为了“无状态”而额外引入一层存储抽象。',
    items: [
      ['/ 持久化', '盘即是系统', '启动、运行与数据都在同一块盘上。盘被移动，系统随之移动；不需要把状态拆进另一套卷与快照体系。'],
      ['/ 授权', '绑定即认证', '设备与 Worker 一对一。开机时只向绑定的设备下发它所属系统的连接信息；绑定关系就是认证边界。'],
      ['/ 交付', '一次启动，一条边界', '引导链负责把系统交给下一次启动。它是交付入口，不是项目本体；项目调度的是系统，不是启动过程。'],
    ],
    fig: 'Fig. 1 — 状态的归属：节点为空，系统有址。',
  },
  s3: {
    side: '验证',
    h2: '已验证发行版。',
    sub: '下列发行版已完成整机冷启动验证；验证范围不构成发行版限制。',
    items: [
      ['/ 整机启动', 'Debian 12/13<br>Ubuntu 24.04/26.04<br>Fedora 44', '冷启动至系统登录'],
      ['/ 数据面', '4421', 'NVMe/TCP · TLS 1.3<br>固件段与内核段共用'],
      ['/ 认证', 'DHHC-1', '建盘即签发<br>轮换 / 吊销即时生效'],
      ['/ 明文', '无', '缺密钥或端口未就绪<br>拒绝引导'],
    ],
  },
  s4: {
    side: '文档',
    items: [
      ['01', '部署指南', '控制面与存储节点，从声明到收敛。', '/guide/deployment'],
      ['02', '使用流程', '入池、绑定、建盘、默认系统与凭据。', '/guide/usage'],
      ['03', '架构', '控制面、数据面与交付链的边界。', 'https://github.com/dutyc/kurrent/blob/main/about/zh/ARCHITECTURE.md'],
      ['04', 'API 参考', '控制面接口与引导变量的权威定义。', 'https://github.com/dutyc/kurrent/blob/main/api/control-plane-api.zh-CN.md'],
      ['05', 'CLI 参考', '命令行接口、凭据与日常操作。', 'https://github.com/dutyc/kurrent/blob/main/cli/README.md'],
    ],
  },
  footer: {
    left: ['KURRENT（周流）', 'Make bare metal flow', 'Apache-2.0'],
    seal: '周流六虚',
    right: ['github.com/dutyc/kurrent', '无状态计算 · 系统即单元'],
  },
}

const en = {
  nav: { proposition: 'Proposition', unit: 'Unit', state: 'State', docs: 'Docs', github: 'GitHub ↗' },
  meta: 'Stateless compute · System as the unit',
  quote: 'The six lines flow, above and below without constancy; the firm and the yielding interchange — it adapts to whatever change may come. — the I Ching · Great Commentary',
  eyebrow: 'Cloud-native · Stateless · Bare metal',
  h1: ['Compute should not', 'be owned by hardware.'],
  lede: 'Kurrent strips identity, system and state away from the machine. <strong>The node stays empty; the system becomes the only unit of scheduling.</strong> The machine is merely where the system happens to be.',
  cta: ['Deployment Guide', 'How it holds'],
  propCap: 'Proposition',
  prop: [
    ['Node', 'Provides location only; <b>holds no identity or state</b>.'],
    ['State', 'Does not vanish; <b>returns to the system itself</b>.'],
    ['Unit', 'What is scheduled is <b>a whole system</b>.'],
    ['Change', 'Replace the machine, not the system; <b>adapt to the change</b>.'],
  ],
  s0: {
    side: 'Proposition',
    h2: 'Statelessness makes a machine replaceable again.',
    sub: 'Not hiding state out of sight, but giving every piece of state a clear owner and returning the node to its only role: a location.',
    quote: ['A node can be pulled, replaced and re-racked while the system still stands.', 'Because the system was never inside that machine.'],
    small: 'Identity lives in the device ledger, the system disk on storage, the declaration in the repository, and credentials are issued by the control plane. What remains on the machine is a temporary connection for one boot.',
  },
  s1: {
    side: 'Unit',
    h2: 'Schedule a whole system, not a workload on a machine.',
    sub: 'A system is defined by four things. They move, exist and are replaced together; remove any one and it is no longer the same system.',
    head: ['Object', 'Content', 'Owner'],
    rows: [
      ['Identity', 'Worker id, hostname, bound device public key', 'Device ledger / control plane'],
      ['System disk', 'Full disk NQN, <code>os_tag</code>, image version', 'Storage node'],
      ['Declaration', '<code>kurrent.yaml</code>: network, backend, data directory, NQN namespace', 'Repository / node'],
      ['Credentials', 'DHHC-1 authentication key; TLS PSK derived from it', 'Control plane / target'],
    ],
    total: ['Node', 'Holds none of the above', 'Empty'],
  },
  s2: {
    side: 'State',
    h2: 'State returns to the system itself, not to another infrastructure layer.',
    sub: 'The system disk carries the whole system, credentials follow the disk, and binding has a single authority. No extra storage abstraction is introduced just to claim statelessness.',
    items: [
      ['/ Persistence', 'The disk is the system', 'Boot, runtime and data live on the same disk. Move the disk and the system moves with it; no separate volume and snapshot layer.'],
      ['/ Authorization', 'Binding is authentication', "Devices and Workers are one-to-one. At boot only the bound device receives its system's connection data; the binding is the authentication boundary."],
      ['/ Delivery', 'One boot, one boundary', 'The boot chain hands the system to the next boot. It is the delivery entry, not the project itself; Kurrent schedules systems, not boot processes.'],
    ],
    fig: 'Fig. 1 — State has an owner; the node is empty.',
  },
  s3: {
    side: 'Verified',
    h2: 'Verified distributions.',
    sub: 'The following distributions have completed whole-machine cold-boot verification; the list does not limit what can boot.',
    items: [
      ['/ Whole machine', 'Debian 12/13<br>Ubuntu 24.04/26.04<br>Fedora 44', 'Cold boot to system login'],
      ['/ Data plane', '4421', 'NVMe/TCP · TLS 1.3<br>shared by firmware and kernel'],
      ['/ Authentication', 'DHHC-1', 'Issued at disk creation<br>rotation / revocation immediate'],
      ['/ Plaintext', 'None', 'Missing key or unready port<br>boot refused'],
    ],
  },
  s4: {
    side: 'Docs',
    items: [
      ['01', 'Deployment Guide', 'Controller and storage node, from declaration to convergence.', '/en/guide/deployment'],
      ['02', 'Usage Guide', 'Enrollment, binding, disks, default system and credentials.', '/en/guide/usage'],
      ['03', 'Architecture', 'The boundaries of control plane, data plane and delivery chain.', 'https://github.com/dutyc/kurrent/blob/main/about/en/ARCHITECTURE.md'],
      ['04', 'API Reference', 'Authoritative definitions of control-plane APIs and boot variables.', 'https://github.com/dutyc/kurrent/blob/main/api/control-plane-api.en.md'],
      ['05', 'CLI Reference', 'Command surface, credentials and day-to-day operations.', 'https://github.com/dutyc/kurrent/blob/main/cli/README.md'],
    ],
  },
  footer: {
    left: ['KURRENT (周流)', 'Make bare metal flow', 'Apache-2.0'],
    seal: '周流六虚',
    right: ['github.com/dutyc/kurrent', 'Stateless compute · System as the unit'],
  },
}

const c = computed(() => (props.lang === 'en' ? en : zh))
</script>

<template>
  <div class="k-home" :lang="lang === 'en' ? 'en' : 'zh-CN'">
    <div class="masthead">
      <div class="wrap">
        <a class="wordmark" :href="homeHref">
          <svg viewBox="0 0 240 240" fill="currentColor" aria-hidden="true">
            <rect x="20" y="18" width="200" height="24" />
            <rect x="20" y="54" width="40" height="24" /><rect x="96" y="54" width="124" height="24" />
            <rect x="20" y="90" width="200" height="24" />
            <rect x="20" y="126" width="82" height="24" /><rect x="138" y="126" width="82" height="24" />
            <rect x="20" y="162" width="200" height="24" />
            <rect x="20" y="198" width="124" height="24" /><rect x="180" y="198" width="40" height="24" />
          </svg>
          Kurrent<small>周流</small>
        </a>
        <div class="mast-meta">{{ c.meta }}</div>
        <nav class="mast-nav">
          <a href="#proposition">{{ c.nav.proposition }}</a>
          <a href="#unit">{{ c.nav.unit }}</a>
          <a href="#state">{{ c.nav.state }}</a>
          <a href="#docs">{{ c.nav.docs }}</a>
          <a href="https://github.com/dutyc/kurrent" target="_blank" rel="noopener">{{ c.nav.github }}</a>
          <a class="lang" :href="langHref" :hreflang="langHreflang">{{ langLabel }}</a>
        </nav>
      </div>
    </div>

    <header class="hero">
      <div class="wrap" :class="{ 'en-grid': lang === 'en' }">
        <div v-if="lang !== 'en'" class="vertical-quote" v-html="c.quote"></div>
        <div>
          <p v-if="lang === 'en'" class="quote-en">{{ c.quote }}</p>
          <div class="eyebrow">{{ c.eyebrow }}</div>
          <h1>{{ c.h1[0] }}<br>{{ c.h1[1] }}</h1>
          <p class="lede" v-html="c.lede"></p>
          <div class="links">
            <a class="link" :href="docHref(lang === 'en' ? 'en/guide/deployment' : 'guide/deployment')">{{ c.cta[0] }} <span>→</span></a>
            <a class="link" href="#unit">{{ c.cta[1] }} <span>↓</span></a>
          </div>
        </div>
        <aside class="prop">
          <div class="cap">{{ c.propCap }}</div>
          <dl>
            <template v-for="item in c.prop" :key="item[0]">
              <dt>{{ item[0] }}</dt>
              <dd v-html="item[1]"></dd>
            </template>
          </dl>
        </aside>
      </div>
    </header>

    <section id="proposition">
      <div class="sec">
        <div class="wrap">
          <div class="side"><b>00</b>{{ c.s0.side }}</div>
          <div>
            <h2>{{ c.s0.h2 }}</h2>
            <p class="sub">{{ c.s0.sub }}</p>
            <div class="empty">
              <div class="yao" aria-hidden="true">
                <div class="yao-row"></div>
                <div class="yao-row left"></div>
                <div class="yao-row"></div>
                <div class="yao-row center"></div>
                <div class="yao-row"></div>
                <div class="yao-row right"></div>
                <span class="yao-cap">六虚 · COLOPHON</span>
              </div>
              <div class="empty-text">
                <p>{{ c.s0.quote[0] }}</p>
                <p>{{ c.s0.quote[1] }}</p>
                <small>{{ c.s0.small }}</small>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section id="unit">
      <div class="sec">
        <div class="wrap">
          <div class="side"><b>01</b>{{ c.s1.side }}</div>
          <div>
            <h2>{{ c.s1.h2 }}</h2>
            <p class="sub">{{ c.s1.sub }}</p>
            <div class="ledger">
              <div class="ledger-row head">
                <span v-for="h in c.s1.head" :key="h">{{ h }}</span>
              </div>
              <div v-for="row in c.s1.rows" :key="row[0]" class="ledger-row">
                <span class="k">{{ row[0] }}</span>
                <span class="v" v-html="row[1]"></span>
                <span class="h">{{ row[2] }}</span>
              </div>
              <div class="ledger-row total">
                <span class="k">{{ c.s1.total[0] }}</span>
                <span class="v">{{ c.s1.total[1] }}</span>
                <span class="h">{{ c.s1.total[2] }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section id="state">
      <div class="sec">
        <div class="wrap">
          <div class="side"><b>02</b>{{ c.s2.side }}</div>
          <div>
            <h2>{{ c.s2.h2 }}</h2>
            <p class="sub">{{ c.s2.sub }}</p>
            <div class="bounds">
              <div v-for="item in c.s2.items" :key="item[0]" class="bound">
                <div class="n">{{ item[0] }}</div>
                <h3>{{ item[1] }}</h3>
                <p>{{ item[2] }}</p>
              </div>
            </div>
            <p class="footnote">{{ c.s2.fig }}</p>
          </div>
        </div>
      </div>
    </section>

    <section id="verified">
      <div class="sec">
        <div class="wrap">
          <div class="side"><b>03</b>{{ c.s3.side }}</div>
          <div>
            <h2>{{ c.s3.h2 }}</h2>
            <p class="sub">{{ c.s3.sub }}</p>
            <div class="evidence">
              <div v-for="item in c.s3.items" :key="item[0]" class="ev">
                <div class="n">{{ item[0] }}</div>
                <div class="big" v-html="item[1]"></div>
                <div class="small" v-html="item[2]"></div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section id="docs">
      <div class="docs">
        <div class="wrap">
          <div class="side"><b>04</b>{{ c.s4.side }}</div>
          <div>
            <div class="doc-list">
              <a v-for="doc in c.s4.items" :key="doc[0]" class="doc" :href="linkHref(doc[3])">
                <span class="num">{{ doc[0] }}</span>
                <span><span class="t">{{ doc[1] }}</span><span class="d">{{ doc[2] }}</span></span>
                <span class="arrow">→</span>
              </a>
            </div>
          </div>
        </div>
      </div>
    </section>

    <footer>
      <div class="wrap">
        <div class="colophon">
          <b>{{ c.footer.left[0] }}</b><br>{{ c.footer.left[1] }}<br>{{ c.footer.left[2] }}
        </div>
        <div class="seal">{{ c.footer.seal }}</div>
        <div class="colophon right-col">{{ c.footer.right[0] }}<br>{{ c.footer.right[1] }}</div>
      </div>
    </footer>
  </div>
</template>

<style scoped>
.k-home {
  --paper: #f4f1ea;
  --ink: #151412;
  --ink-2: #4c4842;
  --ink-3: #8a847a;
  --rule: rgba(21, 20, 18, 0.16);
  --rule-2: rgba(21, 20, 18, 0.08);
  --serif: "Songti SC", "Noto Serif CJK SC", "Source Han Serif SC", "STSong", "SimSun", serif;
  --sans: "PingFang SC", "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", system-ui, sans-serif;
  --mono: "SFMono-Regular", "Cascadia Mono", "Roboto Mono", Menlo, Consolas, monospace;
  min-height: 100vh;
  background: var(--paper);
  color: var(--ink);
  font-family: var(--sans);
  line-height: 1.7;
  -webkit-font-smoothing: antialiased;
  box-sizing: border-box;
}
.k-home *,
.k-home *::before,
.k-home *::after {
  box-sizing: border-box;
}
.k-home h1,
.k-home h2,
.k-home h3,
.k-home p,
.k-home dl,
.k-home dd,
.k-home ol,
.k-home ul {
  margin: 0;
  padding: 0;
}
.k-home a {
  color: inherit;
  text-decoration: none;
}
.k-home .wrap {
  max-width: 1160px;
  margin: 0 auto;
  padding: 0 40px;
}

/* masthead */
.k-home .masthead {
  border-bottom: 1.5px solid var(--ink);
  background: var(--paper);
}
.k-home .masthead .wrap {
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  align-items: end;
  height: 76px;
  padding-bottom: 14px;
}
.k-home .wordmark {
  display: flex;
  align-items: center;
  gap: 14px;
  font-family: var(--serif);
  font-size: 21px;
  letter-spacing: 0.06em;
}
.k-home .wordmark svg {
  width: 17px;
  height: 17px;
  color: var(--ink);
}
.k-home .wordmark small {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.22em;
  color: var(--ink-3);
  margin-left: 2px;
}
.k-home .mast-meta {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.18em;
  color: var(--ink-3);
  text-transform: uppercase;
  text-align: center;
  padding-bottom: 4px;
}
.k-home .mast-nav {
  display: flex;
  justify-content: flex-end;
  gap: 26px;
  font-size: 11.5px;
  color: var(--ink-2);
}
.k-home .mast-nav a {
  white-space: nowrap;
  border-bottom: 1px solid transparent;
  padding-bottom: 2px;
}
.k-home .mast-nav a:hover {
  border-color: var(--ink);
}
.k-home .mast-nav a.lang {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.14em;
  color: var(--ink);
}

/* hero */
.k-home .hero {
  padding: 86px 0 74px;
  border-bottom: 1px solid var(--rule);
}
.k-home .hero .wrap {
  display: grid;
  grid-template-columns: 64px minmax(0, 1fr) 292px;
  column-gap: 48px;
}
.k-home .hero .wrap.en-grid {
  grid-template-columns: minmax(0, 1fr) 292px;
}
.k-home .vertical-quote {
  writing-mode: vertical-rl;
  text-orientation: mixed;
  font-family: var(--serif);
  font-size: 12.5px;
  letter-spacing: 0.24em;
  color: var(--ink-3);
  line-height: 2.1;
  justify-self: start;
  padding-top: 6px;
}
.k-home .vertical-quote span {
  color: var(--ink-3);
}
.k-home .quote-en {
  max-width: 560px;
  font-family: var(--serif);
  font-size: 13px;
  line-height: 1.95;
  color: var(--ink-3);
  margin-bottom: 26px;
}
.k-home .eyebrow {
  font-family: var(--mono);
  font-size: 9.5px;
  letter-spacing: 0.26em;
  color: var(--ink-3);
  text-transform: uppercase;
  margin-bottom: 30px;
}
.k-home h1 {
  font-family: var(--serif);
  font-weight: 600;
  font-size: clamp(52px, 6.6vw, 96px);
  line-height: 1.08;
  letter-spacing: -0.015em;
}
.k-home .lede {
  font-size: 15.5px;
  line-height: 2;
  color: var(--ink-2);
  margin-top: 30px;
  max-width: 560px;
}
.k-home .lede strong {
  font-weight: 600;
  color: var(--ink);
}
.k-home .links {
  display: flex;
  gap: 36px;
  margin-top: 34px;
}
.k-home .link {
  font-size: 13px;
  border-bottom: 1px solid var(--ink);
  padding-bottom: 3px;
  display: inline-flex;
  gap: 8px;
  align-items: baseline;
}
.k-home .link span {
  font-family: var(--mono);
  font-size: 10px;
  color: var(--ink-3);
}
.k-home .link:hover {
  color: var(--ink-2);
  border-color: var(--ink-3);
}
.k-home .prop {
  border-left: 1px solid var(--rule);
  padding-left: 28px;
  align-self: start;
  margin-top: 44px;
}
.k-home .prop .cap {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.22em;
  color: var(--ink-3);
  text-transform: uppercase;
  margin-bottom: 22px;
}
.k-home .prop dl {
  display: grid;
  grid-template-columns: max-content 1fr;
  column-gap: 18px;
  row-gap: 18px;
  font-size: 12.5px;
  line-height: 1.75;
}
.k-home .prop dt {
  font-family: var(--serif);
  font-size: 14px;
  color: var(--ink);
}
.k-home .prop dd {
  color: var(--ink-3);
}
.k-home .prop dd b {
  color: var(--ink-2);
  font-weight: 500;
}

/* sections */
.k-home section {
  border-bottom: 1px solid var(--rule);
}
.k-home .sec {
  padding: 74px 0;
}
.k-home .sec .wrap {
  display: grid;
  grid-template-columns: 180px minmax(0, 1fr);
  column-gap: 48px;
}
.k-home .side {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.22em;
  color: var(--ink-3);
  text-transform: uppercase;
  padding-top: 8px;
}
.k-home .side b {
  display: block;
  font-family: var(--serif);
  font-size: 26px;
  font-weight: 400;
  letter-spacing: 0.06em;
  color: var(--ink);
  margin-bottom: 10px;
}
.k-home h2 {
  font-family: var(--serif);
  font-weight: 600;
  font-size: clamp(30px, 3.4vw, 46px);
  line-height: 1.3;
  letter-spacing: -0.005em;
  max-width: 800px;
}
.k-home .sub {
  font-size: 14px;
  color: var(--ink-3);
  margin-top: 16px;
  max-width: 620px;
  line-height: 1.95;
}

/* six-line colophon (aligned to the logo proportions) */
.k-home .empty {
  display: grid;
  grid-template-columns: minmax(0, 1.1fr) minmax(0, 0.9fr);
  gap: 56px;
  align-items: start;
  margin-top: 48px;
}
.k-home .empty-text {
  font-family: var(--serif);
  font-size: clamp(18px, 2vw, 24px);
  line-height: 1.8;
}
.k-home .empty-text p + p {
  margin-top: 18px;
}
.k-home .empty-text small {
  display: block;
  font-family: var(--sans);
  font-size: 12.5px;
  line-height: 1.95;
  color: var(--ink-3);
  margin-top: 18px;
}
.k-home .yao {
  position: relative;
  height: 220px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
}
.k-home .yao-row {
  width: 100%;
  height: 2px;
  background: var(--ink);
  opacity: 0.82;
}
.k-home .yao-row.left {
  background: linear-gradient(to right, var(--ink) 0 20%, transparent 20% 38%, var(--ink) 38% 100%);
}
.k-home .yao-row.center {
  background: linear-gradient(to right, var(--ink) 0 41%, transparent 41% 59%, var(--ink) 59% 100%);
}
.k-home .yao-row.right {
  background: linear-gradient(to right, var(--ink) 0 62%, transparent 62% 80%, var(--ink) 80% 100%);
}
.k-home .yao-cap {
  position: absolute;
  right: 0;
  bottom: -22px;
  font-family: var(--mono);
  font-size: 8.5px;
  letter-spacing: 0.16em;
  color: var(--ink-3);
}

/* ledger */
.k-home .ledger {
  margin-top: 44px;
  border-top: 1.5px solid var(--ink);
}
.k-home .ledger-row {
  display: grid;
  grid-template-columns: 140px 1fr 300px;
  column-gap: 24px;
  padding: 18px 0;
  border-bottom: 1px solid var(--rule-2);
  align-items: baseline;
}
.k-home .ledger-row.head {
  font-family: var(--mono);
  font-size: 8.5px;
  letter-spacing: 0.2em;
  color: var(--ink-3);
  text-transform: uppercase;
  padding: 10px 0;
}
.k-home .ledger-row .k {
  font-family: var(--serif);
  font-size: 17px;
}
.k-home .ledger-row .v {
  font-size: 13.5px;
  color: var(--ink-2);
  line-height: 1.8;
}
.k-home .ledger-row .v code {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--ink);
  background: rgba(21, 20, 18, 0.05);
  padding: 1px 5px;
}
.k-home .ledger-row .h {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--ink-3);
}
.k-home .ledger-row.total {
  border-bottom: 1.5px solid var(--ink);
}
.k-home .ledger-row.total .k {
  font-size: 20px;
}
.k-home .ledger-row.total .h {
  color: var(--ink);
  font-family: var(--serif);
  font-size: 16px;
}

/* boundaries */
.k-home .bounds {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 0;
  margin-top: 46px;
  border-top: 1.5px solid var(--ink);
}
.k-home .bound {
  padding: 26px 26px 30px 0;
  border-right: 1px solid var(--rule-2);
}
.k-home .bound:last-child {
  border-right: 0;
  padding-right: 0;
}
.k-home .bound .n {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.2em;
  color: var(--ink-3);
  margin-bottom: 16px;
}
.k-home .bound h3 {
  font-family: var(--serif);
  font-size: 21px;
  font-weight: 600;
  margin-bottom: 12px;
}
.k-home .bound p {
  font-size: 12.5px;
  color: var(--ink-3);
  line-height: 1.95;
}
.k-home .footnote {
  margin-top: 34px;
  font-family: var(--mono);
  font-size: 10px;
  color: var(--ink-3);
  letter-spacing: 0.04em;
}

/* verification */
.k-home .evidence {
  margin-top: 44px;
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  border-top: 1.5px solid var(--ink);
}
.k-home .ev {
  padding: 22px 20px 26px 0;
  border-right: 1px solid var(--rule-2);
}
.k-home .ev:last-child {
  border-right: 0;
}
.k-home .ev .n {
  font-family: var(--mono);
  font-size: 8.5px;
  letter-spacing: 0.18em;
  color: var(--ink-3);
  margin-bottom: 14px;
}
.k-home .ev .big {
  font-family: var(--serif);
  font-size: 22px;
  line-height: 1.35;
}
.k-home .ev .small {
  font-family: var(--mono);
  font-size: 10px;
  color: var(--ink-3);
  margin-top: 8px;
  line-height: 1.8;
}

/* docs */
.k-home .docs {
  padding: 66px 0 78px;
}
.k-home .docs .wrap {
  display: grid;
  grid-template-columns: 180px minmax(0, 1fr);
  column-gap: 48px;
}
.k-home .doc-list {
  border-top: 1.5px solid var(--ink);
}
.k-home .doc {
  display: grid;
  grid-template-columns: 52px 1fr auto;
  gap: 20px;
  align-items: baseline;
  padding: 18px 0;
  border-bottom: 1px solid var(--rule-2);
}
.k-home .doc .num {
  font-family: var(--mono);
  font-size: 9.5px;
  color: var(--ink-3);
}
.k-home .doc .t {
  display: block;
  font-family: var(--serif);
  font-size: 19px;
}
.k-home .doc .d {
  display: block;
  font-size: 12px;
  color: var(--ink-3);
  margin-top: 3px;
}
.k-home .doc .arrow {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--ink-3);
}
.k-home .doc:hover .t {
  color: var(--ink-2);
}
.k-home .doc:hover .arrow {
  color: var(--ink);
}

/* footer */
.k-home footer {
  border-top: 1.5px solid var(--ink);
  padding: 40px 0 48px;
}
.k-home footer .wrap {
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  align-items: end;
}
.k-home .colophon {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.16em;
  color: var(--ink-3);
  line-height: 2;
}
.k-home .colophon b {
  color: var(--ink);
  font-weight: 500;
}
.k-home .seal {
  font-family: var(--serif);
  font-size: 13px;
  letter-spacing: 0.3em;
  color: var(--ink-3);
  text-align: center;
}
.k-home .right-col {
  text-align: right;
}

@media (max-width: 980px) {
  .k-home .hero .wrap,
  .k-home .hero .wrap.en-grid {
    grid-template-columns: 1fr;
  }
  .k-home .vertical-quote {
    display: none;
  }
  .k-home .prop {
    margin-top: 44px;
    border-left: 0;
    border-top: 1px solid var(--rule);
    padding-left: 0;
    padding-top: 24px;
  }
  .k-home .sec .wrap,
  .k-home .docs .wrap {
    grid-template-columns: 1fr;
    gap: 22px;
  }
  .k-home .empty {
    grid-template-columns: 1fr;
    gap: 36px;
  }
  .k-home .ledger-row {
    grid-template-columns: 100px 1fr;
    row-gap: 6px;
  }
  .k-home .ledger-row .h {
    grid-column: 2;
  }
  .k-home .bounds {
    grid-template-columns: 1fr;
  }
  .k-home .bound {
    border-right: 0;
    border-bottom: 1px solid var(--rule-2);
    padding-right: 0;
  }
  .k-home .evidence {
    grid-template-columns: repeat(2, 1fr);
  }
  .k-home .ev:nth-child(2n) {
    border-right: 0;
  }
  .k-home .masthead .wrap {
    grid-template-columns: 1fr auto;
  }
  .k-home .mast-meta {
    display: none;
  }
}
@media (max-width: 620px) {
  .k-home .wrap {
    padding: 0 22px;
  }
  .k-home .masthead .wrap {
    grid-template-columns: 1fr;
    height: auto;
    row-gap: 12px;
    padding-top: 16px;
    padding-bottom: 14px;
  }
  .k-home .mast-nav {
    justify-content: flex-start;
    flex-wrap: wrap;
    gap: 18px;
    font-size: 11px;
  }
  .k-home h1 {
    font-size: 44px;
  }
  .k-home .ledger-row {
    grid-template-columns: 1fr;
    row-gap: 6px;
  }
  .k-home .ledger-row.head {
    display: none;
  }
  .k-home .ledger-row .h {
    grid-column: auto;
  }
  .k-home .evidence {
    grid-template-columns: 1fr;
  }
  .k-home .ev {
    border-right: 0;
  }
  .k-home footer .wrap {
    grid-template-columns: 1fr;
    gap: 18px;
  }
  .k-home .right-col {
    text-align: left;
  }
  .k-home .seal {
    text-align: left;
  }
}
</style>
