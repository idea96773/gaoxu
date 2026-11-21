<template>
  <p v-if="info?.error" role="alert">审核记录核对失败：{{ info.error }}</p>
  <p v-else-if="info">
    审核通过稿 · 第 {{ info.revision }} 版 · {{ info.approved_at == null ? '历史记录未保存通过时间' : date(info.approved_at) }}
    <small>来源批次 {{ info.batch }} · 第 {{ info.line }} 篇 · 文章 #{{ info.row_id }}</small>
  </p>
</template>

<script setup>
defineProps({ info: Object, date: Function })
</script>

<style scoped>
small { display: block; overflow-wrap: anywhere }
[role="alert"] { color: var(--el-color-danger) }
</style>
