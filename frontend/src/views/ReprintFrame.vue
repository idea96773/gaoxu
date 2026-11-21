<template>
  <iframe :title="title" sandbox="" referrerpolicy="no-referrer" :srcdoc="frame"></iframe>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({ html: String, title: String })
const policy = "default-src 'none'; img-src " + location.origin + "; style-src 'unsafe-inline'"
const styles = 'body{max-width:680px;margin:18px auto;padding:0 16px;color:#242830;font:16px/1.8 system-ui}img{max-width:100%;height:auto}h1{font-size:24px}a{color:#1766aa}p{overflow-wrap:anywhere}'
const frame = computed(() => '<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="' +
  policy + '"><style>' + styles + '</style>' + props.html)
</script>

<style scoped>
iframe { width:100%; height:65vh; border:0 }
</style>
