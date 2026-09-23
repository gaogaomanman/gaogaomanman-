import { createRouter, createWebHistory } from 'vue-router'

/**
 * 单一入口路径分区：
 *   /           导航首页（原 :8080）
 *   /dm         达梦数据库查询（原 :3001）
 *   /sqlserver  SQL Server 查询（原 :3003）
 *   /board      检测中心数据看板（原 :3005）
 *   /personnel  人员能力表梳理（原 :3007）
 *   /cma        一单一库核对（原 :3002）
 *   /progress   抽采样进度统计（任务量 vs 完成量）
 */
const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'nav', component: () => import('../views/NavView.vue') },
    { path: '/dm', name: 'dm', component: () => import('../views/DmQueryView.vue') },
    { path: '/sqlserver', name: 'sqlserver', component: () => import('../views/SqlServerView.vue') },
    { path: '/board', name: 'board', component: () => import('../views/BoardView.vue') },
    { path: '/personnel', name: 'personnel', component: () => import('../views/PersonnelView.vue') },
    { path: '/cma', name: 'cma', component: () => import('../views/CmaView.vue') },
    { path: '/progress', name: 'progress', component: () => import('../views/ProgressView.vue') },
    // 查询模板规则设置（/dm 的别名与合并折算规则，后端存档 + 版本可回滚）
    { path: '/limsrules', name: 'limsrules', component: () => import('../views/LimsRulesView.vue') },
  ],
})

export default router
