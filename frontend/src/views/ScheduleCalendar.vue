<template>
  <section aria-label="批次排期日历">
    <div class="toolbar">
      <el-button :disabled="!calendar" @click="step(-1)">上月</el-button>
      <label>月份 <input type="month" :value="month" @change="chooseMonth($event.target.value)"></label>
      <el-button :disabled="!calendar" @click="step(1)">下月</el-button>
      <el-button @click="update({month:''})">本月</el-button>
      <label>日期类型 <select :value="dateField" @change="update({date_field:$event.target.value})">
        <option value="execution">预计执行日</option><option value="scheduled">计划排期日</option>
      </select></label>
      <label>平台 <select :value="platform" @change="update({platform:$event.target.value,account:''})"><option value="">全部平台</option>
        <option v-for="(name,key) in names" :key="key" :value="key">{{name}}</option></select></label>
      <label>账号 <select :value="account" @change="update({account:$event.target.value})"><option value="">全部账号</option>
        <option v-for="a in accounts" :key="a">{{a}}</option></select></label>
      <el-button @click="load" :loading="busy">刷新</el-button>
    </div>
    <el-alert v-if="error" :title="error" type="error" :closable="false"/>
    <template v-if="calendar">
      <p>北京时间 · 当月共 {{total}} 项模拟排期</p>
      <div class="week"><span v-for="w in weekdays" :key="w">{{w}}</span></div>
      <div class="grid" v-loading="busy">
        <span v-for="blank in calendar.weekday" :key="'blank'+blank"></span>
        <button v-for="d in cells" :key="d.date" type="button" :class="{today:d.date===calendar.today}" :disabled="busy || !d.total" @click="openDay(d.date)">
          <strong>{{d.number}}<small v-if="d.date===calendar.today"> 今天</small></strong>
          <span v-for="state in states.filter(s=>d.counts[s])" :key="state" :class="state">{{label(state)}} {{d.counts[state]}}</span>
        </button>
      </div>
    </template>
  </section>
</template>
<script setup>
import {ref,computed,watch,onMounted,onBeforeUnmount} from 'vue'
import {useRouter,useRoute} from 'vue-router'
const props=defineProps({api:Function,names:Object,label:Function})
const emit=defineEmits(['platforms'])
const router=useRouter(),route=useRoute()
const calendar=ref(null),error=ref(''),busy=ref(false),choices=ref([])
const weekdays=['周一','周二','周三','周四','周五','周六','周日']
const states=['ready','running','done','failed','paused','cancelled']
const platform=computed(()=>String(route.query.platform||'')),account=computed(()=>String(route.query.account||''))
const month=computed(()=>String(route.query.month||calendar.value?.month||''))
const dateField=computed(()=>route.query.date_field==='scheduled'?'scheduled':'execution')
const accounts=computed(()=>[...new Set(choices.value.filter(x=>!platform.value || x.platform===platform.value).map(x=>x.account))].sort())
const cells=computed(()=>calendar.value ? Array.from({length:calendar.value.size},(_,i)=>{
  const date=calendar.value.month+'-'+String(i+1).padStart(2,'0')
  const counts=calendar.value.days[date] || {}
  return {date,number:i+1,counts,total:Object.values(counts).reduce((a,b)=>a+b,0)}
}):[])
const total=computed(()=>cells.value.reduce((a,b)=>a+b.total,0))
let seq=0
async function load(){
  const n=++seq
  busy.value=true;error.value=''
  const params=new URLSearchParams({month:String(route.query.month||''),platform:platform.value,account:account.value,date_field:dateField.value})
  try{const result=await props.api('schedule-calendar?'+params)
    if(seq===n)calendar.value=result
  }catch(e){if(seq===n){error.value=e.message;calendar.value=null}}
  finally{if(seq===n)busy.value=false}
}
function update(changes){
  const query={...route.query,...changes}
  for(const k of Object.keys(query))if(!query[k])delete query[k]
  return router.replace({query})
}
function chooseMonth(m){if(/^\d{4}-\d{2}$/.test(m))update({month:m})}
function step(n){
  const [y,m]=calendar.value.month.split('-').map(Number)
  const next=new Date(Date.UTC(y,m-1+n,1))
  chooseMonth(next.toISOString().slice(0,7))
}
function openDay(date){
  const query={...route.query,view:'search',date_field:calendar.value.date_field,start:date,end:date}
  delete query.month;delete query.page;delete query.title
  router.push({query})
}
watch(()=>route.fullPath,()=>{if(route.query.view==='calendar')load()},{immediate:true})
onMounted(async()=>{try{const d=await props.api('article-search/accounts');choices.value=d.items;emit('platforms',d.platforms)}catch(e){error.value=e.message}})
onBeforeUnmount(()=>{seq++})
</script>
<style scoped>
.toolbar{display:flex;align-items:center;gap:8px 12px;flex-wrap:wrap;margin:16px 0}.toolbar select,.toolbar input{font:inherit;color:var(--el-text-color-primary);background:var(--el-fill-color-blank);border:1px solid var(--el-border-color);border-radius:4px;padding:6px}
.week,.grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr))}.week span{padding:8px;text-align:center;color:var(--el-text-color-secondary)}
.grid button,.grid>span{min-height:82px;border:1px solid var(--el-border-color);background:var(--el-fill-color-blank);color:var(--el-text-color-primary);text-align:left;padding:7px;display:flex;flex-direction:column;gap:3px;font:inherit}
.grid button:not(:disabled){cursor:pointer}.grid button:not(:disabled):hover,.grid .today{border-color:var(--el-color-primary)}.grid button:disabled{color:var(--el-text-color-secondary)}
.grid button span,.grid small{font-size:12px}.grid .failed{color:var(--el-color-danger)}.grid .done{color:var(--el-color-success)}.grid .paused{color:var(--el-color-warning)}@media(max-width:660px){.grid button,.grid>span{padding:3px;min-height:60px}.grid button span{font-size:10px}}
</style>
