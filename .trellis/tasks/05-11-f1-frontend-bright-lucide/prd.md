# F1 — 前端明亮化 + 去 emoji + Lucide + vue-query

> 父任务：`05-11-upstream-align-register-multimail-frontend-refresh`（Decision Q4）
> 调研依据：`research/frontend-bright-icon.md`（lucide-vue-next + Bright v1 + vue-query）

## Goal
把 `web/` (Vue 3 + Tailwind 3.4) 从深色玻璃风（`ink.950 = #070912`）整体翻转到 **AutoTeam Bright v1**（白底 `#fafafa` / 卡片 `#ffffff` / 边 `#e5e5e5` / 主文字 `#0a0a0a` / indigo `#4f46e5` 强调），同时把 21 个组件里的 emoji 全部换成 `lucide-vue-next` SVG icon，加一层 `@tanstack/vue-query` 让"刷新响应更简便"，并在自路由切换处接入 Vue 原生 `<Transition>`。

## Constraints
- **没有可用 team 母账号 / 子账号** —— 浏览器只能看 UI 自身，不能触发任何真实 rotate / 注册 / OAuth。
- 不动 backend。
- 不上 SSE 实时进度（F2/F3 任务）。
- 不升 Tailwind v3 → v4。
- npm（`package-lock.json`）不是 pnpm —— 但用户指令是 pnpm，先 `pnpm add`，失败回退 `npm i`。

## Approach

### 1. tailwind.config.js
- 删除 `ink.950..600 = #07-#2A` 深色 palette，重定义 `ink` 为中性灰阶（950→0a0a0a / 700→262626 / 500→525252 / 300→a3a3a3 / 100→f5f5f5）。
- 加 `canvas / surface / hairline` 语义色 token。
- `boxShadow` 删 `glow-*`，加 `card / card-hover / ring-accent` 三档轻盈阴影。
- 保留 keyframes/animation（`pulse-dot / shimmer / toast-* / rise`）— 复用现有动画时序。

### 2. web/src/style.css
- 删 `color-scheme: dark` + 整套深色 mesh gradient + `body::before` 噪点 overlay。
- `body bg → #fafafa`。
- `.glass` / `.glass-soft` 重定义为白色卡片（白底 + 1px hairline + 轻微 elevation）。
- `.shimmer-bg` 改为浅色 shimmer。
- `.row-hoverable::before` accent 色保留，整体改为更轻的 indigo tint。
- 滚动条改浅灰。
- **关键**：加一层 `@layer utilities` 兼容覆盖，把现有 21 组件硬编码的深色 utility（`text-white` / `text-gray-100..600` / `bg-white/[0.0X]` / `border-white/[X]` / `text-{tone}-300/200`）就地翻转到明亮等价值——**避免逐文件改 21 套类名**，零回归风险，单点可控。
- 例外：`AtButton` 的 primary/danger 渐变需要白色前景 → 用 `text-[#ffffff]` 任意值绕过覆盖。

### 3. main.js
- 注入 `VueQueryPlugin` + `QueryClient`，`refetchOnWindowFocus: true` / `staleTime: 30s` / `retry: 1`。

### 4. App.vue
- 把 8 路 v-if 动态组件包到 `<Transition mode="out-in" name="page">` 里，配 CSS 120-180ms ease-out + 4px 上滑过渡。
- 同时给主登录页（v-else-if !authenticated）改文字色（深色 → 默认 ink）。

### 5. Emoji → Lucide
覆盖 6 文件已知 emoji 出现位置（Sidebar/TaskPanel/Settings/SetupPage/UsabilityCell/useStatus.js 注释区不计）：
- Sidebar.vue 8 个导航 emoji + 2 个动作 emoji
- TaskPanel.vue 9 个任务 emoji
- Settings.vue 2 处 ⚠️（保留作 inline 文本无副作用，可不动）
- UsabilityCell.vue 已是手写 SVG，保留
- 主映射：`📊 → ChartPie / 👥 → Users / 🔁 → RefreshCw / 🔄 → RotateCw / 🔐 → KeyRound / 📜 → Scroll / 📋 → ClipboardList / ⚙ → Settings / ➕ → Plus / 🪙 → Coins / 🧹 → Brush / ⬇ → Download / 🚪 → LogOut / 🛑 → OctagonAlert`。

### 6. useStatus.js
- 不重写，**保留全部现有 API**。
- 只调整：`STATUS_STYLES` 里 dark `text-{x}-300` / `bg-{x}-500/[0.08]` 等读起来糊在白底上的（其实 §2 兼容层已经全局翻转，这里可不动）。

### 7. 验收
- `cd web && npm run build` 无 warning。
- `npm run dev` 起服务，浏览器访问看：白底 / 卡片可读 / Sidebar Lucide icon / 切页 fade。
- 浏览器 console 无 vue warning。
- 截图：dashboard / sidebar / setup / oauth 至少 4 张存到 `screenshots/`。

## Risks
| 风险 | 缓解 |
|---|---|
| 21 组件全量替换深色 utility 风险高 | 用 `@layer utilities` 单点翻转，避免逐文件 sed |
| `text-white` 在 AtButton primary 渐变上必须保留白色 | 用 `text-[#fff]` 任意值绕过覆盖 |
| `lucide-vue-next` tree-shake 失效爆体积 | 强制 `import { Specific } from 'lucide-vue-next'`，零 `* as` |
| 浏览器实测看不到完整功能（无账号） | 至少看 sidebar/dashboard/setup 静态页，验证视觉与 Lucide 渲染 |
| `@tanstack/vue-query` 引入但本轮不迁现有 composable | 仅做 plugin 注入 + `useStatus.js` 暴露一个 `useQueryStatus()` helper（可选），不强制改 21 组件，留 F2 收尾 |

## Acceptance Criteria
- [ ] tailwind.config.js 重写为 Bright v1
- [ ] style.css 重写（含 @layer utilities 兼容覆盖）
- [ ] main.js 接入 VueQueryPlugin
- [ ] App.vue 加 Transition
- [ ] Sidebar / TaskPanel emoji → Lucide
- [ ] AtButton 渐变 variant 保留白前景
- [ ] `npm run build` 通过
- [ ] 4 张浏览器截图归档

## Out of Scope
- 把 `useStatus.js` 全部迁到 vue-query（F2/F3 责任）
- SSE rotate 实时进度（F2）
- 删 ToastHost backdrop-blur（保留浮层玻璃感）
- 重写 21 组件每个 utility 类名（用兼容层兜底）

## Definition of Done
- `npm run build` 全绿
- commit `feat(round-12 F1): bright theme + lucide + vue-query`
- 父任务 prd.md F1 checkbox 由后续操作员勾上
