<template>
  <section class="confirmation" aria-label="本次知乎草稿">
    <strong>{{report.title}}</strong>
    <p>交付任务 #{{report.job}} · 冻结稿 #{{report.draft}} · 第 {{report.revision}} 版</p>
    <p>接收账号　{{account.name}}（{{account.user_id}}）<br>分稿别名　{{report.account}}</p>
    <p>模拟排期　{{planned}}（北京时间）<br>确认后，助手即可开始保存知乎草稿。</p>
    <p>正文配图 {{report.images.length}} 处，共 {{report.unique_images}} 张。</p>
    <p v-if="report.reader_url">文末读者链接　{{report.reader_url}}
      <br v-if="report.link_mode==='short'"><span v-if="report.link_mode==='short'">登记的目标文章　{{report.destination}}</span>
    </p>
    <p v-else>本稿按审核设置省略文末延伸阅读链接。</p>
    <details @toggle="showBody=$event.target.open"><summary>查看将发送的正文与配图</summary>
      <ReprintFrame v-if="showBody" :html="report.html" title="本次发送的冻结正文"/>
    </details>
    <p v-if="restored">请确认已在此账号的草稿箱查找，尚未找到这篇稿件。</p>
  </section>
</template>
<script setup>
import {ref} from 'vue'
import ReprintFrame from './ReprintFrame.vue'
defineProps({report:Object,account:Object,planned:String,restored:Boolean})
const showBody=ref(false)
</script>
<style scoped>
.confirmation{max-height:60vh;overflow:auto;overflow-wrap:anywhere}.confirmation p{margin:.55em 0}summary{cursor:pointer}.confirmation iframe{height:36vh}
</style>
