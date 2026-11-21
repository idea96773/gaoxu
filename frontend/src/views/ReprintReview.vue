<template>
  <section>
    <p>{{pool}}　·　平台间隔 {{batch.rules.gap}} 秒　·　账号每日 {{batch.rules.account_cap}} 篇　·　平台每日 {{batch.rules.platform_cap}} 篇　·　北京时间 {{batch.rules.start_hour}}–{{batch.rules.end_hour}} 时</p>
    <p>文末延伸阅读　{{batch.destination}}</p>
    <div class="bar"><label><input type="checkbox" :checked="pending.length>0 && checked.length===pending.length" :disabled="!active || !pending.length" @change="selectAll"> 全选待审核</label>
      <el-button type="primary" :loading="busy" :disabled="!active || !checked.length" @click="approve">审核通过并自动排期（{{checked.length}}）</el-button></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false"/>
    <el-table :data="batch.reviews" row-key="id">
      <el-table-column width="50"><template #default="{row}"><input type="checkbox" :aria-label="'选择审核 '+row.title" :disabled="row.state!=='pending'||!active" v-model="ids" :value="row.id"></template></el-table-column>
      <el-table-column label="转载文章" min-width="190"><template #default="{row}"><b>{{row.title}}</b><p v-if="row.note">{{row.note}}</p>
        <el-button v-if="row.state!=='invalid'" type="primary" link @click="preview(row)">预览转载稿</el-button></template></el-table-column>
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
import {ref,computed} from 'vue'
import {ElMessageBox,ElMessage} from 'element-plus'
const props=defineProps({batch:Object,api:Function,names:Object,label:Function,date:Function})
const emit=defineEmits(['job','refresh'])
const ids=ref([]),error=ref(''),busy=ref(false),opened=ref(false),view=ref(null)
const pending=computed(()=>props.batch.reviews.filter(r=>r.state==='pending'))
const checked=computed(()=>pending.value.filter(r=>ids.value.includes(r.id)))
const active=computed(()=>props.batch.control==='active')
const pool=computed(()=>Object.entries(props.names).flatMap(([p,label])=>{
  const accounts=props.batch.targets.filter(t=>t.platform===p).map(t=>t.account)
  return accounts.length?[label+' · '+accounts.join('、')]:[]
}).join('　/　'))
function selectAll(e){ids.value=e.target.checked?pending.value.map(r=>r.id):[]}
const frame=computed(()=>'<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src '+location.origin+'; style-src \'unsafe-inline\'"><style>body{max-width:680px;margin:18px auto;padding:0 16px;color:#242830;font:16px/1.8 system-ui}img{max-width:100%;height:auto}h1{font-size:24px}a{color:#1766aa}p{overflow-wrap:anywhere}</style>'+view.value.html)
async function preview(r){
  try{const result=await props.api('reprints/'+props.batch.id+'/'+r.id);if(result.sha!==r.sha)throw Error('预览版本已变化，请刷新后重试')
    view.value=result.snapshot;opened.value=true
  }catch(e){error.value=e.message}
}
async function approve(){
  const items=checked.value.map(({id,sha})=>({id,sha})),bid=props.batch.id
  try{await ElMessageBox.confirm('确认已审核这 '+items.length+' 篇原文、配图和文末链接，可按本批账号与规则模拟交付？','确认转载审核',{confirmButtonText:'确认模拟排期',cancelButtonText:'返回'})}catch{return}
  busy.value=true;error.value=''
  try{const result=await props.api('reprints/'+bid+'/approve',{approved:true,items})
    ids.value=result.items.filter(x=>x.error).map(x=>x.id)
    error.value=result.items.filter(x=>x.error).map(x=>'#'+x.id+' '+x.error).join('；')
    if(!error.value)ElMessage.success('审核已记录，分稿与排期结果已更新')
    emit('refresh')
  }catch(e){error.value=e.message}finally{busy.value=false}
}
</script>
<style scoped>
.bar{display:flex;align-items:center;gap:18px;margin:18px 0}.assignment{padding:6px 0}.assignment small{display:block}.assignment a{color:var(--el-color-primary);font-size:12px}iframe{width:100%;height:72vh;border:0}p{overflow-wrap:anywhere}
</style>
