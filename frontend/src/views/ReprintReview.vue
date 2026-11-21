<template>
  <section>
    <p>{{pool}}</p>
    <details><summary>查看各账号排期规则（北京时间）</summary>
      <p v-for="r in batch.target_limits" :key="r.platform+'-'+r.account">
        {{names[r.platform]}} · {{r.account}}　{{clock(r.start)}}–{{clock(r.end)}}　平台每日 {{r.platform_cap}} 篇，间隔 {{r.platform_gap}} 秒；
        本账号每日最多 {{Math.min(r.platform_cap,r.account_cap)}} 篇，间隔至少 {{Math.max(r.platform_gap,r.account_gap)}} 秒
      </p>
      <p>{{batch.rules.weekdays_only?'仅周一至周五':'含周末'}}<span v-if="batch.rules.holidays.length">　停排 {{batch.rules.holidays.join('、')}}</span><span v-if="batch.rules.workdays.length">　补班 {{batch.rules.workdays.join('、')}}</span></p>
    </details>
    <p>本批新稿的目标公众号文章　{{batch.destination}}</p>
    <p v-for="p in previewPlatforms" :key="p">{{names[p]}}新稿文末　{{batch.links?.[p]?.mode==='omit'?'不追加延伸阅读':batch.links?.[p]?.short || batch.destination}}</p>
    <div class="bar"><label><input type="checkbox" :checked="pending.length>0 && checked.length===pending.length" :disabled="!active || !pending.length" @change="selectAll"> 全选待审核</label>
      <el-button type="primary" :loading="busy" :disabled="!active || !checked.length" @click="approve">审核通过并自动排期（{{checked.length}}）</el-button></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false"/>
    <el-table :data="batch.reviews" row-key="id">
      <el-table-column width="50"><template #default="{row}"><input type="checkbox" :aria-label="'选择审核 '+row.title" :disabled="row.state!=='pending'||!active" v-model="ids" :value="reviewKey(row)"></template></el-table-column>
      <el-table-column label="转载文章" min-width="190"><template #default="{row}"><b>{{row.line}} · {{row.title}}</b><p v-if="row.note">{{row.note}}</p>
        <p v-if="row.state==='returned' && row.returns?.length">退回原因　{{row.returns[0].reason}}</p>
        <details v-if="row.returns?.length"><summary>退回记录（{{row.returns.length}}）</summary>
          <p v-for="entry in row.returns" :key="entry.revision">第 {{entry.revision}} 版 · {{date(entry.returned_at)}}　{{entry.reason}}
            <small v-if="entry.resumed_at"> · {{date(entry.resumed_at)}} {{entry.resumed_as==='directory'?'从目录重读':'原稿重新送审'}}</small></p>
        </details>
        <template v-if="['pending','returned','approved','duplicate'].includes(row.state)">
          <el-button v-for="p in rowPlatforms(row)" :key="p" type="primary" link @click="preview(row,p)">预览{{names[p]}}{{boundJob(row,p)?'冻结稿':row.state==='returned'?'退回稿':'待审稿'}}</el-button>
        </template>
        <el-button v-if="active && row.state==='pending'" link :disabled="busy" @click="changeDecision(row,'return')">退回此稿</el-button>
        <el-button v-if="active && row.state==='returned'" link :disabled="busy" @click="changeDecision(row,'reopen')">原稿重新送审</el-button>
        <el-button v-if="active && ['pending','returned'].includes(row.state) && row.prepare_job" link :disabled="busy" @click="reread(row)">从目录重读</el-button>
        <el-button v-if="row.prepare_job" link @click="$emit('job',row.prepare_job)">读取记录（{{row.prepare_attempts}} 次）</el-button>
        <el-button v-if="row.prepare_state==='failed' && active" link type="primary" @click="$emit('retry',row.prepare_job,row.prepare_attempts)">重新读取这篇</el-button></template></el-table-column>
      <el-table-column label="进度" width="98"><template #default="{row}">{{row.progress==='pending'?'待审核':row.progress==='returned'?'审核退回':label(row.progress)}}</template></el-table-column>
      <el-table-column label="模拟账号、排期和回执" min-width="230"><template #default="{row}">
        <template v-if="row.state==='pending'">
          <small v-if="row.missing_platforms?.length">审核后排期　{{row.missing_platforms.map(p=>names[p]).join('、')}}</small>
          <div v-for="job in row.prior_jobs" :key="job.id"><el-button link @click="$emit('job',job.id)">已有任务 · {{names[job.platform]}} · {{job.account}} · {{label(job.state)}} ›</el-button></div>
        </template>
        <div v-for="a in row.assignments" :key="a.platform" class="assignment"><el-button link @click="$emit('job',a.id)">{{names[a.platform]}} · {{a.account}} · {{label(a.state)}} ›</el-button>
          <small>{{date(a.slot)}}　{{a.reused?'沿用原冻结稿及排期，本批跳过':''}}</small>
          <small v-if="a.due>a.slot">顺延至 {{date(a.due)}}</small>
          <small v-if="a.draft_recipient_id">知乎草稿接收账号　{{a.draft_recipient}}（{{a.draft_recipient_id}}）</small>
          <small>授权范围　{{a.usage_scope==='zhihu_draft'?'指定知乎账号草稿箱':'仅本地试稿'}}</small>
          <a :href="'/api/distribution/editorial/'+a.draft+'/export'">下载冻结稿</a></div>
        <span v-if="!row.assignments.length && row.state!=='pending'">—</span></template></el-table-column>
    </el-table>
    <el-drawer v-model="opened" append-to-body :title="(names[previewPlatform] || '转载稿')+'审核预览'" size="min(820px, 96vw)">
      <template v-if="view"><p v-if="previewTask">{{previewTask.reused?'沿用冻结稿':'已冻结'}} · 任务 #{{previewTask.job}} · {{previewTask.account}}</p>
        <ReprintApproval v-if="previewTask" :info="previewTask.approval" :date="date" />
        <p v-else>本批送审稿 · 第 {{ previewRevision }} 版</p>
        <p>{{view.images.length}} 处配图 · {{view.link.mode==='omit'?'不追加延伸阅读':'读者链接　'+(view.link.short || view.link.destination)}}</p>
        <p v-if="view.link.mode==='short'">目标公众号文章　{{view.link.destination}}</p>
        <ReprintFrame v-if="view.html" title="审核版转载正文" :html="view.html" />
        <article v-else class="plain-preview"><h3>{{view.title}}</h3><p>{{view.text}}</p><img v-for="src in localImages" :key="src" :src="src" alt="冻结稿配图"></article></template>
    </el-drawer>
  </section>
</template>
<script setup>
import {ref,computed,watch} from 'vue'
import ReprintFrame from './ReprintFrame.vue'
import ReprintApproval from './ReprintApproval.vue'
import {ElMessageBox,ElMessage} from 'element-plus'
const props=defineProps({batch:Object,api:Function,names:Object,label:Function,date:Function})
const clock=m=>[Math.floor(m/60),m%60].map(n=>String(n).padStart(2,'0')).join(':')
const emit=defineEmits(['job','refresh','retry'])
const ids=ref([]),error=ref(''),busy=ref(false),opened=ref(false),view=ref(null)
const pending=computed(()=>props.batch.reviews.filter(r=>r.state==='pending'))
const priorIds=r=>Object.fromEntries((r.prior_jobs || []).map(job=>[job.platform,job.id]))
const reviewKey=r=>[r.id,r.revision,r.sha,JSON.stringify(r.expected_prior || priorIds(r))].join(':')
const checked=computed(()=>pending.value.filter(r=>ids.value.includes(reviewKey(r))))
const active=computed(()=>props.batch.control==='active')
const pool=computed(()=>Object.entries(props.names).flatMap(([p,label])=>{
  const accounts=props.batch.targets.filter(t=>t.platform===p).map(t=>t.account)
  return accounts.length?[label+' · '+accounts.join('、')]:[]
}).join('　/　'))
function selectAll(e){ids.value=e.target.checked?pending.value.map(reviewKey):[]}
const previewKey=ref(''),previewPlatform=ref(''),previewTask=ref(null),previewRevision=ref(0)
const previewPlatforms=computed(()=>[...new Set(props.batch.targets.map(t=>t.platform))])
const rowPlatforms=row=>['pending','returned'].includes(row.state)?previewPlatforms.value:row.assignments.map(a=>a.platform)
const boundJob=(row,p)=>(row.state==='pending'?row.prior_jobs:row.assignments)?.find(job=>job.platform===p)?.id ?? null
const previewIdentity=(row,p)=>JSON.stringify([props.batch.id,reviewKey(row),row.state,p,boundJob(row,p)])
const localImages=computed(()=>(view.value?.images || []).filter(src=>typeof src==='string' && /^\/api\/distribution\/media\/[a-f0-9]{64}$/.test(src)))
watch(()=>props.batch.reviews,rows=>{
  if(opened.value && !rows.some(r=>previewIdentity(r,previewPlatform.value)===previewKey.value)){
    opened.value=false;view.value=null;error.value='原预览已失效，请重新打开待审核稿'
  }
})
async function reread(row){
  const bid=props.batch.id
  try{await ElMessageBox.confirm('确认已修正下载目录内的正文或配图？重读后请重新审核这篇稿件。','从目录重读',{confirmButtonText:'重新读取',cancelButtonText:'返回'})}catch{return}
  busy.value=true;error.value=''
  try{
    await props.api('reprints/'+bid+'/'+row.id+'/reread',{sha:row.sha,revision:row.revision,expected_state:row.state})
    ids.value=ids.value.filter(key=>key!==reviewKey(row))
    emit('refresh');ElMessage.success('已排队重读这篇文章')
  }catch(e){error.value=e.message}finally{busy.value=false}
}
async function changeDecision(row,action){
  const bid=props.batch.id
  const data={sha:row.sha,revision:row.revision}
  try{
    if(action==='return'){
      const input=await ElMessageBox.prompt('请说明《'+row.title+'》需要处理的问题。','退回此稿',{
        inputType:'textarea',inputPlaceholder:'例如：缺少一张配图，或转载授权需核实',
        inputValidator:text=>!!text?.trim() && text.trim().length<=500 || '请填写 1 至 500 字的原因',
        confirmButtonText:'记录并退回',cancelButtonText:'返回'
      })
      data.reason=input.value.trim()
    }else{
      await ElMessageBox.confirm('将《'+row.title+'》当前保存的正文和配图重新送审？如果已修改下载目录中的文件，请选择「从目录重读」。','原稿重新送审',{
        confirmButtonText:'重新送审',cancelButtonText:'返回'
      })
    }
  }catch{return}
  busy.value=true;error.value=''
  try{
    await props.api('reprints/'+bid+'/'+row.id+'/'+action,data)
    ids.value=ids.value.filter(key=>key!==reviewKey(row))
    emit('refresh')
    ElMessage.success(action==='return'?'已记录退回原因':'原稿已重新进入待审列表')
  }catch(e){error.value=e.message}finally{busy.value=false}
}
let previewRequest=0
async function preview(r,platform){
  const seq=++previewRequest,key=previewIdentity(r,platform)
  error.value=''
  try{
    const result=await props.api('reprints/'+props.batch.id+'/'+r.id+'?platform='+encodeURIComponent(platform))
    if(seq!==previewRequest)return
    const current=props.batch.reviews.find(row=>row.id===r.id)
    if(!current || previewIdentity(current,platform)!==key || result.sha!==r.sha ||
       result.revision!==r.revision || result.job!==boundJob(r,platform)){
      emit('refresh');throw Error('稿件或沿用任务已变化，请在刷新后重新核对')
    }
    previewKey.value=key;previewPlatform.value=platform;previewTask.value=result.job?result:null
    previewRevision.value=result.revision;view.value=result.snapshot;opened.value=true
  }catch(e){if(seq===previewRequest)error.value=e.message}
}
async function approve(){
  const items=checked.value.map(row=>({id:row.id,sha:row.sha,revision:row.revision,expected_prior:priorIds(row)})),bid=props.batch.id
  try{await ElMessageBox.confirm('确认已审核这 '+items.length+' 篇原文、配图和文末链接，可在本地试稿并按规则模拟交付？新冻结稿的授权范围为「仅本地试稿」。','确认转载审核',{confirmButtonText:'确认模拟排期',cancelButtonText:'返回'})}catch{return}
  if(props.batch.id!==bid || !active.value){error.value='当前批次已变化，请在目标批次中重新选择';return}
  busy.value=true;error.value=''
  try{const result=await props.api('reprints/'+bid+'/approve',{approved:true,items})
    const failed=new Set(result.items.filter(x=>x.error).map(x=>x.id))
    ids.value=items.filter(x=>failed.has(x.id)).map(reviewKey)
    error.value=result.items.filter(x=>x.error).map(x=>'#'+x.id+' '+x.error).join('；')
    if(!error.value)ElMessage.success('审核已记录，分稿与排期结果已更新')
    emit('refresh')
  }catch(e){error.value=e.message}finally{busy.value=false}
}
</script>
<style scoped>
.plain-preview p{white-space:pre-wrap}.plain-preview img{display:block;max-width:100%;height:auto}
.bar{display:flex;align-items:center;gap:18px;margin:18px 0}.assignment{padding:6px 0}.assignment small{display:block}.assignment a{color:var(--el-color-primary);font-size:12px}iframe{width:100%;height:72vh;border:0}p{overflow-wrap:anywhere}
</style>
