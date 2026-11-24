<template>
  <details @toggle="onToggle">
    <summary>比对从知乎编辑器复制的正文</summary>
    <p>在指定账号的草稿编辑器内，从署名选到正文末尾，含配图和文末链接。标题单独核对。</p>
    <textarea aria-label="粘贴草稿正文进行本地比对" rows="2" readonly
      placeholder="点这里，按 Ctrl+V 粘贴正文" @paste.prevent="paste" />
    <div aria-live="polite">
      <p v-if="error" role="alert">{{error}}</p>
      <template v-if="result">
        <p>{{result.aligned ? '正文文字及顺序一致（忽略空白）。' : '正文文字存在差异。'}}</p>
        <div v-if="result.difference" class="difference">
          <div>首处差异前文　{{result.difference.context || '文章开头'}}</div>
          <div>冻结稿　{{result.difference.expected || '已结束'}}</div>
          <div>复制稿　{{result.difference.actual || '已结束'}}</div>
        </div>
        <template v-if="result.rich">
          <p v-if="result.aligned">段落与换行边界相较冻结稿减少 {{result.missingBreaks}} 处，增加 {{result.extraBreaks}} 处。</p>
          <p>配图标记　冻结稿 {{result.expectedImages}} 处，复制稿 {{result.actualImages}} 处。<span v-if="result.movedImage">第 {{result.movedImage}} 处位置有变化。</span><span v-else-if="result.movedImage===0 && result.actualImages">文字中的位置一致。</span>请在编辑器核对图片内容和显示效果。</p>
          <p v-if="result.footerMatches!==undefined">文末链接　{{result.footerMatches ? '链接文字及地址与冻结稿一致。' : '未匹配到冻结稿末尾的链接文字及地址，请检查平台是否移除或改写了链接。'}}</p>
          <p v-else>此冻结稿设置为省略附加链接。</p>
        </template>
        <p v-else>此次复制仅含纯文本。请在编辑器核对排版、配图和实际链接地址。</p>
      </template>
    </div>
  </details>
</template>

<script setup>
import {ref} from 'vue'
import {compareClipboard} from '../utils/draftClipboard'

const props = defineProps({snapshot: Object})
const result = ref(null), error = ref('')
function onToggle(event) {
  if (!event.target.open) { result.value = null; error.value = '' }
}
function paste(event) {
  result.value = null
  error.value = ''
  try {
    const clipboard = event.clipboardData
    result.value = compareClipboard(props.snapshot, clipboard?.getData('text/html') || '', clipboard?.getData('text/plain') || '')
  } catch (e) { error.value = e.message }
}
</script>

<style scoped>
summary{cursor:pointer}textarea{box-sizing:border-box;width:100%;padding:8px;font:inherit}
.difference{overflow-wrap:anywhere;border-left:3px solid var(--el-color-warning);padding-left:10px}
</style>
