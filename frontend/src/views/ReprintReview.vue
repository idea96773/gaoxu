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
    <p>文末延伸阅读　{{batch.destination}}</p>
    <div class="bar"><label><input type="checkbox" :checked="pending.length>0 && checked.length===pending.length" :disabled="!active || !pending.length" @change="selectAll"> 全选待审核</label>
      <el-button type="primary" :loading="busy" :disabled="!active || !checked.length" @click="approve">审核通过并自动排期（{{checked.length}}）</el-button></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false"/>
    <el-table :data="batch.reviews" row-key="id">
      <el-table-column width="50"><template #default="{row}"><input type="checkbox" :aria-label="'选择审核 '+row.title" :disabled="row.state!=='pending'||!active" v-model="ids" :value="reviewKey(row)"></template></el-table-column>
      <el-table-column label="转载文章" min-width="190"><template #default="{row}"><b>{{row.title}}</b><p v-if="row.note">{{row.note}}</p>
        <el-button v-if="['pending','approved','duplicate'].includes(row.state)" type="primary" link @click="preview(row)">预览转载稿</el-button>
        <el-button v-if="active && row.state==='pending' && row.prepare_job" link :disabled="busy" @click="reread(row)">从目录重读</el-button>
        <el-button v-if="row.prepare_job" link @click="$emit('job',row.prepare_job)">读取记录（{{row.prepare_attempts}} 次）</el-button>
        <el-button v-if="row.prepare_state==='failed' && active" link type="primary" @click="$emit('retry',row.prepare_job,row.prepare_attempts)">重新读取这篇</el-button></template></el-table-column>
      <el-table-column label="进度" width="98"><template #default="{row}">{{row.progress==='pending'?'待审核':label(row.progress)}}</template></el-table-column>
      <el-table-column label="模拟账号、排期和回执" min-width="230"><template #default="{row}">
        <div v-for="a in row.assignments" :key="a.platform" class="assignment"><el-button link @click="$emit('job',a.id)">{{names[a.platform]}} · {{a.account}} · {{label(a.state)}} ›</el-button>
          <small>{{date(a.slot)}}　{{a.reused?'已有任务，本批跳过':''}}</small>
          <a :href="'/api/distribution/editorial/'+a.draft+'/export'">下载冻结稿</a></div>
        <span v-if="!row.assignments.length">—</span></template></el-table-column>
    </el-table>
    <el-drawer v-model="opened" append-to-body title="转载稿审核预览" size="min(820px, 96vw)">
      <template v-if="view"><p>{{view.images.length}} 处配图 · 文末链接 {{view.link.destination}}</p>
        <iframe title="审核版转载正文" sandbox="" :srcdoc="frame"></iframe></template>
    </el-drawer>
  </section>
</template>
<script setup>
import {ref,computed,watch} from 'vue'
import {ElMessageBox,ElMessage} from 'element-plus'
const props=defineProps({batch:Object,api:Function,names:Object,label:Function,date:Function})
const clock=m=>[Math.floor(m/60),m%60].map(n=>String(n).padStart(2,'0')).join(':')
const emit=defineEmits(['job','refresh','retry'])
const ids=ref([]),error=ref(''),busy=ref(false),opened=ref(false),view=ref(null)
const pending=computed(()=>props.batch.reviews.filter(r=>r.state==='pending'))
const reviewKey=r=>[r.id,r.revision,r.sha].join(':')
const checked=computed(()=>pending.value.filter(r=>ids.value.includes(reviewKey(r))))
const active=computed(()=>props.batch.control==='active')
const pool=computed(()=>Object.entries(props.names).flatMap(([p,label])=>{
  const accounts=props.batch.targets.filter(t=>t.platform===p).map(t=>t.account)
  return accounts.length?[label+' · '+accounts.join('、')]:[]
}).join('　/　'))
function selectAll(e){ids.value=e.target.checked?pending.value.map(reviewKey):[]}
const frame=computed(()=>'<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src '+location.origin+'; style-src \'unsafe-inline\'"><style>body{max-width:680px;margin:18px auto;padding:0 16px;color:#242830;font:16px/1.8 system-ui}img{max-width:100%;height:auto}h1{font-size:24px}a{color:#1766aa}p{overflow-wrap:anywhere}</style>'+view.value.html)
const previewKey=ref('')
watch(()=>props.batch.reviews,rows=>{
  if(opened.value && !rows.some(r=>reviewKey(r)===previewKey.value)){
    opened.value=false;view.value=null;error.value='原预览已失效，请重新打开待审核稿'
  }
})
async function reread(row){
  const bid=props.batch.id
  try{await ElMessageBox.confirm('确认已修正下载目录内的正文或配图？重读后请重新审核这篇稿件。','从目录重读',{confirmButtonText:'重新读取',cancelButtonText:'返回'})}catch{return}
  busy.value=true;error.value=''
  try{
    await props.api('reprints/'+bid+'/'+row.id+'/reread',{sha:row.sha,revision:row.revision})
    ids.value=ids.value.filter(key=>key!==reviewKey(row))
    emit('refresh');ElMessage.success('已排队重读这篇文章')
  }catch(e){error.value=e.message}finally{busy.value=false}
}
async function preview(r){
  try{const result=await props.api('reprints/'+props.batch.id+'/'+r.id);if(result.sha!==r.sha || result.revision!==r.revision)throw Error('预览版本已变化，请刷新后重试')
    previewKey.value=reviewKey(r);view.value=result.snapshot;opened.value=true
  }catch(e){error.value=e.message}
}
async function approve(){
  const items=checked.value.map(({id,sha,revision})=>({id,sha,revision})),bid=props.batch.id
  try{await ElMessageBox.confirm('确认已审核这 '+items.length+' 篇原文、配图和文末链接，可按本批账号与规则模拟交付？','确认转载审核',{confirmButtonText:'确认模拟排期',cancelButtonText:'返回'})}catch{return}
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
.bar{display:flex;align-items:center;gap:18px;margin:18px 0}.assignment{padding:6px 0}.assignment small{display:block}.assignment a{color:var(--el-color-primary);font-size:12px}iframe{width:100%;height:72vh;border:0}p{overflow-wrap:anywhere}
</style>
