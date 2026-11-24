<template>
  <section class="search-page" aria-label="跨批次查询">
    <form class="filters" @submit.prevent="submit">
      <label class="title-filter">文章标题<input v-model.trim="form.title" type="search" placeholder="输入原文或平台稿标题关键词"></label>
      <label>平台<select v-model="form.platform" @change="syncAccount"><option value="">全部平台</option>
        <option v-for="p in platforms" :value="p" :key="p">{{names[p] || p}}</option></select></label>
      <label>分稿账号<select v-model="form.account"><option value="">全部账号</option>
        <option v-for="a in accounts" :key="a">{{a}}</option></select></label>
      <div class="date-row">
        <label>工作进度<select v-model="form.work" @change="switchWork"><option value="">全部进度</option>
          <option value="pending_reprint">待审转载</option><option value="scheduling_reprint">审核已受理待排期</option><option value="returned_reprint">审核退回</option><option value="withdrawn_reprint">本批已撤回</option>
          <option value="frozen_delivery">全部冻结稿交付任务</option><option value="queued_delivery">冻结稿待交付</option>
          <option value="confirmed_delivery">冻结稿已模拟交付</option></select></label>
        <label>模拟排期状态<select v-model="form.schedule_state"><option value="">全部状态</option>
          <option v-for="s in ['ready','running','done','failed','paused','cancelled','unconfirmed']" :key="s" :value="s">{{label(s)}}</option></select></label>
        <label v-if="showZhihu">知乎接收账号<input v-model.trim="form.draft_recipient" type="search" placeholder="锁定的昵称或完整账号 ID"></label>
        <label v-if="showZhihu">知乎草稿状态<select v-model="form.draft_review"><option value="">全部</option>
          <option value="unsubmitted_reprint">运行中批次 · 尚未建草稿任务的冻结转载稿</option>
          <option value="waiting">等待保存</option><option value="saving">正在准备或保存</option><option value="failed">准备失败</option>
          <option value="awaiting">待人工核对</option><option value="uncertain">回执不确定</option>
          <option value="issue">核对发现问题</option><option value="verified">已核对通过</option><option value="cancelled">已取消</option></select></label>
        <label>日期类型<select v-model="form.date_field"><option value="scheduled">计划排期日</option><option value="execution">预计执行日</option><option value="delivered">模拟交付日</option><option value="imported">导入日期</option><option v-if="showZhihu" value="draft_updated">知乎草稿最近更新日</option></select></label>
        <label>开始日期<input type="date" v-model="form.start"></label>
        <label>结束日期<input type="date" v-model="form.end"></label>
        <div class="commands"><el-button type="primary" native-type="submit">查询</el-button><el-button @click="reset">重置</el-button></div>
      </div>
    </form>
    <el-alert v-if="error" :title="error" type="error" :closable="false"/>
    <div class="result-head"><strong>查询结果</strong><span v-if="loaded">共 {{result.total}} 条记录</span><small>北京时间</small></div>
    <el-table :data="result.items" v-loading="loading" empty-text="没有匹配记录，请调整查询条件">
      <el-table-column label="文章 / 批次" min-width="235"><template #default="{row}">
        <strong>{{row.title}}</strong><div v-if="row.edition && row.edition!==row.title" class="secondary">平台稿 · {{row.edition}}</div>
        <div class="secondary">{{kinds[row.stage]}} · <router-link :to="{query:{batch:row.batch}}">批次 {{row.batch.slice(0,8)}}</router-link> · 导入 {{date(row.created)}}</div></template></el-table-column>
      <el-table-column label="平台 / 账号" min-width="135"><template #default="{row}">{{names[row.platform] || '—'}}<div class="secondary">{{row.account || '待分配'}}</div>
        <div v-if="row.recipient_id" class="secondary">知乎接收账号　{{row.recipient_name}}<br>ID　{{row.recipient_id}}</div></template></el-table-column>
      <el-table-column label="计划交付" min-width="147"><template #default="{row}">{{date(row.slot)}}
        <small v-if="row.state==='scheduling' && row.slot" class="secondary">审核时锁定</small>
        <small v-if="row.execution_slot>row.slot" class="secondary">顺延至 {{date(row.execution_slot)}}</small></template></el-table-column>
      <el-table-column label="实际模拟交付" min-width="137"><template #default="{row}">{{date(row.received)}}</template></el-table-column>
      <el-table-column label="交付状态" width="116"><template #default="{row}"><el-tag :type="colors[row.schedule_state || row.state] || 'info'">{{states[row.schedule_state || row.state] || label(row.schedule_state || row.state)}}</el-tag>
        <div v-if="row.draft_state" class="secondary">知乎草稿 · {{draftStates[row.draft_state] || row.draft_state}}</div>
        <div v-if="row.draft_updated" class="secondary">草稿更新 {{date(row.draft_updated)}}</div>
        <div v-if="row.control==='paused' && !row.schedule_state && ['unscheduled','preparing','pending','returned','scheduling'].includes(row.state)" class="secondary">批次已暂停</div>
        <el-button v-if="row.job_id" link @click="$emit('job',row.job_id)">任务详情</el-button>
        <router-link v-if="row.stage==='review' && row.state==='pending' && row.control==='active'"
          :to="{query:{batch:row.batch,review_row:String(row.row_id)}}">审核这篇</router-link>
        <router-link v-if="row.state==='scheduling'" :to="{query:{batch:row.batch,review_row:String(row.row_id)}}">查看排期进度</router-link>
        <router-link v-if="row.state==='returned'" :to="{query:{batch:row.batch,review_row:String(row.row_id)}}">查看退回原因</router-link>
        <router-link v-if="row.state==='withdrawn'" :to="{query:{batch:row.batch,review_row:String(row.row_id)}}">查看已撤回稿件</router-link></template></el-table-column>
    </el-table>
    <nav v-if="result.total>result.page_size" class="pages" aria-label="查询结果翻页">
      <el-button :disabled="loading || result.page===1" @click="move(-1)">上一页</el-button>
      <span>第 {{result.page}} / {{Math.ceil(result.total/result.page_size)}} 页</span>
      <el-button :disabled="loading || result.page*result.page_size>=result.total" @click="move(1)">下一页</el-button>
    </nav>
  </section>
</template>
<script setup>
import {computed, onMounted, onBeforeUnmount, reactive, ref, watch} from 'vue'
import {useRoute,useRouter} from 'vue-router'
const props=defineProps({api:Function,names:Object,date:Function,label:Function,version:Number})
const emit=defineEmits(['job','platforms'])
const route=useRoute(),router=useRouter()
const defaults={title:'',platform:'',account:'',draft_review:'',draft_recipient:'',work:'',schedule_state:'',date_field:'scheduled',start:'',end:''}
const draftStates={queued:'待保存',preparing:'准备中',sending:'保存中',saved:'待核对',unknown:'回执待核对',
  needs_review:'发现问题',verified:'已核对通过',failed:'准备失败',cancelled:'已取消'}
const form=reactive({...defaults}),choices=ref([]),error=ref(''),loading=ref(false),loaded=ref(false)
const empty=()=>({items:[],total:0,page:1,page_size:25})
const result=ref(empty())
const kinds={deliver:'冻结稿模拟交付',publish:'模拟流程发布',pipeline:'模拟流程',review:'转载审核'}
const colors={done:'success',failed:'danger',blocked:'danger',invalid:'danger',ready:'primary',pending:'warning',running:'warning'}
const states={preparing:'准备转载资料',done:'已模拟交付',pending:'待审核',returned:'审核退回',withdrawn:'本批已撤回',invalid:'资料异常',blocked:'排期前失败',unscheduled:'待排期'}
const platforms=computed(()=>[...new Set([...Object.keys(props.names),...choices.value.map(x=>x.platform)])])
const accounts=computed(()=>[...new Set(choices.value.filter(x=>!form.platform || x.platform===form.platform).map(x=>x.account))].sort())
const showZhihu=computed(()=>!form.platform || form.platform==='zhihu')
function syncAccount(){
  if(!accounts.value.includes(form.account))form.account=''
  if(!showZhihu.value){
    form.draft_recipient='';form.draft_review=''
    if(form.date_field==='draft_updated')form.date_field='scheduled'
  }
}
function switchWork(){
  if(form.work==='scheduling_reprint'){form.date_field='imported';form.schedule_state='';form.draft_review='';form.draft_recipient=''}
  if(['pending_reprint','returned_reprint','withdrawn_reprint'].includes(form.work)){form.date_field='imported';form.platform='';form.account='';form.draft_review='';form.draft_recipient='';form.schedule_state=''}
  if(form.work==='queued_delivery')form.date_field='execution'
  if(form.work==='confirmed_delivery')form.date_field='delivered'
}
function paramsFromRoute(){
  const values={...defaults}
  for(const k of Object.keys(values))if(typeof route.query[k]==='string')values[k]=route.query[k]
  return values
}
let sequence=0
async function load(){
  const number=++sequence, values=paramsFromRoute()
  Object.assign(form,values)
  const params=new URLSearchParams({...values,page:String(route.query.page || 1)})
  loading.value=true;error.value=''
  try{const data=await props.api('article-search?'+params)
    if(number===sequence){result.value=data;loaded.value=true}
  }catch(e){if(number===sequence){error.value=e.message;result.value=empty();loaded.value=false}}
  finally{if(number===sequence)loading.value=false}
}
function show(values,page=1){
  const query={...route.query,view:'search'}
  for(const [k,v] of Object.entries(values)){if(v && v!==defaults[k])query[k]=v;else delete query[k]}
  if(page===1)delete query.page;else query.page=String(page)
  if(router.resolve({query}).fullPath===route.fullPath)return load()
  return router.replace({query})
}
function submit(){
  if(form.start && form.end && form.start>form.end){error.value='结束日期应等于或晚于开始日期';return}
  return show(form)
}
function reset(){return show(defaults)}
function move(step){return show(paramsFromRoute(),result.value.page+step)}
watch([()=>route.fullPath,()=>props.version],()=>{if(route.query.view==='search')load()},{immediate:true})
onMounted(async()=>{try{const data=await props.api('article-search/accounts');choices.value=data.items;emit('platforms',data.platforms)}catch(e){error.value=e.message}})
onBeforeUnmount(()=>{sequence++})
</script>
<style scoped>
.filters{display:grid;grid-template-columns:minmax(220px,2fr) repeat(2,minmax(125px,1fr));gap:14px 18px;border-block:1px solid var(--border);padding:20px 0}
.filters label{display:grid;gap:5px;font-size:13px}.filters input,.filters select{width:100%;box-sizing:border-box;height:34px;font:inherit;border-radius:6px;border:1px solid var(--el-border-color);background:var(--el-fill-color-blank);color:var(--el-text-color-primary);padding:4px 8px}
.date-row{grid-column:1/-1;display:flex;align-items:end;flex-wrap:wrap;gap:12px}.commands{display:flex}
.result-head,.pages{display:flex;align-items:center;gap:16px;margin:20px 0}.result-head small{margin-left:auto}
.secondary,.result-head span,.result-head small{font-size:12px;color:var(--el-text-color-secondary)}.secondary{line-height:1.5;margin-top:4px}
.pages{justify-content:flex-end}.search-page :deep(.el-table .cell){overflow-wrap:anywhere}
@media(max-width:720px){.filters{grid-template-columns:1fr 1fr}.title-filter{grid-column:1/-1}}
</style>
