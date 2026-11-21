<template>
  <div class="agent-runs">
    <div class="controls">
      <el-button :loading="loading" @click="load([])">刷新到最新</el-button>
      <el-button :disabled="loading || !history.length" @click="load(history.slice(0,-1))">上一页</el-button>
      <el-button :disabled="loading || !next" @click="load([...history,next])">下一页</el-button>
    </div>
    <el-alert v-if="problem" :title="problem" type="warning" :closable="false"/>
    <el-table v-loading="loading" :data="items" empty-text="暂无工具调用记录">
      <el-table-column label="调用时间（北京）" width="166"><template #default="{row}">{{date(row.started)}}</template></el-table-column>
      <el-table-column prop="tool" label="工具" min-width="155"/>
      <el-table-column label="操作对象" min-width="160"><template #default="{row}">{{row.subject || '—'}}</template></el-table-column>
      <el-table-column label="接口结果" min-width="145"><template #default="{row}">{{outcome(row)}}</template></el-table-column>
    </el-table>
  </div>
</template>
<script setup>
import {onMounted,ref} from 'vue'

const props=defineProps({api:Function,date:Function})
const items=ref([]), history=ref([]), next=ref(null), loading=ref(false), problem=ref('')

function outcome(row) {
  if (row.status_code == null) return '尚无结束记录'
  if (row.status_code >= 400) return '请求失败（HTTP '+row.status_code+'）'
  if (row.requested != null) return '重新排队 '+row.applied+' / '+row.requested+' 项'
  return '请求成功（HTTP '+row.status_code+'）'
}

async function load(pages) {
  if (loading.value) return
  loading.value=true
  problem.value=''
  try {
    const cursor=pages.at(-1)
    const result=await props.api('agent-runs'+(cursor?'?before='+cursor:''))
    items.value=result.items
    next.value=result.next
    history.value=pages
  } catch (error) {
    problem.value=error.message || '读取调用日志失败'
  } finally {
    loading.value=false
  }
}
onMounted(()=>load([]))
</script>
<style scoped>
.controls{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
.controls .el-button{margin-left:0}
</style>
