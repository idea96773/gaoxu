<template>
  <details v-if="eligible.length || result" class="postpone">
    <summary>批量顺延本批交付</summary>
    <label>分稿账号 <select v-model="account" :disabled="busy"><option value="">本批全部账号</option>
      <option v-for="[key,j] in accounts" :key="key" :value="key">{{names[j.platform]}} · {{j.account}}</option>
    </select></label>
    <label>最早执行时间（北京时间） <input v-model="start" type="datetime-local" :disabled="busy"></label>
    <el-button :disabled="busy || !candidates.length" @click="preview">预览顺延（{{candidates.length}} 份）</el-button>
    <p v-if="start && !candidates.length">当前列表没有早于该时间且仍待交付的本批任务。</p>
    <template v-if="plan">
      <el-table :data="plan" max-height="320">
        <el-table-column label="文章 / 任务" min-width="150"><template #default="{row}">{{row.title}} #{{row.id}}</template></el-table-column>
        <el-table-column label="平台 / 账号"><template #default="{row}">{{names[row.platform]}} · {{row.account}}</template></el-table-column>
        <el-table-column label="原执行时间" min-width="148"><template #default="{row}">{{date(Math.max(row.slot,row.due))}}</template></el-table-column>
        <el-table-column label="预计执行时间" min-width="148"><template #default="{row}">{{row.error || date(row.time)}}</template></el-table-column>
      </el-table>
      <el-button type="primary" :disabled="busy || !movable.length" @click="confirm">按预览顺延 {{movable.length}} 份</el-button>
    </template>
    <p v-if="result" role="status">{{result}}</p>
    <p v-if="error" role="alert">{{error}}</p>
  </details>
</template>

<script setup>
import {computed,ref,watch} from 'vue'

const props=defineProps({batch:String,jobs:Array,api:Function,names:Object,date:Function})
const emit=defineEmits(['saved'])
const account=ref(''),start=ref(''),busy=ref(false),plan=ref(null),error=ref(''),result=ref('')
const stamp=computed(()=>Date.parse(start.value+'+08:00')/1000)
const eligible=computed(()=>props.jobs.filter(j=>j.source_batch===props.batch && ['ready','paused'].includes(j.display_state)))
const accountKey=j=>JSON.stringify([j.platform,j.account])
const accounts=computed(()=>new Map(eligible.value.map(j=>[accountKey(j),j])))
const candidates=computed(()=>eligible.value.filter(j=>(!account.value || account.value===accountKey(j)) && Math.max(j.slot,j.due)<stamp.value)
  .sort((a,b)=>Math.max(a.slot,a.due)-Math.max(b.slot,b.due) || a.id-b.id))
const selection=computed(()=>JSON.stringify([stamp.value,candidates.value.map(j=>[j.id,j.slot,j.due])]))
const movable=computed(()=>(plan.value || []).filter(j=>!j.error))
watch(selection,()=>{plan.value=null})
async function preview(){
  const before=selection.value,rows=candidates.value
  busy.value=true;error.value='';plan.value=null;result.value=''
  try{
    const response=await props.api('batches/'+props.batch+'/postpone/preview',{start:stamp.value,items:rows.map(({id})=>({id}))})
    if(before!==selection.value)throw Error('任务列表已变化，请重新预览。')
    plan.value=response.items.map((item,index)=>({...rows[index],...item}))
  }catch(e){error.value=e.message}finally{busy.value=false}
}
async function confirm(){
  busy.value=true;error.value='';result.value=''
  try{
    const items=movable.value.map(({id,time,expected})=>({id,time,expected}))
    const response=await props.api('batches/'+props.batch+'/postpone/confirm',{start:stamp.value,items})
    const skipped=response.items.filter(row=>row.error)
    const already=response.items.filter(row=>row.already).length
    result.value='已顺延 '+(items.length-skipped.length-already)+' 份。'+(already?'另有 '+already+' 份已在预览时段。':'')
    error.value=skipped.map(row=>'#'+row.id+' '+row.error).join('；')
    emit('saved')
  }catch(e){error.value=e.message}finally{plan.value=null;busy.value=false}
}
</script>

<style scoped>
.postpone{margin:12px 0}.postpone summary{cursor:pointer}.postpone label{display:inline-block;margin:12px 8px 12px 0}.postpone [role="alert"]{color:var(--el-color-danger)}
</style>
