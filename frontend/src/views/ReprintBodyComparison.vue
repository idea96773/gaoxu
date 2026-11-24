<template>
  <section aria-label="原 HTML 正文对照">
    <p v-if="!report">此版本未保存原 HTML 正文对照记录。</p>
    <p v-else-if="report.status!=='compared'">{{report.status==='no_html'?'下载目录中没有 HTML，正文按 Markdown 送审。':'下载的 HTML 无法唯一定位公众号正文，本篇需人工比对。'}}</p>
    <template v-else>
      <p v-if="!report.difference">原 HTML 与转载稿正文文字一致（忽略空白，共 {{report.source_chars}} 字符）。</p>
      <details v-else open>
        <summary>正文有差异 · 首处在第 {{report.difference.position}} 个非空白字符</summary>
        <p v-if="report.difference.context">前文　{{report.difference.context}}</p>
        <p>原 HTML　{{report.difference.source || '（此处已结束）'}}</p>
        <p>转载稿　{{report.difference.draft || '（此处已结束）'}}</p>
      </details>
      <p v-if="report.structure_counts">排版结构有变化。{{structure || '标题、列表等元素的排列顺序有变化。'}}</p>
      <p v-if="report.break_example">段落或换行位置有差异。转载稿缺少 {{report.missing_breaks}} 处，多出 {{report.extra_breaks}} 处。
        首处在「{{report.break_example.before}}」与「{{report.break_example.after}}」之间，{{report.break_example.missing?'原 HTML 在此断开。':'转载稿在此断开。'}}</p>
      <p v-if="report.restored_breaks">已按原 HTML 恢复 {{report.restored_breaks}} 处段落或换行。</p>
      <p v-if="report.restored_bold">已按原 HTML 恢复 {{report.restored_bold}} 处加粗。</p>
    </template>
  </section>
</template>
<script setup>
import {computed} from 'vue'
const props=defineProps({report:Object})
const names={h1:'一级标题',h2:'二级标题',h3:'三级标题',h4:'四级标题',h5:'五级标题',h6:'六级标题',ul:'无序列表',ol:'有序列表',li:'列表项',
  blockquote:'引用块',table:'表格',tr:'表格行',th:'表头单元格',td:'数据单元格',sub:'下标',sup:'上标',pre:'预排文本块'}
const structure=computed(()=>Object.entries(props.report?.structure_counts || {}).map(([key,[before,after]])=>
  (names[key] || key)+' '+before+' → '+after).join('，'))
</script>
<style scoped>
section{overflow-wrap:anywhere}details p{margin:.35em 0}summary{cursor:pointer}
</style>
