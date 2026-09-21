<template>
  <!--
    统一入口：不做任何全局外壳（不新增顶栏/侧栏），保证各模块页面与原有视觉完全一致。
    每个视图自带原有的 header / 布局 / 样式。
  -->
  <router-view />
</template>
