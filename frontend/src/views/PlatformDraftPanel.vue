<template>
  <section class="draft">
    <h3>知乎草稿箱</h3>
    <p>分稿别名　{{job.job.account}}　<el-button link @click="$emit('accounts',job.job.account)">管理知乎账号</el-button></p>
    <p>{{recipientSource==='binding'?'本次保存将锁定账号':'本稿指定账号'}}　{{recipient?.name || '待绑定'}}
      <small v-if="recipient">账号 ID　{{recipient.user_id}}</small></p>
    <BridgeConnection :bridge="bridge" :account="account" :target="recipient" :job-id="job.job.id"/>
    <div v-if="grant" class="scope">
      <p>转载授权　<strong>{{grant.scope==='local'?'仅本地试稿':'可用于指定知乎账号的草稿箱'}}</strong>
        <small v-if="grant.scope==='zhihu_draft'">获准账号 ID　{{grant.user_id}}</small></p>
      <el-button v-if="recipient && !permitted && control!=='cancelled' && job.job.state!=='cancelled'" link type="primary" :disabled="busy" @click="setScope('zhihu_draft')">记录此账号的草稿授权</el-button>
      <el-button v-if="grant.scope==='zhihu_draft'" link :disabled="busy" @click="setScope('local')">改为仅本地试稿</el-button>
      <details v-if="history.length"><summary>近期授权记录</summary>
        <small v-for="(event,index) in history" :key="index">{{date(event.confirmed)}}　{{event.scope==='local'?'仅本地试稿':'知乎草稿 · '+event.user_id}}</small>
      </details>
    </div>
    <p v-if="record">目标账号　{{record.name}}　·　{{labels[record.state]}}
      <a v-if="record.url" :href="record.url" target="_blank" rel="noopener noreferrer">打开知乎草稿</a>
    </p>
    <p v-if="record?.reason">{{record.reason}}</p>
    <p v-if="reusableImages && ['queued','preparing','failed','cancelled'].includes(record?.state)">
      本稿可沿用 {{reusableImages}} 张已转存配图，共 {{uniqueImages}} 张不同配图。
    </p>
    <p v-if="!record && !recipient">在「管理知乎账号」中将分稿别名与指定登录账号绑定。</p>
    <el-button v-if="canSend" type="primary" :loading="busy" @click="enqueue">保存到 {{account.name}} 的草稿箱</el-button>
    <el-button v-if="record && ['queued','preparing'].includes(record.state)" :disabled="busy" @click="change({action:'cancel'})">取消草稿任务</el-button>
    <DraftInspection v-if="record && ['saved','unknown','needs_review','verified'].includes(record.state)"
      :key="record.id" :api="api" :path="path" :record="record" :entries="inspections" :snapshot="job.delivery.snapshot"
      :date="date" @refresh="load" @recorded="$emit('inspected')" />
    <el-alert v-if="error" :title="error" type="error" :closable="false"/>
  </section>
</template>
<script setup>
import {ref,computed,onMounted,onBeforeUnmount,watch} from 'vue'
import {ElMessageBox} from 'element-plus'
import DraftInspection from './DraftInspection.vue'
import BridgeConnection from './BridgeConnection.vue'
const props=defineProps({api:Function,job:Object,bindingsVersion:Number,date:Function})
defineEmits(['accounts','inspected'])
const recipient=ref(null),recipientSource=ref(''),control=ref('')
const grant=ref(null),history=ref([]),permitted=ref(false)
const reusableImages=ref(0)
const uniqueImages=computed(()=>new Set(props.job.delivery.snapshot.images).size)
const bridge=ref(null),account=ref(null),record=ref(null),inspections=ref([]),busy=ref(false),error=ref('')
const labels={queued:'等待保存',preparing:'准备配图',sending:'正在请求保存',saved:'已找到草稿，待核对',verified:'已人工核对',needs_review:'核对发现问题',unknown:'回执不确定，需核对草稿箱',failed:'准备失败',cancelled:'已取消'}
const path='jobs/'+props.job.job.id+'/platform-draft'
const canSend=computed(()=>!bridge.value?.check_only && (!bridge.value?.job_id || bridge.value.job_id===props.job.job.id) &&
    permitted.value && recipient.value && account.value && control.value==='active' && recipient.value.user_id===account.value.user_id && props.job.job.state!=='cancelled' && (!record.value ||
    (['failed','cancelled'].includes(record.value.state) && record.value.user_id===account.value.user_id)))
let stopped=false,timer,sequence=0
async function load(){
  const n=++sequence
  try{const info=await props.api(path);if(!stopped && n===sequence){bridge.value=info.bridge;account.value=info.account;record.value=info.record;recipient.value=info.recipient;recipientSource.value=info.recipient_source;control.value=info.control
    grant.value=info.rights;permitted.value=info.permitted;history.value=info.rights_history;inspections.value=info.inspections || [];reusableImages.value=info.reusable_images || 0}}
  catch(e){if(!stopped && n===sequence){bridge.value=null;account.value=null;grant.value=null;permitted.value=false;reusableImages.value=0;error.value=e.message}}
}
async function poll(){await load();if(!stopped)timer=setTimeout(poll,4000)}
async function change(data){
  busy.value=true;error.value=''
  try{await props.api(path,data)}catch(e){error.value=e.message}
  finally{await load();busy.value=false}
}
async function enqueue(){
  const target=account.value,revision=grant.value.revision
  try{await ElMessageBox.confirm('将已冻结的《'+props.job.delivery.snapshot.title+'》及其配图发送至知乎账号「'+target.name+'」（'+target.user_id+'）的草稿箱。','确认保存平台草稿',{confirmButtonText:'仅保存草稿',cancelButtonText:'返回'})}catch{return}
  await change({action:'queue',consent:true,user_id:target.user_id,sha:props.job.delivery.sha,scope_revision:revision})
}
async function setScope(scope){
  const target=recipient.value
  const data={action:'scope',scope,sha:props.job.delivery.sha,scope_revision:grant.value.revision}
  let prompt='改为仅本地后，排队或准备中的草稿任务将取消。已发出的请求和已保存的草稿仍需在平台核对。'
  if(scope==='zhihu_draft'){
    data.user_id=target.user_id;data.rights_confirmed=true
    prompt='确认《'+props.job.delivery.snapshot.title+'》的正文和配图已获准保存到知乎账号「'+target.name+'」（'+target.user_id+'）的草稿箱？记录授权后，可另行选择保存草稿。'
  }
  try{await ElMessageBox.confirm(prompt,'核对转载授权',{confirmButtonText:'确认授权范围',cancelButtonText:'返回'})}catch{return}
  await change(data)
}
watch(()=>props.bindingsVersion,load)
onMounted(poll)
onBeforeUnmount(()=>{stopped=true;clearTimeout(timer)})
</script>
<style scoped>
.draft{border-block:1px solid var(--el-border-color);margin:16px 0;padding:8px 0 16px}.draft small{display:block;color:var(--el-text-color-secondary);overflow-wrap:anywhere}.draft p{line-height:1.6}.draft a{color:var(--el-color-primary)}.draft .el-checkbox{height:auto;white-space:normal;margin:10px 0}
</style>
