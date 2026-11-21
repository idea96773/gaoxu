<template>
  <el-button type="primary" @click="open">批量转载</el-button>
  <el-dialog v-model="visible" append-to-body title="从固定目录选择转载文章" width="min(780px, 95vw)" :close-on-click-modal="false">
    <small>{{root}}</small>
    <p><el-input v-model="filter" placeholder="搜索文章标题或目录" clearable /></p>
    <div class="articles" v-loading="loading">
      <label v-for="item in filtered" :key="item.directory" :title="item.directory">
        <input type="checkbox" v-model="dirs" :value="item.directory" :disabled="dirs.length>=100 && !dirs.includes(item.directory)"> {{item.title}}
      </label>
      <p v-if="!filtered.length && !loading">没有找到符合条件的文章。</p>
    </div>
    <p>已选 {{dirs.length}} 篇 / 最多 100 篇</p>
    <fieldset><legend>参与轮流分稿的模拟账号</legend>
      <div v-for="(title,p) in channels" :key="p" class="pool"><strong>{{title}}</strong>
        <label v-for="a in aliases[p]" :key="a"><input type="checkbox" v-model="chosen[p]" :value="a" :aria-label="title+a"> {{a}}</label>
        <input v-model.trim="custom[p]" placeholder="新账号别名" maxlength="40" @keyup.enter="add(p)"><el-button size="small" @click="add(p)">添加别名</el-button>
      </div>
    </fieldset>
    <p><label>文末公众号链接 <el-input v-model.trim="destination" /></label></p>
    <fieldset><legend>排期规则（北京时间）</legend>
      <div class="rules"><label v-for="r in fields" :key="r[0]">{{r[1]}} <input type="number" v-model.number="rules[r[0]]" :min="r[2]" :max="r[3]"></label></div>
      <ScopeSchedule v-model="overrides" :targets="targets" :channels="channels" />
      <label><input type="checkbox" v-model="rules.weekdays_only"> 仅在周一至周五排期</label>
      <details class="calendar"><summary>节假日与补班日期</summary>
        <label>停排日期 <el-input type="textarea" v-model="holidays" placeholder="YYYY-MM-DD，每行一个或用逗号分隔"/></label>
        <label>补班日期 <el-input type="textarea" v-model="workdays" placeholder="允许排期的周末或例外日期"/></label>
      </details>
    </fieldset>
    <div class="defaults"><el-button :disabled="loading || busy || saving || !targets.length" :loading="saving" @click="saveDefaults">保存为常用设置</el-button>
      <el-button link :disabled="loading || busy || saving" @click="reloadDefaults">读取常用设置</el-button>
      <small role="status">{{notice}}</small></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <template #footer><el-button @click="visible=false">关闭</el-button><el-button type="primary" :disabled="loading || saving || !dirs.length || !targets.length" :loading="busy" @click="submit">提交选稿到后台</el-button></template>
  </el-dialog>
</template>
<script setup>
import {ref, computed, reactive} from 'vue'
import ScopeSchedule from './ScopeSchedule.vue'
const props=defineProps({api:Function})
const emit=defineEmits(['created'])
const visible=ref(false),busy=ref(false),loading=ref(false),error=ref(''),root=ref(''),items=ref([]),dirs=ref([]),filter=ref('')
const destination=ref('https://mp.weixin.qq.com/s/YFkz4nZBTLm-GHtqz_5lUw')
const channels={zhihu:'知乎',toutiao:'今日头条'}
const aliases=reactive({zhihu:['模拟一号','模拟二号'],toutiao:['模拟一号','模拟二号']})
const valid=a=>typeof a==='string' && /^[\p{L}\p{N}_-]{1,40}$/u.test(a)
try{
  const saved=JSON.parse(localStorage.getItem('distribution.reprint.aliases')||'{}')
  for(const p of Object.keys(channels)) if(Array.isArray(saved[p])) aliases[p]=[...new Set([...aliases[p],...saved[p].filter(valid)])]
}catch{}
const chosen=reactive({zhihu:[],toutiao:[]}),custom=reactive({zhihu:'',toutiao:''})
const rules=reactive({gap:900,account_cap:3,platform_cap:6,start_hour:9,end_hour:18,weekdays_only:false})
const holidays=ref(''),workdays=ref(''),revision=ref(0),saving=ref(false),notice=ref('')
const overrides=ref([])
const fields=[['gap','平台间隔（秒）',1,86400],['account_cap','账号每日上限',1,1000],['platform_cap','平台每日上限',1,10000],['start_hour','开始小时',0,23],['end_hour','结束小时（不含）',1,24]]
const filtered=computed(()=>items.value.filter(i=>i.directory.includes(filter.value.trim())))
const targets=computed(()=>Object.keys(channels).flatMap(platform=>chosen[platform].map(account=>({platform,account}))))
function add(p){
  const a=custom[p]
  if(!valid(a)){error.value='别名可使用 1 至 40 个汉字、字母、数字、下划线或短横线';return}
  if(!aliases[p].includes(a))aliases[p].push(a)
  if(!chosen[p].includes(a))chosen[p].push(a)
  localStorage.setItem('distribution.reprint.aliases',JSON.stringify(aliases))
  custom[p]='';error.value=''
}
function options(){
  const dates=t=>[...new Set(t.split(/[\s,，;；]+/).filter(Boolean))].sort()
  const off=dates(holidays.value),on=dates(workdays.value)
  if(on.some(day=>off.includes(day)))throw Error('同一日期不能同时设为停排和补班')
  const active=overrides.value.filter(r=>targets.value.some(t=>t.platform===r.platform && (!r.account || t.account===r.account)))
  return {targets:targets.value,destination:destination.value,rules:{...rules,holidays:off,workdays:on,overrides:active}}
}
function applyDefaults(data){
  revision.value=data.revision
  const saved=data.settings
  if(!saved){notice.value='尚未保存常用设置';return}
  for(const p of Object.keys(channels)){
    chosen[p]=saved.targets.filter(t=>t.platform===p).map(t=>t.account)
    aliases[p]=[...new Set([...aliases[p],...chosen[p]])]
  }
  destination.value=saved.destination
  for(const key of Object.keys(rules))rules[key]=saved.rules[key]
  overrides.value=saved.rules.overrides ?? []
  holidays.value=saved.rules.holidays.join('\n');workdays.value=saved.rules.workdays.join('\n')
  notice.value='已读取常用设置'
}
async function reloadDefaults(){
  saving.value=true;error.value=''
  try{applyDefaults(await props.api('reprint-settings'))}catch(e){error.value=e.message}
  finally{saving.value=false}
}
async function saveDefaults(){
  saving.value=true;error.value=''
  try{const data=await props.api('reprint-settings',{...options(),revision:revision.value})
    revision.value=data.revision;notice.value='已保存，创建新批次时自动读取'}catch(e){error.value=e.message}
  finally{saving.value=false}
}
async function open(){
  visible.value=true;loading.value=true;error.value='';notice.value=''
  const [library,saved]=await Promise.allSettled([props.api('library'),props.api('reprint-settings')])
  if(library.status==='fulfilled'){root.value=library.value.root;items.value=library.value.items}
  else{items.value=[];error.value=library.reason.message}
  if(saved.status==='fulfilled')applyDefaults(saved.value)
  else error.value=saved.reason.message
  loading.value=false
}
let signature='',key=''
async function submit(){
  busy.value=true;error.value=''
  try{
    const data={directories:dirs.value,...options()}
    const current=JSON.stringify(data)
    if(current!==signature){signature=current;key=crypto.randomUUID()}
    const result=await props.api('reprints',{...data,request_id:key})
    dirs.value=[];signature='';filter.value='';visible.value=false;emit('created',result.id)
  }catch(e){error.value=e.message}finally{busy.value=false}
}
</script>
<style scoped>
.articles{max-height:208px;min-height:50px;overflow:auto;border-block:1px solid var(--border);padding:6px 0}
.articles label{display:block;padding:5px;cursor:pointer}.pool,.rules{display:flex;flex-wrap:wrap;align-items:center;gap:8px 18px;margin:8px 0}.pool>input{width:95px}
.calendar{margin-top:8px}.calendar label{display:block;margin:6px 0}.defaults{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.defaults small{color:var(--el-text-color-secondary)}
fieldset{border:1px solid var(--border);border-radius:6px;margin:10px 0;padding:8px 14px}legend{font-weight:600}.rules input{width:72px}.pool label,.rules label{white-space:nowrap}
</style>
