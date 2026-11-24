<template>
  <section class="draft">
    <h3>知乎草稿箱</h3>
    <p>分稿别名　{{job.job.account}}　<el-button link @click="$emit('accounts',job.job.account)">管理知乎账号</el-button></p>
    <p>{{recipientSource==='binding'?'本次保存将锁定账号':'本稿指定账号'}}　{{recipient?.name || '待绑定'}}
      <small v-if="recipient">账号 ID　{{recipient.user_id}}</small></p>
    <p v-if="cancelled">这份稿件的{{control==='cancelled'?'所属批次':'排期任务'}}已取消。可在此查看历史记录。</p>
    <p v-else-if="control==='paused'">批次已暂停，继续批次后可办理草稿交付。</p>
    <BridgeConnection v-else :bridge="bridge" :account="account" :target="recipient" :job-id="job.job.id"/>
    <div v-if="grant" class="scope">
      <p>转载授权　<strong>{{grant.scope==='local'?'仅本地试稿':'可用于指定知乎账号的草稿箱'}}</strong>
        <small v-if="grant.scope==='zhihu_draft'">获准账号 ID　{{grant.user_id}}</small></p>
      <el-button v-if="recipient && !permitted && !cancelled" link type="primary" :disabled="busy" @click="setScope('zhihu_draft')">记录此账号的草稿授权</el-button>
      <el-button v-if="grant.scope==='zhihu_draft'" link :disabled="busy" @click="setScope('local')">改为仅本地试稿</el-button>
      <details v-if="history.length"><summary>近期授权记录</summary>
        <small v-for="(event,index) in history" :key="index">{{date(event.confirmed)}}　{{event.scope==='local'?'仅本地试稿':'知乎草稿 · '+event.user_id}}</small>
      </details>
    </div>
    <p v-if="record">目标账号　{{record.name}}　·　{{labels[record.state]}}
      <a v-if="record.url" :href="record.url" target="_blank" rel="noopener noreferrer">打开知乎草稿</a>
    </p>
    <p v-if="record?.reason">{{record.reason}}</p>
    <div v-if="(bridge?.phase==='offline' || awaitingChoice) && ['sending','preparing'].includes(record?.state)">
      <p>{{record.state==='sending'?'上次保存结果待核对，请在「'+record.name+'」的草稿箱查找本文。':'上次配图准备已中断，登记后可以重新确认保存。'}}</p>
      <el-button v-if="awaitingChoice && account?.user_id===record.user_id" :disabled="busy" @click="recoverInterrupted">登记此稿中断</el-button>
      <p v-else>启动助手选项 3 并登录原接收账号，可在本页登记中断。</p>
    </div>
    <details v-if="stateHistory.length"><summary>草稿处理记录（最近 {{stateHistory.length}} 条）</summary>
      <ol><li v-for="event in stateHistory" :key="event.id">{{date(event.changed)}} · {{labels[event.state] || event.state}}
        <small v-if="event.baseline">升级时保留的最后状态</small><small v-if="event.reason">{{event.reason}}</small></li></ol>
    </details>
    <p v-if="recoveryReview">恢复后核对 · {{date(recoveryReview.checked_at)}} · {{recoveryReview.result==='found'?'在指定账号找到并关联草稿':'确认指定账号未找到此稿后加入队列'}}</p>
    <p v-if="record?.state==='unknown' && receiptUrl">扩展曾返回编辑地址，接收账号仍待核对。请在「{{record.name}}」的浏览器中
      <a :href="receiptUrl" target="_blank" rel="noopener noreferrer">打开待核对草稿</a>，核对账号和内容后填写下方核对表。
      <el-button link :disabled="busy" @click="dismissReceipt">标记此地址与本稿不符</el-button></p>
    <p v-if="dismissedReceipt">已于 {{date(dismissedReceipt.dismissed_at)}} 记录回执地址与本稿不符
      <small>{{dismissedReceipt.edit_url}}</small></p>
    <p v-if="reusableImages && ['queued','preparing','failed','cancelled'].includes(record?.state)">
      本稿可沿用 {{reusableImages}} 张已转存配图，共 {{uniqueImages}} 张不同配图。
    </p>
    <p v-if="!record && !recipient && !cancelled">在「管理知乎账号」中将分稿别名与指定登录账号绑定。</p>
    <div v-if="recovery && !record">
      <p>恢复副本中没有此稿的保存记录。请先在指定账号的知乎草稿箱查找《{{job.delivery.snapshot.title}}》。</p>
      <el-button v-if="recipient" :disabled="busy" @click="recoverExisting">关联已找到的知乎草稿</el-button>
    </div>
    <el-button v-if="canSend" type="primary" :loading="busy" @click="enqueue">{{awaitingChoice?'选定本稿并':''}}保存到 {{account.name}} 的草稿箱</el-button>
    <el-button v-if="record && ['queued','preparing'].includes(record.state)" :disabled="busy" @click="change({action:'cancel',expected_event:eventId})">取消草稿任务</el-button>
    <DraftInspection v-if="record && ['saved','unknown','needs_review','verified'].includes(record.state)"
      :key="record.id+':'+receiptUrl" :api="api" :path="path" :record="record" :receipt-url="receiptUrl" :entries="inspections" :snapshot="job.delivery.snapshot"
      :date="date" :dismissed-receipt="dismissedReceipt" @refresh="load" @recorded="$emit('inspected')" />
    <el-alert v-if="error" :title="error" type="error" :closable="false"/>
  </section>
</template>
<script setup>
import {ref,computed,h,onMounted,onBeforeUnmount,watch} from 'vue'
import {ElMessageBox} from 'element-plus'
import DraftInspection from './DraftInspection.vue'
import DraftSavePreview from './DraftSavePreview.vue'
import {readFrozenReprint} from '../api/frozenReprint'
import BridgeConnection from './BridgeConnection.vue'
const props=defineProps({api:Function,job:Object,bindingsVersion:Number,date:Function})
defineEmits(['accounts','inspected'])
const recipient=ref(null),recipientSource=ref(''),control=ref('')
const jobState=ref(props.job.job.state)
const cancelled=computed(()=>control.value==='cancelled' || jobState.value==='cancelled')
const grant=ref(null),history=ref([]),permitted=ref(false)
const recovery=ref(null),recoveryReview=ref(null)
const receiptUrl=ref(''),dismissedReceipt=ref(null)
const reusableImages=ref(0)
const uniqueImages=computed(()=>new Set(props.job.delivery.snapshot.images).size)
const bridge=ref(null),account=ref(null),record=ref(null),inspections=ref([]),busy=ref(false),error=ref('')
const stateHistory=ref([])
const eventId=computed(()=>stateHistory.value[0]?.id ?? 0)
const labels={queued:'等待保存',preparing:'准备配图',sending:'正在请求保存',saved:'已找到草稿，待核对',verified:'已人工核对',needs_review:'核对发现问题',unknown:'回执不确定，需核对草稿箱',failed:'准备失败',cancelled:'已取消'}
const path='jobs/'+props.job.job.id+'/platform-draft'
const awaitingChoice=computed(()=>!!bridge.value?.choice_session && !bridge.value.job_id)
const canSend=computed(()=>!bridge.value?.check_only && (!bridge.value?.job_id || bridge.value.job_id===props.job.job.id) &&
    permitted.value && recipient.value && account.value && control.value==='active' && recipient.value.user_id===account.value.user_id && !cancelled.value && (!record.value ||
    ((['failed','cancelled'].includes(record.value.state) || (awaitingChoice.value && record.value.state==='queued')) && record.value.user_id===account.value.user_id)))
let stopped=false,timer,sequence=0
async function load(){
  const n=++sequence
  try{const info=await props.api(path);if(!stopped && n===sequence){bridge.value=info.bridge;account.value=info.account;record.value=info.record;recipient.value=info.recipient;recipientSource.value=info.recipient_source;control.value=info.control
    grant.value=info.rights;permitted.value=info.permitted;recovery.value=info.recovery;recoveryReview.value=info.recovery_review;history.value=info.rights_history;inspections.value=info.inspections || [];reusableImages.value=info.reusable_images || 0
    jobState.value=info.job_state;receiptUrl.value=info.unverified_edit_url || '';dismissedReceipt.value=info.dismissed_receipt
    stateHistory.value=info.state_history || []}}
  catch(e){if(!stopped && n===sequence){bridge.value=null;account.value=null;grant.value=null;permitted.value=false;reusableImages.value=0;error.value=e.message}}
}
async function poll(){await load();if(!stopped)timer=setTimeout(poll,4000)}
async function change(data){
  busy.value=true;error.value=''
  try{await props.api(path,data)}catch(e){error.value=e.message}
  finally{await load();busy.value=false}
}
async function enqueue(){
  if(busy.value || !canSend.value)return
  const target=account.value,revision=grant.value.revision,generation=recovery.value?.generation,expected=eventId.value
  const choice=bridge.value?.choice_session
  busy.value=true
  error.value=''
  try{
    const report=await readFrozenReprint(props.api,props.job)
    if(stopped)return
    const message=h(DraftSavePreview,{report,account:target,planned:props.date(report.planned),restored:!!generation})
    await ElMessageBox.confirm(message,'确认保存知乎草稿',{confirmButtonText:generation?'已核对无此稿，保存草稿':'仅保存草稿',cancelButtonText:'返回'})
    if(stopped)return
    await change({action:'queue',consent:true,user_id:target.user_id,sha:report.sha,expected_slot:report.planned,
      scope_revision:revision,generation,checked_absent:!!generation,expected_event:expected,choice_session:choice})
  }catch(e){if(!stopped && e!=='cancel' && e!=='close')error.value=e.message || '未能核对冻结稿，请重新打开任务详情。'}
  finally{busy.value=false}
}
async function recoverInterrupted(){
  await change({action:'recover_interrupted',choice_session:bridge.value.choice_session,expected_event:eventId.value,
    sha:record.value.sha,user_id:record.value.user_id})
}
async function recoverExisting(){
  const target=recipient.value,generation=recovery.value.generation
  let input
  try{input=await ElMessageBox.prompt('请在「'+target.name+'」（'+target.user_id+'）确认《'+props.job.delivery.snapshot.title+'》，粘贴该稿的知乎编辑地址。','关联已找到的知乎草稿',{
    confirmButtonText:'关联到此冻结稿',cancelButtonText:'返回',inputPlaceholder:'https://zhuanlan.zhihu.com/p/…/edit'
  })}catch{return}
  await change({action:'recover_existing',generation,checked:true,user_id:target.user_id,sha:props.job.delivery.sha,url:input.value?.trim()})
}
async function dismissReceipt(){
  const data={action:'dismiss_receipt',user_id:record.value.user_id,sha:record.value.sha,url:receiptUrl.value,checked:true}
  try{await ElMessageBox.confirm('确认已在「'+record.value.name+'」的草稿箱中核对，此地址与《'+props.job.delivery.snapshot.title+'》不符？标记后地址可供其他任务核对，此稿继续等待人工核对。','核对回执地址',{confirmButtonText:'确认地址不符',cancelButtonText:'返回'})}catch{return}
  await change(data)
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
