<template>
  <div>
    <el-alert v-if="problem" :title="problem" type="warning" :closable="false"/>
    <el-descriptions v-if="status" :column="1" border>
      <el-descriptions-item label="调度器">{{problem?'连接中断':status.worker_online?'在线':'心跳过期或尚未启动'}}</el-descriptions-item>
      <el-descriptions-item v-if="status.api_build && status.worker_build" label="后台版本">
        {{status.api_build===status.worker_build?'管理台与调度器一致':'版本不同，请双击「启动稿序」更新'}}
        <small>管理台 {{status.api_build.slice(0,10)}} · 调度器 {{status.worker_build.slice(0,10)}}</small>
      </el-descriptions-item>
      <el-descriptions-item label="数据截至">{{date(status.checked)}}</el-descriptions-item>
      <el-descriptions-item label="最后心跳">{{date(status.worker_seen)}}</el-descriptions-item>
      <el-descriptions-item label="最近领取任务">{{date(status.last_claim)}}</el-descriptions-item>
      <el-descriptions-item label="已到期 / 执行中">{{status.queue.due}} / {{status.queue.running}}</el-descriptions-item>
      <el-descriptions-item label="晚于原排期的待交付稿">{{status.queue.deferred_deliveries}}</el-descriptions-item>
      <el-descriptions-item label="最早待处理时间">{{date(status.queue.oldest_due)}}</el-descriptions-item>
      <el-descriptions-item label="下一项排期或重试">{{date(status.queue.next_due)}}</el-descriptions-item>
      <el-descriptions-item label="失败步骤 / 租约到期">{{status.queue.failed}} / {{status.queue.expired}}</el-descriptions-item>
      <el-descriptions-item label="待核对的平台草稿回执">{{status.unknown_drafts}}</el-descriptions-item>
    </el-descriptions>
    <span v-else-if="!problem">正在读取服务状态…</span>
    <section v-if="status?.backup" class="backup">
      <h3>每日备份</h3>
      <p>{{status.backup.enabled?'每天 '+clock(status.backup.minute)+'（北京时间）':'未启用'}}
        <el-button link @click="editBackup">设置</el-button></p>
      <p>备份目录　{{status.backup.destination}}</p>
      <p v-if="status.backup.enabled">本次状态　{{backupStates[status.backup.state]}}
        <span v-if="status.backup.retry_at"> · 下次重试 {{date(status.backup.retry_at)}}</span></p>
      <p>最近成功　{{status.backup.last_success?date(status.backup.last_success):'尚无自动备份记录'}}</p>
      <p v-if="status.backup.last_bundle">恢复目录　{{status.backup.last_bundle}}</p>
      <el-alert v-if="status.backup.message" :title="status.backup.message" :closable="false" type="warning"/>
      <form v-if="editing" @submit.prevent="saveBackup">
        <p v-if="status.backup.configured_destination!==status.backup.destination">保存后使用　{{status.backup.configured_destination}}</p>
        <label><input type="checkbox" v-model="enabled"> 开启每日备份</label>
        <label>北京时间 <input type="time" required v-model="at"></label>
        <p>自动备份保留最近 {{status.backup.keep}} 份。新备份完成后清理更早的自动副本。</p>
        <el-button native-type="submit" type="primary" :loading="saving">保存</el-button>
        <el-button :disabled="saving" @click="editing=false">返回</el-button>
        <p v-if="saveError" role="alert">{{saveError}}</p>
      </form>
    </section>
  </div>
</template>
<script setup>
import {ref,onMounted,onBeforeUnmount} from 'vue'
const props=defineProps({api:Function,date:Function})
const status=ref(null),problem=ref('')
const editing=ref(false),enabled=ref(false),at=ref('03:00'),revision=ref(0),saving=ref(false),saveError=ref('')
const backupStates={waiting:'等待备份时间',running:'正在备份',success:'已完成',failed:'失败'}
const clock=minute=>[Math.floor(minute/60),minute%60].map(n=>String(n).padStart(2,'0')).join(':')
function editBackup(){
  const backup=status.value.backup
  enabled.value=Boolean(backup.enabled);at.value=clock(backup.minute);revision.value=backup.revision
  saveError.value='';editing.value=true
}
async function saveBackup(){
  if(saving.value)return
  saving.value=true;saveError.value=''
  try{
    const [hour,minute]=at.value.split(':').map(Number)
    const backup=await props.api('backup-settings',{enabled:enabled.value,minute:hour*60+minute,revision:revision.value})
    if(!stopped){status.value.backup=backup;editing.value=false}
  }catch(e){saveError.value=e.message}
  finally{saving.value=false}
}
let stopped=false,timer
async function poll(){
  try{const result=await props.api('runtime-status');if(!stopped){status.value=result;problem.value=''}}
  catch{if(!stopped)problem.value='无法读取本机服务，已有数据以最后读取时间为准'}
  finally{if(!stopped)timer=setTimeout(poll,5000)}
}
onMounted(poll)
onBeforeUnmount(()=>{stopped=true;clearTimeout(timer)})
</script>
<style scoped>
.backup{margin-top:16px;overflow-wrap:anywhere}.backup label{display:inline-block;margin:4px 14px 4px 0}
</style>
