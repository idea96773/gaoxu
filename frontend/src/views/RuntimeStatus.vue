<template>
  <div>
    <el-alert v-if="problem" :title="problem" type="warning" :closable="false"/>
    <el-descriptions v-if="status" :column="1" border>
      <el-descriptions-item label="调度器">{{problem?'连接中断':status.worker_online?'在线':'心跳过期或尚未启动'}}</el-descriptions-item>
      <el-descriptions-item label="数据截至">{{date(status.checked)}}</el-descriptions-item>
      <el-descriptions-item label="最后心跳">{{date(status.worker_seen)}}</el-descriptions-item>
      <el-descriptions-item label="最近领取任务">{{date(status.last_claim)}}</el-descriptions-item>
      <el-descriptions-item label="已到期 / 执行中">{{status.queue.due}} / {{status.queue.running}}</el-descriptions-item>
      <el-descriptions-item label="最早待处理时间">{{date(status.queue.oldest_due)}}</el-descriptions-item>
      <el-descriptions-item label="下一项排期或重试">{{date(status.queue.next_due)}}</el-descriptions-item>
      <el-descriptions-item label="失败步骤 / 租约到期">{{status.queue.failed}} / {{status.queue.expired}}</el-descriptions-item>
      <el-descriptions-item label="待核对的平台草稿回执">{{status.unknown_drafts}}</el-descriptions-item>
    </el-descriptions>
    <span v-else-if="!problem">正在读取服务状态…</span>
  </div>
</template>
<script setup>
import {ref,onMounted,onBeforeUnmount} from 'vue'
const props=defineProps({api:Function,date:Function})
const status=ref(null),problem=ref('')
let stopped=false,timer
async function poll(){
  try{const result=await props.api('runtime-status');if(!stopped){status.value=result;problem.value=''}}
  catch{if(!stopped)problem.value='无法读取本机服务，已有数据以最后读取时间为准'}
  finally{if(!stopped)timer=setTimeout(poll,5000)}
}
onMounted(poll)
onBeforeUnmount(()=>{stopped=true;clearTimeout(timer)})
</script>
