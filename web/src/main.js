import { createApp } from 'vue'
import { VueQueryPlugin, QueryClient } from '@tanstack/vue-query'
import App from './App.vue'
import './style.css'

// round-12 F1 — vue-query 全局注入,refetchOnWindowFocus + staleTime 30s
// 让"刷新响应更简便",F2/F3 后续把 useStatus 等手写 polling 全迁过来。
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: true,
      retry: 1,
    },
  },
})

createApp(App)
  .use(VueQueryPlugin, { queryClient })
  .mount('#app')
