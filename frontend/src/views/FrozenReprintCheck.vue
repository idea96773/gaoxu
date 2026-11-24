<template>
  <section aria-label="冻结稿本机核对">
    <el-button :loading="loading" @click="check">{{ report ? '重新核对冻结稿' : '本机核对并预览冻结稿' }}</el-button>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="report">
      <p role="status">冻结版本、账号和排期一致。正文配图 {{ report.images.length }} 处，共 {{ report.unique_images }} 张归档图片。</p>
      <p v-if="report.link_mode === 'short'">文末短链接 {{ report.reader_url }}<br>登记的目标文章 {{ report.destination }}</p>
      <p v-else>{{ report.reader_url ? '文末公众号链接　' + report.reader_url : '本稿按审核设置保留原文链接，未追加延伸阅读。' }}</p>
      <ReprintBodyComparison :report="report.body_comparison" />
      <details open>
        <summary>查看正文排版与配图</summary>
        <h3>{{ report.title }}</h3>
        <ReprintFrame title="冻结稿正文与配图" :html="report.html" />
      </details>
    </template>
  </section>
</template>

<script setup>
import { ref } from 'vue'
import { readFrozenReprint } from '../api/frozenReprint'
import ReprintFrame from './ReprintFrame.vue'
import ReprintBodyComparison from './ReprintBodyComparison.vue'

const props = defineProps({ job: Object, api: Function })
const report = ref(null)
const error = ref('')
const loading = ref(false)

async function check() {
  loading.value = true
  error.value = ''
  report.value = null
  try {
    report.value = await readFrozenReprint(props.api, props.job)
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
section { margin: 14px 0; overflow-wrap: anywhere }
summary { cursor: pointer }
[role="alert"] { color: var(--el-color-danger) }
</style>
