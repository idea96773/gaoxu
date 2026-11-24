<template>
  <section>
    <p>{{pool}}</p>
    <details><summary>查看各账号排期规则（北京时间）</summary>
      <p v-if="batch.saved_revision">本批采用常用设置第 {{batch.saved_revision}} 版</p>
      <p v-for="r in batch.target_limits" :key="r.platform+'-'+r.account">
        {{names[r.platform]}} · {{r.account}}　{{clock(r.start)}}–{{clock(r.end)}}　平台每日 {{r.platform_cap}} 篇，间隔 {{r.platform_gap}} 秒；
        本账号每日最多 {{Math.min(r.platform_cap,r.account_cap)}} 篇，间隔至少 {{Math.max(r.platform_gap,r.account_gap)}} 秒
      </p>
      <p>{{batch.rules.weekdays_only?'仅周一至周五':'含周末'}}<span v-if="batch.rules.holidays.length">　停排 {{batch.rules.holidays.join('、')}}</span><span v-if="batch.rules.workdays.length">　补班 {{batch.rules.workdays.join('、')}}</span></p>
    </details>
    <p>本批新稿的目标公众号文章　{{batch.destination}}</p>
    <p v-for="p in previewPlatforms" :key="p">{{names[p]}}新稿文末　{{batch.links?.[p]?.mode==='omit'?'不追加延伸阅读':batch.links?.[p]?.short || batch.destination}}</p>
    <label v-if="active && pending.length">新任务最早排期（北京时间，可选）
      <input v-model="startAt" type="datetime-local" :disabled="busy">
    </label>
    <div class="bar"><label><input type="checkbox" :checked="pending.length>0 && checked.length===pending.length" :disabled="!canSelect || !pending.length || busy" @change="selectAll"> 全选待审核</label>
      <el-button :disabled="busy || planning || !canSelect || !checked.length" @click="changeSelection(checked,false)">撤回所选</el-button>
      <el-button :loading="planning" :disabled="busy || !active || !checked.length" @click="plan">预览分稿与排期</el-button>
      <el-button type="primary" :loading="busy" :disabled="planning || !active || !approvable.length" @click="approve">{{startAt?'审核并锁定预览排期':'审核通过并自动排期'}}（{{approvable.length}}）</el-button></div>
    <section v-if="forecast" aria-label="分稿与排期预览">
      <p>预计排期 · 北京时间，试算于 {{date(forecast.calculated_at)}}。{{startAt?'审核后按表中账号和时间排期；遇到冲突的文章会退回待审。':'正式时段以审核时可用名额为准。'}}</p>
      <p v-for="r in forecast.items.filter(r=>r.error)" :key="r.id" role="alert">《{{r.title}}》暂缓审核：{{r.error}}</p>
      <el-table v-if="planRows.length" :data="planRows" max-height="340">
        <el-table-column prop="title" label="文章" min-width="180" />
        <el-table-column label="平台 / 分稿账号" min-width="140"><template #default="{row}">{{names[row.platform]}} · {{row.account}}</template></el-table-column>
        <el-table-column label="预计排期" min-width="155"><template #default="{row}">{{date(row.slot)}}</template></el-table-column>
        <el-table-column label="任务" width="125"><template #default="{row}">{{row.prior_job?'沿用 #'+row.prior_job:'审核后新建'}}</template></el-table-column>
      </el-table>
    </section>
    <el-alert v-if="error" :title="error" type="error" :closable="false"/>
    <el-table :data="batch.reviews" row-key="id">
      <el-table-column width="50"><template #default="{row}"><input type="checkbox" :aria-label="'选择审核 '+row.title" :disabled="row.state!=='pending'||!canSelect||busy" v-model="ids" :value="reviewKey(row)"></template></el-table-column>
      <el-table-column label="转载文章" min-width="190"><template #default="{row}"><b>{{row.line}} · {{row.title}}</b><p v-if="row.note">{{row.note}}</p>
        <p v-if="row.state==='pending' && (row.text_changed || row.structure_changed)">请在预览中核对{{row.text_changed?'正文文字':''}}{{row.text_changed && row.structure_changed?'和':''}}{{row.structure_changed?'排版结构':''}}差异。</p>
        <p v-if="row.state==='returned' && row.returns?.length">退回原因　{{row.returns[0].reason}}</p>
        <details v-if="row.returns?.length"><summary>退回记录（{{row.returns.length}}）</summary>
          <p v-for="entry in row.returns" :key="entry.revision">第 {{entry.revision}} 版 · {{date(entry.returned_at)}}　{{entry.reason}}
            <small v-if="entry.resumed_at"> · {{date(entry.resumed_at)}} {{entry.resumed_as==='directory'?'从目录重读':'原稿重新送审'}}</small></p>
        </details>
        <details v-if="row.withdrawals?.length"><summary>撤回记录（{{row.withdrawals.length}}）</summary>
          <p v-for="entry in row.withdrawals" :key="entry.revision">第 {{entry.revision}} 版 · {{date(entry.withdrawn_at)}} 撤回
            <small v-if="entry.restored_at"> · {{date(entry.restored_at)}} 恢复待审</small></p>
        </details>
        <el-button v-if="canSelect && row.state==='withdrawn'" link type="primary" :disabled="busy" @click="changeSelection([row],true)">恢复待审</el-button>
        <template v-if="['pending','scheduling','returned','withdrawn','approved','duplicate'].includes(row.state)">
          <el-button v-for="p in rowPlatforms(row)" :key="p" type="primary" link @click="preview(row,p)">预览{{names[p]}}{{boundJob(row,p)?'冻结稿':row.state==='returned'?'退回稿':row.state==='withdrawn'?'撤回稿':'待审稿'}}</el-button>
        </template>
        <el-button v-if="active && row.state==='pending'" link :disabled="busy" @click="changeDecision(row,'return')">退回此稿</el-button>
        <el-button v-if="active && row.state==='returned'" link :disabled="busy" @click="changeDecision(row,'reopen')">原稿重新送审</el-button>
        <el-button v-if="active && ['pending','returned'].includes(row.state) && row.prepare_job" link :disabled="busy" @click="reread(row)">从目录重读</el-button>
        <el-button v-if="row.prepare_job" link @click="$emit('job',row.prepare_job)">读取记录（{{row.prepare_attempts}} 次）</el-button>
        <el-button v-if="row.schedule_job" link @click="$emit('job',row.schedule_job)">分稿排期记录 #{{row.schedule_job}}</el-button>
        <p v-if="row.state==='pending' && row.schedule_failure" role="alert">{{row.schedule_failure}}</p>
        <p v-if="row.relocated_directory">重新选定的目录　{{row.relocated_directory}}</p>
        <template v-if="row.prepare_state==='failed' && active">
          <el-button link type="primary" @click="$emit('retry',row.prepare_job,row.prepare_attempts)">重新读取这篇</el-button>
          <el-button v-if="row.can_identify_source" link @click="chooseDirectory(row)">从新目录重试</el-button>
        </template></template></el-table-column>
      <el-table-column label="进度" width="116"><template #default="{row}">{{row.progress==='pending'?'待审核':row.progress==='returned'?'审核退回':row.progress==='withdrawn'?'本批已撤回':label(row.progress)}}
        <small v-if="row.prepare_retry_at">{{active?'下次可重读 '+date(row.prepare_retry_at):'批次继续后重读'}}</small>
        <small v-if="row.state==='duplicate' && row.delivery_progress"> · 关联任务{{label(row.delivery_progress)}}</small></template></el-table-column>
      <el-table-column label="模拟账号、排期和回执" min-width="230"><template #default="{row}">
        <template v-if="row.state==='pending'">
          <small v-if="row.missing_platforms?.length">审核后排期　{{row.missing_platforms.map(p=>names[p]).join('、')}}</small>
          <div v-for="job in row.prior_jobs" :key="job.id"><el-button link @click="$emit('job',job.id)">已有任务 · {{names[job.platform]}} · {{job.account}} · {{label(job.state)}} ›</el-button></div>
        </template>
        <div v-for="a in row.assignments" :key="a.platform" class="assignment"><el-button link @click="$emit('job',a.id)">{{names[a.platform]}} · {{a.account}} · {{label(a.display_state)}} ›</el-button>
          <small>{{date(a.slot)}}　{{a.reused?'沿用原冻结稿及排期，本批跳过':''}}</small>
          <small v-if="a.due>a.slot">顺延至 {{date(a.due)}}</small>
          <small v-if="a.draft_recipient_id">知乎草稿接收账号　{{a.draft_recipient}}（{{a.draft_recipient_id}}）</small>
          <small>授权范围　{{a.usage_scope==='zhihu_draft'?'指定知乎账号草稿箱':'仅本地试稿'}}</small>
          <a :href="'/api/distribution/editorial/'+a.draft+'/export'">下载冻结稿</a></div>
        <span v-if="!row.assignments.length && row.state!=='pending'">—</span></template></el-table-column>
    </el-table>
    <el-dialog v-model="moving" title="从新目录重试" append-to-body width="min(620px, 94vw)">
      <p>请从当前保存目录选择《{{moveTarget?.title}}》。系统会核对原文标识，再重新读取这一篇。</p>
      <p v-if="moveRoot">{{moveRoot}}</p>
      <el-select v-model="moveDir" filterable placeholder="输入文章标题或目录名筛选" aria-label="新的文章目录" :loading="loadingDirs" style="width:100%">
        <el-option v-for="item in moveOptions" :key="item.directory" :value="item.directory" :label="item.title+' · '+item.directory" />
      </el-select>
      <p v-if="moveError" role="alert">{{moveError}}</p>
      <template #footer><el-button @click="moving=false">关闭</el-button><el-button type="primary" :loading="busy" :disabled="!moveDir || loadingDirs" @click="relocate">核对并重读</el-button></template>
    </el-dialog>
    <el-drawer v-model="opened" append-to-body :title="(names[previewPlatform] || '转载稿')+'审核预览'" size="min(820px, 96vw)">
      <template v-if="view"><p v-if="previewTask">{{previewTask.reused?'沿用冻结稿':'已冻结'}} · 任务 #{{previewTask.job}} · {{previewTask.account}}</p>
        <ReprintApproval v-if="previewTask" :info="previewTask.approval" :date="date" />
        <p v-else>本批送审稿 · 第 {{ previewRevision }} 版</p>
        <p>{{view.images.length}} 处配图 · {{view.link.mode==='omit'?'不追加延伸阅读':'读者链接　'+(view.link.short || view.link.destination)}}</p>
        <p v-if="view.html_image_count != null">已按原 HTML 顺序核对 {{view.html_image_count}} 处配图<span v-if="view.images.length>view.html_image_count">，请核对 Markdown 多出的 {{view.images.length-view.html_image_count}} 处配图</span>。</p>
        <ReprintBodyComparison :report="view.body_comparison"/>
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
import ReprintBodyComparison from './ReprintBodyComparison.vue'
import {ElMessageBox,ElMessage} from 'element-plus'
const props=defineProps({batch:Object,api:Function,names:Object,label:Function,date:Function})
const clock=m=>[Math.floor(m/60),m%60].map(n=>String(n).padStart(2,'0')).join(':')
const emit=defineEmits(['job','refresh','retry'])
const ids=ref([]),error=ref(''),busy=ref(false),opened=ref(false),view=ref(null)
const pending=computed(()=>props.batch.reviews.filter(r=>r.state==='pending'))
const priorIds=r=>Object.fromEntries((r.prior_jobs || []).map(job=>[job.platform,job.id]))
const reviewKey=r=>[r.id,r.revision,r.sha,r.previous_attempt??r.schedule_attempts??0,JSON.stringify(r.expected_prior || priorIds(r))].join(':')
const checked=computed(()=>pending.value.filter(r=>ids.value.includes(reviewKey(r))))
const active=computed(()=>props.batch.control==='active')
const canSelect=computed(()=>active.value || props.batch.control==='paused')
const moving=ref(false),moveTarget=ref(null),moveDir=ref(''),moveRoot=ref(''),moveOptions=ref([]),moveError=ref(''),loadingDirs=ref(false)
watch(()=>props.batch.id,()=>{moving.value=false;moveTarget.value=null})
async function chooseDirectory(row){
  moveTarget.value=row;moving.value=true;moveDir.value='';moveRoot.value='';moveOptions.value=[];moveError.value='';loadingDirs.value=true
  try{
    const catalog=await props.api('library')
    if(moveTarget.value!==row)return
    moveRoot.value=catalog.root;moveOptions.value=catalog.items.filter(item=>item.selection_key)
    if(!moveOptions.value.length)moveError.value='当前保存目录里没有可核对的文章，请检查下载目录设置。'
  }catch(e){if(moveTarget.value===row)moveError.value=e.message}
  finally{if(moveTarget.value===row)loadingDirs.value=false}
}
async function relocate(){
  const row=moveTarget.value,item=moveOptions.value.find(entry=>entry.directory===moveDir.value)
  if(!item || !row)return
  busy.value=true;moveError.value=''
  try{
    await props.api('reprints/'+props.batch.id+'/'+row.id+'/relocate',{directory:item.directory,expected_root:moveRoot.value,
      selection_key:item.selection_key,expected_attempt:row.prepare_attempts})
    moving.value=false;emit('refresh');ElMessage.success('已核对来源，原读取任务已重新入队')
  }catch(e){moveError.value=e.message}finally{busy.value=false}
}
const planning=ref(false),forecast=ref(null),startAt=ref('')
const selection=computed(()=>JSON.stringify([props.batch.id,active.value,startAt.value,checked.value.map(reviewKey)]))
const planRows=computed(()=>(forecast.value?.items||[]).flatMap(r=>(r.assignments||[]).map(a=>({...a,title:r.title}))))
const plannedAssignments=computed(()=>new Map((forecast.value?.items||[]).filter(r=>!r.error).map(r=>[r.id,r.assignments])))
const approvable=computed(()=>forecast.value?checked.value.filter(r=>plannedAssignments.value.has(r.id)):(startAt.value?[]:checked.value))
const reviewItem=row=>({id:row.id,sha:row.sha,revision:row.revision,expected_prior:priorIds(row),previous_attempt:row.schedule_attempts||0})
watch(selection,()=>{forecast.value=null},{flush:'sync'})
async function plan(){
  const key=selection.value
  planning.value=true;error.value=''
  try{
    const data={items:checked.value.map(reviewItem)}
    if(startAt.value){
      data.start_at=Date.parse(startAt.value+'+08:00')/1000
      if(!Number.isSafeInteger(data.start_at))throw Error('请输入有效的北京时间')
    }
    const result=await props.api('reprints/'+props.batch.id+'/plan',data)
    if(selection.value===key)forecast.value=result
  }catch(e){if(selection.value===key){forecast.value=null;error.value=e.message}}
  finally{planning.value=false}
}
const pool=computed(()=>Object.entries(props.names).flatMap(([p,label])=>{
  const accounts=props.batch.targets.filter(t=>t.platform===p).map(t=>t.account)
  return accounts.length?[label+' · '+accounts.join('、')]:[]
}).join('　/　'))
function selectAll(e){ids.value=e.target.checked?pending.value.map(reviewKey):[]}
async function changeSelection(rows,restore){
  const bid=props.batch.id,items=rows.map(({id,sha,revision})=>({id,sha,revision}))
  if(!restore){
    try{await ElMessageBox.confirm('将所选 '+items.length+' 篇移出本批待审清单？如需继续处理，可在原行恢复待审。','撤回待审稿',
      {confirmButtonText:'确认撤回',cancelButtonText:'返回'})}catch{return}
  }
  busy.value=true;error.value=''
  try{
    const result=await props.api('reprints/'+bid+'/'+(restore?'restore-selection':'withdraw'),{items})
    const failed=result.items.filter(r=>r.error)
    error.value=failed.map(r=>'#'+r.id+' '+r.error).join('；')
    const applied=new Set(result.items.filter(r=>!r.error).map(r=>r.id))
    ids.value=ids.value.filter(key=>!applied.has(Number(key.split(':')[0])))
    if(applied.size)ElMessage.success(restore?'已恢复待审，核对后可重新审核':'本批已撤回 '+applied.size+' 篇')
    emit('refresh')
  }catch(e){error.value=e.message}finally{busy.value=false}
}
const previewKey=ref(''),previewPlatform=ref(''),previewTask=ref(null),previewRevision=ref(0)
const previewPlatforms=computed(()=>[...new Set(props.batch.targets.map(t=>t.platform))])
const rowPlatforms=row=>['pending','scheduling','returned','withdrawn'].includes(row.state)?previewPlatforms.value:row.assignments.map(a=>a.platform)
const boundJob=(row,p)=>(['pending','scheduling'].includes(row.state)?row.prior_jobs:row.assignments)?.find(job=>job.platform===p)?.id ?? null
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
  const key=selection.value,bid=props.batch.id
  const items=approvable.value.map(row=>{
    const item=reviewItem(row),assignments=plannedAssignments.value.get(row.id)
    if(assignments)item.expected_accounts=Object.fromEntries(assignments.map(a=>[a.platform,a.account]))
    if(startAt.value && assignments)item.expected_slots=Object.fromEntries(assignments.map(a=>[a.platform,a.slot]))
    return item
  })
  try{await ElMessageBox.confirm('确认已审核这 '+items.length+' 篇原文、配图和文末链接，可在本地试稿并按规则模拟交付？新冻结稿的授权范围为「仅本地试稿」。','确认转载审核',{confirmButtonText:'确认模拟排期',cancelButtonText:'返回'})}catch{return}
  if(selection.value!==key || !active.value){error.value='当前选稿或批次已变化，请重新核对';return}
  busy.value=true;error.value=''
  try{const result=await props.api('reprints/'+bid+'/approve',{approved:true,background:true,items})
    const failed=new Set(result.items.filter(x=>x.error).map(x=>x.id))
    const completed=new Set(items.filter(x=>!failed.has(x.id)).map(reviewKey))
    ids.value=ids.value.filter(id=>!completed.has(id))
    forecast.value=null
    error.value=result.items.filter(x=>x.error).map(x=>'#'+x.id+' '+x.error).join('；')
    if(!error.value)ElMessage.success('审核已受理，可关闭页面。分稿和排期进度会保存在本批次。')
    emit('refresh')
  }catch(e){error.value=e.message}finally{busy.value=false}
}
</script>
<style scoped>
.plain-preview p{white-space:pre-wrap}.plain-preview img{display:block;max-width:100%;height:auto}
.bar{display:flex;align-items:center;gap:18px;margin:18px 0}.assignment{padding:6px 0}.assignment small{display:block}.assignment a{color:var(--el-color-primary);font-size:12px}iframe{width:100%;height:72vh;border:0}p{overflow-wrap:anywhere}
</style>
