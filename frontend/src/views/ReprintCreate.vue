<template>
  <el-button type="primary" @click="open">批量转载</el-button>
  <el-dialog v-model="visible" append-to-body title="从固定目录选择转载文章" width="min(780px, 95vw)" :close-on-click-modal="false">
    <p v-if="pending" role="status">上次提交 {{pending.data.directories.length}} 篇，回执待核对。重试将使用上次提交的选稿和设置。
      <el-button link :disabled="busy || loading" @click="recover">查找原批次</el-button></p>
    <div :inert="busy || loading">
    <template v-if="root"><small>{{root}}</small> <el-button link @click="reloadLibrary(root)">刷新目录</el-button></template>
    <LibrarySettings :api="api" :disabled="loading || busy" :needs-setup="needsSetup" @saved="reloadLibrary" />
    <p><el-input v-model="filter" placeholder="搜索文章标题或目录" clearable /></p>
    <div class="coverage-tools">
      <label>来源公众号 <select v-model="sourceName"><option value="">全部公众号</option>
        <option v-for="name in sourceNames" :key="name" :value="name">{{name}}</option></select></label>
      <label>原文发布日期 <input type="date" v-model="fromDay" :max="toDay || undefined"></label>
      <label>至 <input type="date" v-model="toDay" :min="fromDay || undefined"></label>
      <label><input type="checkbox" v-model="newestFirst"> 新稿优先</label>
    </div>
    <p v-if="invalidRange" role="alert">结束日期应等于或晚于开始日期。</p>
    <div class="coverage-tools">
      <label>筛选文章 <select v-model="coverageFilter">
        <option value="all">全部</option><option value="missing" :disabled="!selectedPlatforms.length">所选平台有缺项</option>
        <option value="covered" :disabled="!selectedPlatforms.length">所选平台均有任务</option>
        <option value="unknown">待识别原文链接</option>
      </select></label>
      <el-button size="small" :disabled="loading || !additions.length" @click="addMissing">补选缺排文章（{{additions.length}}）</el-button>
      <small v-if="!selectedPlatforms.length">勾选下方平台账号后可按缺项补选</small>
    </div>
    <div class="articles" v-loading="loading">
      <div v-for="item in pageItems" :key="item.directory" class="catalog-row">
        <label :title="item.directory"><input type="checkbox" v-model="dirs" :value="item.directory" :disabled="(!item.selection_key || dirs.length>=100) && !dirs.includes(item.directory)"> {{item.title}}</label>
        <small>{{item.source_name || '公众号未识别'}} · {{item.publish_time || '原文发布时间未识别'}}</small>
        <small v-if="!item.selection_key">请核对导出的 Markdown 文件后重新打开选稿</small>
        <small v-else-if="!item.source">原文链接待识别，按当前 Markdown 文件选稿</small>
        <small v-else-if="selectedPlatforms.length">{{missing(item).length?'待补排　'+missing(item).map(p=>channels[p]).join('、'):'所选平台均已有任务'}}</small>
        <el-button v-for="job in item.deliveries" :key="job.id" type="primary" link @click="$emit('job',job.id)">
          {{channels[job.platform] || job.platform}} · {{job.account}} · {{job.control==='paused' && job.state==='ready'?'批次暂停':states[job.state] || job.state}}{{job.control==='cancelled'?' · 原批次已取消':''}} ›
        </el-button>
        <el-button v-for="job in item.edited_deliveries" :key="job.id" link @click="$emit('job',job.id)">
          改编稿 #{{job.id}} · {{channels[job.platform] || job.platform}} · {{job.account}} · {{states[job.state] || job.state}} ›
        </el-button>
      </div>
      <p v-if="!filtered.length && !loading">没有找到符合条件的文章。</p>
    </div>
    <nav v-if="pageCount>1" class="coverage-tools" aria-label="文章目录翻页">
      <el-button size="small" :disabled="page<=1" @click="page--">上一页</el-button>
      <span>{{page}} / {{pageCount}} 页 · 共 {{filtered.length}} 篇</span>
      <el-button size="small" :disabled="page>=pageCount" @click="page++">下一页</el-button>
    </nav>
    <details v-if="dirs.length"><summary>查看已选清单（{{dirs.length}} / 100）</summary>
      <div v-for="entry in selection" :key="entry.directory" class="catalog-row">
        <span>{{entry.title || entry.directory}}</span>
        <small>{{entry.source_name || entry.directory}}{{entry.problem?' · '+entry.problem:''}}</small>
        <el-button link @click="dirs=dirs.filter(dir=>dir!==entry.directory)">移出</el-button>
      </div>
      <el-button link @click="dirs=[]">清空选稿</el-button>
    </details>
    <p v-else>已选 0 篇 / 最多 100 篇</p>
    <p v-if="!loading && unavailable" role="alert">有 {{unavailable}} 项选稿需要核对，请移出后重新选择。</p>
    <fieldset><legend>参与轮流分稿的模拟账号</legend>
      <div v-for="(title,p) in channels" :key="p" class="pool"><strong>{{title}}</strong>
        <label v-for="a in aliases[p]" :key="a"><input type="checkbox" v-model="chosen[p]" :value="a" :aria-label="title+a"> {{a}}</label>
        <input v-model.trim="custom[p]" placeholder="新账号别名" maxlength="40" @keyup.enter="add(p)"><el-button size="small" @click="add(p)">添加别名</el-button>
      </div>
    </fieldset>
    <fieldset><legend>文末延伸阅读</legend>
      <label>目标公众号文章 <el-input v-model.trim="destination" /></label>
      <div v-for="p in selectedPlatforms" :key="p" class="link-rule">
        <label>{{channels[p]}}读者链接
          <select v-model="links[p].mode">
            <option value="direct">公众号直链</option><option value="short">已有 HTTPS 短链</option><option value="omit">不追加延伸阅读</option>
          </select>
        </label>
        <template v-if="links[p].mode==='short'">
          <el-input v-model.trim="links[p].short" :aria-label="channels[p]+'短链'" placeholder="粘贴读者可打开的 HTTPS 短链" />
          <label><input type="checkbox" v-model="links[p].verified"> 已核对短链跳转到上方文章，且此平台所选账号允许使用</label>
        </template>
      </div>
    </fieldset>
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
    </div>
    <p v-if="storageError" role="status">{{storageError}}</p>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <template #footer><el-button @click="visible=false">关闭</el-button><el-button type="primary" :disabled="busy || loading || saving || (!pending && (!dirs.length || unavailable || !targets.length))" :loading="busy" @click="submit">{{pending?'重试上次提交':'提交选稿到后台'}}</el-button></template>
  </el-dialog>
</template>
<script setup>
import {ref, computed, reactive, watch} from 'vue'
import ScopeSchedule from './ScopeSchedule.vue'
import LibrarySettings from './LibrarySettings.vue'
const props=defineProps({api:Function})
const emit=defineEmits(['created','job'])
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
const coverageFilter=ref('all')
const sourceName=ref(''),fromDay=ref(''),toDay=ref(''),newestFirst=ref(false)
const sourceNames=computed(()=>[...new Set(items.value.map(item=>item.source_name).filter(Boolean))].sort((a,b)=>a.localeCompare(b,'zh-CN')))
const byDirectory=computed(()=>new Map(items.value.map(item=>[item.directory,item])))
const marked=reactive(new Map())
watch(dirs,chosenDirs=>{
  const chosenSet=new Set(chosenDirs)
  for(const dir of marked.keys())if(!chosenSet.has(dir))marked.delete(dir)
  for(const dir of chosenDirs){
    const item=byDirectory.value.get(dir)
    if(!marked.has(dir) && item)marked.set(dir,{directory:dir,title:item.title,source_name:item.source_name,key:item.selection_key})
  }
},{flush:'sync'})
const selection=computed(()=>dirs.value.map(directory=>{
  const entry=marked.get(directory),current=byDirectory.value.get(directory)
  const problem=!current?'目录不可用':!entry?.key || current.selection_key!==entry.key?'文章标识已变化':''
  return {...entry,directory,problem}
}))
const unavailable=computed(()=>selection.value.filter(item=>item.problem).length)
const invalidRange=computed(()=>fromDay.value && toDay.value && fromDay.value>toDay.value)
const states={ready:'已排期',running:'交付中',done:'已模拟交付',failed:'交付失败'}
const targets=computed(()=>Object.keys(channels).flatMap(platform=>chosen[platform].map(account=>({platform,account}))))
const selectedPlatforms=computed(()=>Object.keys(channels).filter(p=>chosen[p].length))
const missing=item=>selectedPlatforms.value.filter(p=>!item.deliveries?.[p])
const filtered=computed(()=>{
  const query=filter.value.trim().toLowerCase()
  const matching=items.value.filter(item=>{
    if(sourceName.value && item.source_name!==sourceName.value)return false
    const day=item.publish_time?.slice(0,10)
    if((fromDay.value && (!day || day<fromDay.value)) || (toDay.value && (!day || day>toDay.value)))return false
    if(!item.directory.toLowerCase().includes(query) && !item.title.toLowerCase().includes(query))return false
    if(coverageFilter.value==='unknown')return !item.source
    if(!selectedPlatforms.value.length || coverageFilter.value==='all')return true
    return item.source && (coverageFilter.value==='missing'?missing(item).length>0:missing(item).length===0)
  })
  return newestFirst.value?matching.sort((a,b)=>(b.publish_time || '').localeCompare(a.publish_time || '')):matching
})
const additions=computed(()=>{
  if(!selectedPlatforms.value.length || dirs.value.length>=100)return []
  const selected=new Set(dirs.value)
  const sources=new Set(items.value.filter(item=>selected.has(item.directory)).map(item=>item.source))
  const result=[]
  for(const item of filtered.value){
    if(!item.source || sources.has(item.source) || !missing(item).length)continue
    sources.add(item.source);result.push(item.directory)
    if(selected.size+result.length>=100)break
  }
  return result
})
const page=ref(1),pageSize=30
const pageCount=computed(()=>Math.max(1,Math.ceil(filtered.value.length/pageSize)))
const pageItems=computed(()=>filtered.value.slice((page.value-1)*pageSize,page.value*pageSize))
watch([filter,coverageFilter,selectedPlatforms,items,sourceName,fromDay,toDay,newestFirst],()=>{page.value=1})
function addMissing(){dirs.value=[...dirs.value,...additions.value]}
watch(selectedPlatforms,platforms=>{if(!platforms.length && ['missing','covered'].includes(coverageFilter.value))coverageFilter.value='all'})
const links=reactive(Object.fromEntries(Object.keys(channels).map(p=>[p,{mode:'direct',short:'',verified:false}])))
for(const p of Object.keys(channels)){
  watch(()=>[destination.value,links[p].mode,links[p].short,JSON.stringify(chosen[p])],()=>{links[p].verified=false},{flush:'sync'})
}
const sessionKey='gaoxu.reprint.selection.v1'
const pending=ref(null),storageError=ref('')
const needsSetup=ref(false)
let initialized=false

function editState(){
  return {root:root.value,entries:dirs.value.map(dir=>marked.get(dir)),targets:targets.value,destination:destination.value,
    links,rules,overrides:overrides.value,holidays:holidays.value,workdays:workdays.value}
}
function persist(){
  if(!initialized || loading.value)return
  try{
    if(dirs.value.length || pending.value){
      sessionStorage.setItem(sessionKey,JSON.stringify({form:editState(),pending:pending.value}))
    }else sessionStorage.removeItem(sessionKey)
    storageError.value=''
  }catch{storageError.value='当前浏览器无法保存选稿进度，请在此页完成提交并核对批次编号。'}
}
watch([dirs,targets,destination,links,rules,overrides,holidays,workdays],persist,{deep:true})

function stored(){
  try{return JSON.parse(sessionStorage.getItem(sessionKey) || 'null')}
  catch{return null}
}
function restoreForm(form){
  if(!Array.isArray(form?.entries) || !form.entries.length || form.entries.length>100 || form.root!==root.value ||
     !Array.isArray(form.targets) || !form.targets.every(t=>t && channels[t.platform] && valid(t.account)) ||
     !form.rules || !form.links?.zhihu || !form.links?.toutiao || !Array.isArray(form.overrides) ||
     typeof form.destination!=='string' || typeof form.holidays!=='string' || typeof form.workdays!=='string')return false
  for(const entry of form.entries){
    if(!entry || typeof entry.directory!=='string' || !entry.key)return false
  }
  for(const p of Object.keys(channels)){
    chosen[p]=form.targets.filter(t=>t.platform===p).map(t=>t.account)
    aliases[p]=[...new Set([...aliases[p],...chosen[p]])]
  }
  destination.value=form.destination
  Object.assign(rules,form.rules)
  overrides.value=form.overrides;holidays.value=form.holidays;workdays.value=form.workdays
  for(const p of Object.keys(channels))Object.assign(links[p],form.links[p])
  marked.clear()
  for(const entry of form.entries)marked.set(entry.directory,entry)
  dirs.value=form.entries.map(e=>e.directory)
  return true
}

// Compare the saved form before opening a previously committed batch. Edits
// made after an uncertain response still belong to the operator's next batch.
function finish(id,keepEdits){
  if(typeof id!=='string' || !/^[a-f0-9]{32}$/.test(id))throw Error('批次编号尚未确认，请再次查找原批次。')
  pending.value=null
  if(!keepEdits){dirs.value=[];filter.value=''}
  loading.value=false
  persist()
  visible.value=false
  emit('created',id)
}
async function findPending(){
  const candidate=pending.value
  if(!candidate)return false
  const found=await props.api('reprint-request?request_id='+encodeURIComponent(candidate.id))
  if(!found.id || pending.value?.id!==candidate.id)return false
  finish(found.id,candidate.form!==JSON.stringify(editState()))
  return true
}
async function recover(){
  if(busy.value || !pending.value)return
  busy.value=true;error.value=''
  try{
    if(!await findPending())error.value='后台尚未查到这次提交，可沿用原编号再次提交。'
  }catch(e){error.value=e.message}finally{busy.value=false}
}
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
  const policies={}
  for(const p of selectedPlatforms.value){
    const rule=links[p]
    if(rule.mode==='short'){
      if(!rule.verified)throw Error('请先核对'+channels[p]+'的短链及账号权限')
      policies[p]={...rule}
    }else if(rule.mode==='omit')policies[p]={mode:'omit'}
  }
  return {targets:targets.value,destination:destination.value,links:policies,rules:{...rules,holidays:off,workdays:on,overrides:active}}
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
  for(const p of Object.keys(channels)){
    const rule=saved.links?.[p]
    links[p].mode=rule?.mode || 'direct'
    links[p].short=rule?.short || ''
    links[p].verified=rule?.verified===true
  }
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
let openRequest=0
function applyLibrary(data){
  needsSetup.value=!data.root
  if(root.value && root.value!==data.root && dirs.value.length){
    dirs.value=[];notice.value='保存目录已更换，请从当前目录重新选稿'
  }
  root.value=data.root;items.value=data.items
}
async function reloadLibrary(folder){
  const request=++openRequest
  if(folder!==root.value)dirs.value=[]
  items.value=[];loading.value=true;error.value=''
  try{
    const data=await props.api('library')
    if(request===openRequest)applyLibrary(data)
  }catch(e){if(request===openRequest)error.value=e.message}
  finally{if(request===openRequest)loading.value=false}
}
async function open(){
  if(busy.value)return
  const request=++openRequest
  const savedSession=stored()
  if(!initialized && savedSession?.pending?.id && Array.isArray(savedSession.pending.data?.directories)){
    pending.value=savedSession.pending
  }
  visible.value=true;loading.value=true;error.value='';notice.value=''
  const [library,saved,bound]=await Promise.allSettled([props.api('library'),props.api('reprint-settings'),props.api('zhihu-draft-accounts')])
  if(request!==openRequest)return
  if(bound.status==='fulfilled'){
    const available=bound.value.items.filter(a=>a.user_id && valid(a.alias)).map(a=>a.alias)
    aliases.zhihu=[...new Set([...aliases.zhihu,...available])]
  }
  if(library.status==='fulfilled')applyLibrary(library.value)
  else{items.value=[];error.value=library.reason.message}
  if(saved.status==='fulfilled')applyDefaults(saved.value)
  else error.value=saved.reason.message
  if(library.status==='rejected' && savedSession?.form)root.value=savedSession.form.root
  if(restoreForm(savedSession?.form)){
    notice.value='已恢复本标签页的选稿和排期设置'
  }else if(savedSession?.form?.entries?.length && savedSession.form.root!==root.value){
    notice.value='保存目录已更换，请从当前目录重新选稿'
  }
  initialized=true
  if(pending.value){
    try{if(await findPending())return}catch(e){error.value=e.message}
  }
  loading.value=false
  if(library.status==='fulfilled')persist()
}
async function submit(){
  if(busy.value)return
  busy.value=true;error.value=''
  try{
    if(pending.value && await findPending())return
    if(!pending.value){
      if(unavailable.value)throw Error('选稿与当前目录不一致，请重新核对')
      const selections=Object.fromEntries(selection.value.map(item=>[item.directory,{key:item.key,title:item.title}]))
      const data={directories:[...dirs.value],expected_root:root.value,selections,...options()}
      pending.value={id:crypto.randomUUID(),form:JSON.stringify(editState()),data:JSON.parse(JSON.stringify(data))}
      persist()
    }
    const submission=pending.value
    try{
      const result=await props.api('reprints',{...submission.data,request_id:submission.id})
      finish(result.id,submission.form!==JSON.stringify(editState()))
    }catch(e){
      if(e.status===400 || e.status===409){
        // A copied browser tab may have committed this key first.
        if(await findPending())return
        pending.value=null;persist()
      }
      throw e
    }
  }catch(e){error.value=e.message}finally{busy.value=false}
}
</script>
<style scoped>
.coverage-tools{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px}.catalog-row{padding:4px 0;border-bottom:1px solid var(--el-border-color-lighter)}.catalog-row small{margin:0 8px 0 24px;color:var(--el-text-color-secondary)}.catalog-row .el-button{font-size:12px}
.link-rule{display:grid;gap:6px;margin:10px 0}.link-rule select{margin-left:8px;padding:4px}.articles{max-height:208px;min-height:50px;overflow:auto;border-block:1px solid var(--border);padding:6px 0}
.articles label{display:block;padding:5px;cursor:pointer}.pool,.rules{display:flex;flex-wrap:wrap;align-items:center;gap:8px 18px;margin:8px 0}.pool>input{width:95px}
.calendar{margin-top:8px}.calendar label{display:block;margin:6px 0}.defaults{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.defaults small{color:var(--el-text-color-secondary)}
fieldset{border:1px solid var(--border);border-radius:6px;margin:10px 0;padding:8px 14px}legend{font-weight:600}.rules input{width:72px}.pool label,.rules label{white-space:nowrap}
</style>
