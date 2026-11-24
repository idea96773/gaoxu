<template>
  <section aria-label="失败任务">
    <div class="filters">
      <label>失败日期（北京时间）<input v-model="day" type="date" :disabled="saving"></label>
      <label>失败步骤<select v-model="stage" :disabled="saving">
        <option value="">全部步骤</option>
        <option v-for="key in stages" :key="key" :value="key">{{ label(key) }}</option>
      </select></label>
      <el-button :disabled="loading || saving" @click="load(result.page)">刷新</el-button>
    </div>
    <div class="commands">
      <span>当前失败 {{ result.total }} 项</span>
      <el-button v-if="result.total>result.page_size" :disabled="loading || saving" @click="selectMatching">勾选最近 100 项可重试任务</el-button>
      <el-button v-if="selected.size" link :disabled="loading || saving" @click="clearSelection">清空勾选</el-button>
      <el-button type="primary" :loading="saving" :disabled="!selected.size || loading" @click="retry">重试所选（{{ selected.size }}）</el-button>
    </div>
    <el-alert v-if="error" :title="error" type="error" :closable="false"/>
    <el-alert v-if="message" :title="message" :type="skipped.length ? 'warning' : 'success'" @close="message=''"/>
    <ul v-if="message && skipped.length"><li v-for="item in skipped" :key="item.id">任务 #{{item.id}}　{{item.reason}}</li></ul>
    <el-table :data="result.items" v-loading="loading" row-key="id" empty-text="当前条件下没有失败任务">
      <el-table-column width="56">
        <template #header><input type="checkbox" aria-label="选择本页可重试任务" :checked="allSelected" :indeterminate="pageSelected>0 && !allSelected" :disabled="!retryable.length || saving || loading" @change="selectPage"></template>
        <template #default="{row}"><input type="checkbox" :aria-label="'选择任务 '+row.id" :checked="isSelected(row)"
          :disabled="!mayRetry(row) || saving || loading || (!isSelected(row) && selected.size>=100)" @change="toggleRow(row,$event.target.checked)"></template>
      </el-table-column>
      <el-table-column label="文章 / 任务" min-width="180"><template #default="{row}">
        <strong>{{row.title}}</strong><small>批次 {{row.batch.slice(0,8)}} · {{label(row.control)}}</small>
        <el-button link type="primary" @click="$emit('job',row.id)">查看任务 #{{row.id}}</el-button>
      </template></el-table-column>
      <el-table-column label="失败步骤 / 账号" min-width="135"><template #default="{row}">
        {{label(row.stage)}}<small v-if="row.platform">{{result.platforms[row.platform] || row.platform}} · {{row.account}}</small>
      </template></el-table-column>
      <el-table-column label="失败原因" min-width="230"><template #default="{row}">
        {{row.failure}}<small v-if="row.directory">下载目录内位置　{{row.directory}}</small>
      </template></el-table-column>
      <el-table-column label="最近失败（北京时间）" min-width="155"><template #default="{row}">
        {{date(row.failed_at)}}<small>第 {{row.attempts}} 次尝试</small>
      </template></el-table-column>
    </el-table>
    <nav v-if="result.total>result.page_size" aria-label="失败任务翻页">
      <el-button :disabled="result.page===1 || loading || saving" @click="load(result.page-1)">上一页</el-button>
      {{ result.page }} / {{ Math.ceil(result.total/result.page_size) }}
      <el-button :disabled="result.page*result.page_size>=result.total || loading || saving" @click="load(result.page+1)">下一页</el-button>
    </nav>
  </section>
</template>
<script setup>
import {computed,ref,watch,onBeforeUnmount} from 'vue'
const props=defineProps({api:Function,label:Function,date:Function,batch:String,version:String})
const emit=defineEmits(['job','retried'])
const stages=['reprint_prepare','reprint_schedule','ingest','normalize','analyze','adapt','check','plan','deliver','publish']
const empty=()=>({items:[],platforms:{},page:1,page_size:25,total:0})
const result=ref(empty()),selected=ref(new Map()),day=ref(''),stage=ref('')
const loading=ref(false),saving=ref(false),message=ref(''),error=ref(''),skipped=ref([])
const mayRetry=r=>r.stage!=='reprint_schedule' && r.control!=='cancelled'
const isSelected=r=>selected.value.get(r.id)?.expected_attempt===r.attempts
const retryable=computed(()=>result.value.items.filter(mayRetry))
const pageSelected=computed(()=>retryable.value.filter(isSelected).length)
const allSelected=computed(()=>retryable.value.length>0 && pageSelected.value===retryable.value.length)
let sequence=0,disposed=false
function toggleRow(row,checked){
  message.value=''
  if(!checked)selected.value.delete(row.id)
  else if(selected.value.size<100)selected.value.set(row.id,{id:row.id,batch:row.batch,expected_attempt:row.attempts})
}
function clearSelection(){selected.value.clear();message.value=''}
function selectPage(e){
  for(const row of retryable.value)toggleRow(row,e.target.checked)
  if(e.target.checked && !allSelected.value){message.value='本次已勾选 100 项，重试后可继续处理其余任务。';skipped.value=[]}
  e.target.checked=allSelected.value
}
function filters(){
  const query=new URLSearchParams()
  if(day.value)query.set('day',day.value)
  if(stage.value)query.set('stage',stage.value)
  if(props.batch)query.set('batch',props.batch)
  return query
}
async function selectMatching(){
  if(loading.value || saving.value)return
  const seq=++sequence
  loading.value=true;error.value=''
  try{
    const data=await props.api('failed-jobs/selection?'+filters())
    if(seq!==sequence)return
    selected.value=new Map(data.items.map(item=>[item.id,item]))
    skipped.value=[]
    message.value='当前筛选有 '+data.total+' 项可重试，已勾选 '+data.items.length+' 项。'
  }catch(e){if(seq===sequence){clearSelection();error.value=e.message}}
  finally{if(seq===sequence)loading.value=false}
}
async function load(page=1){
  const seq=++sequence
  const query=filters()
  query.set('page',page)
  loading.value=true;error.value=''
  try{
    const data=await props.api('failed-jobs?'+query)
    if(seq!==sequence)return
    result.value=data
    for(const row of data.items){
      if(selected.value.has(row.id) && (!mayRetry(row) || !isSelected(row)))selected.value.delete(row.id)
    }
  }catch(e){
    if(seq===sequence){error.value=e.message;result.value=empty();selected.value.clear()}
  }finally{if(seq===sequence)loading.value=false}
}
async function retry(){
  if(saving.value || loading.value)return
  const items=[...selected.value.values()]
  if(!items.length)return
  saving.value=true;error.value='';message.value=''
  try{
    const data=await props.api('failed-jobs/retry',{items})
    if(disposed)return
    skipped.value=data.items.filter(item=>!item.queued)
    message.value='已重新排队 '+(data.items.length-skipped.value.length)+' 项'+(skipped.value.length?'，跳过 '+skipped.value.length+' 项':'')
    selected.value.clear()
    emit('retried')
    await load(result.value.page)
  }catch(e){error.value=e.message}finally{saving.value=false}
}
watch([day,stage,()=>props.batch],()=>{selected.value.clear();message.value='';load()},{immediate:true})
watch(()=>props.version,()=>{if(!saving.value && !loading.value)load(result.value.page)})
onBeforeUnmount(()=>{disposed=true;sequence++})
</script>
<style scoped>
.filters,.commands,nav{display:flex;align-items:end;gap:12px;flex-wrap:wrap;margin:12px 0}
.filters label{display:grid;gap:5px;font-size:13px}.filters input,.filters select{height:32px;border:1px solid var(--el-border-color);border-radius:5px;padding:0 8px;color:inherit;background:var(--el-fill-color-blank)}
.commands{align-items:center;justify-content:space-between}small{display:block;color:var(--el-text-color-secondary);overflow-wrap:anywhere}nav{align-items:center;justify-content:flex-end}
</style>
