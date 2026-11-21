<template>
  <section class="distribution">
    <header class="heading">
      <div><h1>{{calendarView?'排期日历':searching?'跨批次查询':'文章分发'}}</h1><p v-if="!searching && !calendarView">本地导入 · 平台成稿审核 · 模拟排期与发布 · 北京时间</p></div>
      <div v-if="searching || calendarView" class="actions"><el-button v-if="searching" @click="openCalendar">排期日历</el-button><el-button @click="leaveSearch">返回批次管理</el-button></div>
      <div v-else class="actions"><el-button @click="openCalendar">排期日历</el-button><el-button @click="router.push({query:{...route.query,view:'search'}})">跨批次查询</el-button><el-button :type="online ? 'success' : 'warning'" plain @click="runtimeOpen=true">{{online ? '调度器在线' : '查看调度器状态'}}</el-button>
        <ReprintCreate :api="api" @created="openReprint" /><el-button @click="showImport=true">导入文章</el-button></div>
    </header>
    <el-alert v-if="error" :title="error" type="error" show-icon @close="error=''" />
    <ScheduleCalendar v-if="calendarView" :api="api" :names="names" :label="label" @platforms="names=$event" />
    <ArticleSearch v-else-if="searching" :api="api" :names="names" :date="date" :label="label" @job="openJob" @platforms="names=$event" />
    <template v-else>
    <div class="batchbar">
      <label>当前批次</label>
      <el-select v-model="bid" placeholder="请选择批次" @change="selectBatch" style="width:320px">
        <el-option v-for="b in batches" :key="b.id" :value="b.id" :label="date(b.created)+' · '+b.rows_total+' 行 · '+b.id.slice(0,6)" />
      </el-select>
      <template v-if="batch"><el-tag>{{label(batch.control)}}</el-tag>
        <el-button v-if="batch.control==='active'" @click="control('pause')">暂停</el-button>
        <el-button v-if="batch.control==='paused'" @click="control('resume')">继续</el-button>
        <el-button v-if="batch.control!=='cancelled'" @click="cancelBatch">取消批次</el-button></template>
    </div>
    <el-empty v-if="!batch" description="选择本地文章目录，或粘贴链接新建模拟批次" />
    <template v-else>
      <div class="metrics">
        <div v-for="[title,num] in (tab==='deliveries' || batch.reprint) ? deliveryMetrics : [['导入行数',batch.rows_total],['有效唯一文章',batch.unique],['重复行',batch.counts.duplicate||0],['失败步骤',batch.failures.length],['模拟回执',batch.simulated+' / '+batch.expected],['真实公开',batch.public]]" :key="title">
          <small>{{title}}</small><strong>{{num}}</strong></div>
      </div>
      <p v-if="batch.reprint" class="progress">共 {{batch.rows_total}} 篇　等待读取 {{batch.counts.preparing||0}}　读取中 {{batch.counts.reading||0}}　待审核 {{batch.counts.pending||0}}　交付中 {{batch.counts.processing||0}}　全部交付 {{batch.counts.done||0}}　重复 {{batch.counts.duplicate||0}}　资料异常 {{batch.counts.invalid||0}}　部分成功 {{batch.counts.partial||0}}　失败 {{batch.counts.failed||0}}　取消 {{batch.counts.cancelled||0}}</p>
      <p v-if="!batch.reprint && tab!=='deliveries'" class="progress">模拟流程进度　已完成 {{batch.counts.done||0}}　处理中 {{batch.counts.processing||0}}　部分成功 {{batch.counts.partial||0}}　失败 {{batch.counts.failed||0}}　取消 {{batch.counts.cancelled||0}}　无效行 {{batch.counts.invalid||0}}</p>
      <div v-if="batch.has_local" class="batchbar"><span>成稿　待生成 {{batch.editorial?.queued||0}}　生成中 {{batch.editorial?.generating||0}}　待审核 {{batch.editorial?.editing||0}}　已冻结 {{batch.editorial?.approved||0}}　失败 {{batch.editorial?.failed||0}}</span>
        <el-button :disabled="!batch.ai_ready || batch.control!=='active'" @click="generateBatch">批量生成平台成稿</el-button></div>
      <el-tabs v-model="tab">
        <el-tab-pane v-if="batch.reprint" label="转载审核与分稿" name="reprint">
          <ReprintReview @retry="retry" :key="batch.id" :batch="batch" :api="api" :names="names" :label="label" :date="date" @refresh="refresh" @job="openJob" />
        </el-tab-pane>
        <el-tab-pane v-else label="文章收件箱" name="rows">
          <el-table :data="batch.rows" max-height="490" @row-click="r=>openArticle(r.id)">
            <el-table-column prop="line" label="行" width="62"/><el-table-column prop="url" label="原文或目录" min-width="265" show-overflow-tooltip/>
            <el-table-column label="状态" width="110"><template #default="{row}">{{label(row.status)}}</template></el-table-column>
            <el-table-column label="阶段 / 原因" min-width="160"><template #default="{row}">{{row.note || row.stage.split(', ').map(label).join('、') || '—'}}</template></el-table-column>
            <el-table-column width="72"><template #default><el-button link type="primary">详情</el-button></template></el-table-column>
          </el-table>
        </el-tab-pane>
        <el-tab-pane :label="'冻结稿交付 ('+(batch.deliveries?.length||0)+')'" name="deliveries">
          <el-table :data="batch.deliveries" empty-text="审核定稿后可从文章详情加入模拟交付" @row-click="r=>openJob(r.id)">
            <el-table-column label="成稿"><template #default="{row}">#{{row.draft_id}} · 第 {{row.revision}} 版</template></el-table-column>
            <el-table-column label="平台 / 账号"><template #default="{row}">{{names[row.platform]}} · {{row.account}}</template></el-table-column>
            <el-table-column label="计划交付（北京时间）" min-width="155"><template #default="{row}">{{date(row.slot)}}</template></el-table-column>
            <el-table-column label="状态"><template #default="{row}">{{label(row.state)}}</template></el-table-column>
            <el-table-column label="模拟回执" prop="receipt" min-width="185"/>
          </el-table>
        </el-tab-pane>
        <el-tab-pane v-if="!batch.reprint" label="模拟流程排期" name="tasks">
          <el-table :data="ordered" max-height="490" @row-click="r=>openJob(r.id)">
            <el-table-column prop="row_id" label="文章 ID" width="90"/><el-table-column label="平台 / 账号"><template #default="{row}">{{names[row.platform]}} · {{row.account}}</template></el-table-column>
            <el-table-column label="计划时间" min-width="160"><template #default="{row}">{{date(row.slot)}}</template></el-table-column>
            <el-table-column label="状态"><template #default="{row}">{{label(row.state)}}</template></el-table-column>
            <el-table-column label="模拟回执" min-width="170"><template #default="{row}">{{row.receipt||'—'}}</template></el-table-column>
          </el-table>
        </el-tab-pane>
        <el-tab-pane :label="'失败待办 ('+batch.failures.length+')'" name="failed">
          <el-table :data="batch.failures"><el-table-column prop="row_id" label="文章 ID"/><el-table-column label="步骤"><template #default="{row}">{{label(row.stage)}} {{names[row.platform]}}</template></el-table-column><el-table-column prop="failure" label="原因"/><el-table-column label="操作"><template #default="{row}"><el-button link type="primary" @click="retry(row.id)">仅重试此项</el-button></template></el-table-column></el-table>
        </el-tab-pane>
        <el-tab-pane label="平台统计" name="stats">
          <el-table :data="stats"><el-table-column prop="platform" label="平台"/><el-table-column prop="done" label="模拟成功"/><el-table-column prop="failed" label="发布失败"/><el-table-column prop="pending" label="待执行"/><el-table-column prop="rate" label="模拟终态成功率"/></el-table>
        </el-tab-pane>
      </el-tabs>
    </template>
    </template>
    <el-dialog v-model="runtimeOpen" title="调度器运行状态" width="min(580px, 95vw)">
      <RuntimeStatus v-if="runtimeOpen" :api="api" :date="date" />
    </el-dialog>
    <el-dialog v-model="showImport" title="导入文章" width="min(660px, 94vw)" :close-on-click-modal="false">
      <el-radio-group v-model="source"><el-radio-button value="library">固定保存目录</el-radio-button><el-radio-button value="folder">另选文件夹</el-radio-button><el-radio-button value="url">链接模拟</el-radio-button></el-radio-group>
      <div v-if="source==='library'" class="input-block"><p>{{libraryRoot || 'Easy WeChat 保存目录'}}</p>
        <el-button @click="scan" :loading="scanning">读取新文章</el-button>
        <small v-if="scanned">共 {{libraryItems.length}} 个文章目录，{{libraryItems.filter(x=>!x.known).length}} 个未入库。已选 {{selectedDirs.length}} 个。</small>
        <el-select v-if="scanned" v-model="selectedDirs" multiple filterable collapse-tags :multiple-limit="100" placeholder="选择要导入的文章" style="width:100%;margin-top:10px">
          <el-option v-for="item in libraryItems" :key="item.directory" :value="item.directory" :label="item.directory" :disabled="item.known"/>
        </el-select></div>
      <div v-else-if="source==='folder'" class="input-block"><p>选择一篇文章的文件夹，或包含多篇文章的上级目录。正文使用 Markdown，HTML 和 img 图片随文归档。</p>
        <input type="file" webkitdirectory multiple @change="chooseFiles"><small v-if="files.length">已选择 {{files.length}} 个正文和图片文件</small></div>
      <div v-else class="input-block"><p>每行一个公众号链接。链接模式使用模拟正文，不访问微信。</p><el-input v-model="urls" type="textarea" :rows="5" placeholder="https://mp.weixin.qq.com/s/…"/><el-button link type="primary" @click="fillSample">填入 100 篇模拟链接</el-button></div>
      <el-checkbox-group v-model="platforms"><el-checkbox v-for="(name,key) in names" :key="key" :value="key">{{name}}</el-checkbox></el-checkbox-group>
      <div v-for="p in platforms" :key="p" class="input-block"><label>{{names[p]}}账号别名（多个用逗号隔开）<el-input v-model="accounts[p]" placeholder="主号，科普号" /></label></div>
      <el-radio-group v-model="routing"><el-radio value="rotate">同平台多个账号轮流分稿</el-radio><el-radio value="all">每篇分配给全部所选账号</el-radio></el-radio-group>
      <div class="settings">
        <label>平台间隔（秒） <el-input-number v-model="gap" :min="1" :max="86400" /></label>
        <label>账号每日上限 <el-input-number v-model="cap" :min="1" :max="1000"/></label>
        <label>平台每日上限 <el-input-number v-model="platformCap" :min="1" :max="10000"/></label></div>
      <el-alert v-if="formError" :title="formError" type="error" :closable="false"/>
      <template #footer><el-button @click="showImport=false">关闭</el-button><el-button type="primary" :loading="submitting" @click="submit">创建模拟批次</el-button></template>
    </el-dialog>
    <el-drawer v-model="articleOpen" title="文章详情" size="min(1080px, 95vw)">
      <template v-if="article"><h2>{{article.document?.title || article.row.note || '待处理文章'}}</h2>
        <p>{{article.row.url}}</p><p v-if="article.row.owner">关联原任务 #{{article.row.owner}}</p>
        <template v-if="article.document"><p>{{article.document.source_name}} · {{article.document.author}} · {{article.document.publish_time || '无原始发布时间'}}</p>
          <p>{{article.document.mode==='local-dry-run'?'本地正文':'模拟正文'}} · 图片 {{article.document.images.length}} 张 · 来源权利 {{article.document.copyright_status==='unknown'?'待核实':'测试样本'}}</p>
          <details><summary>完整 Markdown</summary><pre>{{article.document.markdown_content}}</pre></details>
          <div class="pictures"><img v-for="s in article.document.images" :key="s" :src="s" alt="文章配图" loading="lazy"/></div>
        </template>
        <EditorialPanel @view-job="openJob" v-if="article.document && article.document.mode==='local-dry-run'" :key="article.row.owner||article.row.id" :rid="article.row.owner||article.row.id" :source="article.document" />
        <h3>模拟平台版本</h3><details v-for="v in article.variants" :key="v.platform"><summary>{{names[v.platform]}} · {{v.payload.format}} · v{{v.revision}}</summary><pre>{{v.payload.title}}\n{{v.payload.body}}</pre></details>
        <h3>执行记录</h3><div v-for="j in article.jobs" :key="j.id" class="record"><span>{{label(j.stage)}} {{names[j.platform]}} {{j.account}}</span><el-button link @click="openJob(j.id)">{{label(j.state)}} ›</el-button></div>
      </template>
    </el-drawer>
    <el-dialog v-model="jobOpen" title="任务与回执" width="min(570px, 95vw)">
      <template v-if="job"><h3>{{label(job.job.stage)}} · {{names[job.job.platform]||'文章'}} {{job.job.account}}</h3>
        <div v-if="job.delivery"><b>冻结稿 #{{job.delivery.draft_id}} · 第 {{job.job.revision}} 版</b>
          <p>{{job.delivery.snapshot.title}}</p>
          <details><summary>查看交付正文与配图</summary><pre>{{job.delivery.snapshot.text}}</pre>
            <div class="pictures"><img v-for="url in job.delivery.snapshot.images" :key="url" :src="url" alt="冻结稿配图"></div></details>
          <p>快照 SHA-256　<code style="overflow-wrap:anywhere">{{job.delivery.sha}}</code></p>
          <p v-if="job.delivery.received">模拟接收端核对　{{job.delivery.received.matches?'与冻结稿一致':'内容不一致'}}</p>
        </div>
        <PlatformDraftPanel v-if="jobOpen && job.job.platform==='zhihu' && job.delivery?.snapshot.kind==='reprint'" :key="job.job.id" :job="job" :api="api" />
        <div v-for="entry in job.delivery?.reschedules" :key="entry.changed+'-'+entry.after_slot" class="record">
          <small>改期于 {{date(entry.changed)}}<br>{{date(entry.before_slot)}} → {{date(entry.after_slot)}}</small>
        </div>
        <p>状态 {{label(job.job.state)}} · 执行 {{job.job.attempts}} 次</p>
        <p>计划 {{date(job.job.slot)}}　完成 {{date(job.job.finished)}}</p><p>模拟回执 {{job.job.receipt||'—'}}</p><p>{{job.job.failure}}</p>
        <div v-if="['publish','deliver'].includes(job.job.stage) && job.job.state==='ready'"><label>改期（北京时间） <input v-model="newTime" type="datetime-local"></label> <el-button @click="reschedule">保存改期</el-button></div>
        <el-button v-if="job.job.state==='failed'" @click="retry(job.job.id)">仅重试此项</el-button>
        <div v-for="h in job.history" :key="h.seq" class="record"><span>#{{h.seq}}　{{date(h.started)}}</span><span>{{label(h.outcome)}} {{h.reason}}</span></div>
      </template>
    </el-dialog>
  </section>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import ScheduleCalendar from './ScheduleCalendar.vue'
import ArticleSearch from "./ArticleSearch.vue"
import PlatformDraftPanel from './PlatformDraftPanel.vue'
import RuntimeStatus from "./RuntimeStatus.vue"
import ReprintCreate from "./ReprintCreate.vue"
import ReprintReview from "./ReprintReview.vue"
import EditorialPanel from "./EditorialPanel.vue"
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
const runtimeOpen=ref(false)
const route=useRoute(),router=useRouter()
const searching=computed(()=>route.query.view==='search')
const calendarView=computed(()=>route.query.view==='calendar')
function openCalendar(){
  const query={view:'calendar'}
  for(const k of ['batch','platform','account'])if(route.query[k])query[k]=route.query[k]
  if(searching.value && route.query.start)query.month=String(route.query.start).slice(0,7)
  router.push({query})
}
async function leaveSearch(){await router.push({query:bid.value?{batch:bid.value}:{}});await refresh()}
const batches=ref([]),names=ref({}),bid=ref(String(route.query.batch||'')),batch=ref(null),error=ref(''),online=ref(false)
const tab=ref(route.query.tab==='deliveries'?'deliveries':'rows'),showImport=ref(false),source=ref('library'),files=ref([]),urls=ref(''),platforms=ref(['zhihu','xiaohongshu']),routing=ref('rotate'),gap=ref(60),cap=ref(10),platformCap=ref(20)
const accounts=ref({})
try { accounts.value=JSON.parse(localStorage.getItem('distribution.account-aliases') || '{}') || {} } catch {}
function targetsForImport(){
  return platforms.value.flatMap(platform=>{
    const aliases=[...new Set((accounts.value[platform] ?? '模拟账号').split(/[,，、;；\n]/).map(a=>a.trim()).filter(Boolean))]
    if(!aliases.length)throw Error('请填写'+names.value[platform]+'账号别名')
    return aliases.map(account=>({platform,account}))
  })
}
const libraryRoot=ref(''),libraryItems=ref([]),selectedDirs=ref([]),scanning=ref(false),scanned=ref(false)
async function scan(){scanning.value=true;formError.value='';try{const info=await api('library');libraryRoot.value=info.root;libraryItems.value=info.items;selectedDirs.value=info.items.filter(x=>!x.known).slice(0,100).map(x=>x.directory);scanned.value=true}catch(e){formError.value=e.message}finally{scanning.value=false}}
const formError=ref(''),submitting=ref(false),article=ref(null),articleOpen=ref(false),job=ref(null),jobOpen=ref(false),newTime=ref('')
const labels={preparing:'等待读取',reading:'正在读取',reprint_prepare:'读取转载资料',done:'已完成',ready:'排队中',active:'运行中',paused:'已暂停',cancelled:'已取消',processing:'处理中',partial:'部分成功',failed:'失败',invalid:'无效',duplicate:'重复',running:'执行中',interrupted:'已中断',ingest:'导入正文',normalize:'标准化',analyze:'模拟分析',adapt:'平台适配',check:'质量检查',plan:'生成排期',publish:'模拟发布',deliver:'冻结稿模拟交付'}
const label=x=>labels[x]||x
const date=t=>t?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(t*1000)):'—'
const deliveryMetrics=computed(()=>{
  const tasks=batch.value?.deliveries||[]
  const count=state=>tasks.filter(t=>t.state===state).length
  return [['冻结稿任务',tasks.length],['排队中',count('ready')],['执行中',count('running')],['已模拟交付',count('done')],['交付失败',count('failed')],['已取消',count('cancelled')]]
})
const ordered=computed(()=>[...(batch.value?.publishes||[])].sort((a,b)=>a.slot-b.slot))
const stats=computed(()=>[...new Set((batch.value?.targets||[]).map(t=>t.platform))].map(key=>{
  const n=batch.value.platforms[key]||{},done=n.done||0,failed=n.failed||0
  return {platform:names.value[key],done,failed,pending:(n.ready||0)+(n.running||0),rate:done+failed?Math.round(done*100/(done+failed))+'%':'—'}
}))
async function api(path,data){
  const opts=data===undefined?{}:{method:'POST',headers:data instanceof FormData?{'X-Distribution-Import':'folder'}:{'Content-Type':'application/json'},body:data instanceof FormData?data:JSON.stringify(data)}
  const response=await fetch('/api/distribution/'+path,opts)
  if(!response.ok){const err=await response.json().catch(()=>({}));throw Error(err.error||'请求失败 '+response.status)}
  return response.json()
}
async function detail(){const id=bid.value;if(id){const result=await api('batches/'+id);if(bid.value===id){if(batch.value?.id!==id){if(result.reprint && tab.value!=='deliveries')tab.value='reprint';else if(!result.reprint && tab.value==='reprint')tab.value='rows'}batch.value=result}}}
async function refresh(){if(searching.value || calendarView.value)return;try{const data=await api('batches?summary=1');batches.value=data.batches;names.value=data.platforms;for(const p of Object.keys(data.platforms))accounts.value[p]??='模拟账号';online.value=!!data.worker_seen && data.checked>=data.worker_seen && data.checked-data.worker_seen<18
  if(!bid.value || !data.batches.some(b=>b.id===bid.value))bid.value=data.batches[0]?.id||''
  await detail();error.value=''}catch(e){online.value=false;error.value=e.message}}
async function selectBatch(){await router.replace({query:{batch:bid.value}});await refresh()}
async function openReprint(id){bid.value=id;tab.value='reprint';await selectBatch()}
function chooseFiles(e){files.value=[...e.target.files].filter(f=>/\.(html|md|jpg|jpeg|png|gif|webp|other)$/i.test(f.name))}
function fillSample(){const prefix=crypto.randomUUID().slice(0,8);urls.value=Array.from({length:100},(_,i)=>'https://mp.weixin.qq.com/s/demo-'+prefix+'-'+(i+1)).join('\n')}
let lastSignature='',requestId=''
async function submit(){
  formError.value='';submitting.value=true
  try{
    const input=source.value==='library'?selectedDirs.value:source.value==='folder'?files.value.map(f=>[f.webkitRelativePath,f.size,f.lastModified]):urls.value.trim()
    if(!platforms.value.length||!input.length)throw Error('请选择文章和至少一个模拟平台')
    const options={targets:targetsForImport(),account_routing:routing.value,rules:{gap:gap.value,account_cap:cap.value,platform_cap:platformCap.value}}
    const sig=JSON.stringify([source.value,options,input])
    if(sig!==lastSignature){lastSignature=sig;requestId=crypto.randomUUID()} options.request_id=requestId
    let result
    if(source.value==='library'){
      result=await api('library',{...options,directories:selectedDirs.value});scanned.value=false;selectedDirs.value=[]
    }else if(source.value==='folder'){
      const form=new FormData();form.append('options',JSON.stringify(options))
      for(const f of files.value)form.append('files',f,f.webkitRelativePath)
      result=await api('folders',form)
    }else result=await api('batches',{...options,urls:urls.value})
    localStorage.setItem('distribution.account-aliases',JSON.stringify(accounts.value))
    bid.value=result.id;await selectBatch();showImport.value=false;lastSignature='';tab.value='rows'
  }catch(e){formError.value=e.message}finally{submitting.value=false}
}
async function action(fn){try{await fn();await refresh()}catch(e){ElMessage.error(e.message)}}
const control=a=>action(()=>api('batches/'+bid.value+'/'+a,{}))
async function generateBatch(){
  const id=bid.value
  try{await ElMessageBox.confirm('确认此批全部本地文章均已获授权，可发送正文给 DeepSeek？将为所选账号生成缺少的成稿。','批量成稿')}catch{return}
  await action(async()=>{const result=await api('batches/'+id+'/editorial',{authorized:true});ElMessage.success('已新增 '+result.created+' 项成稿任务')})
}
async function cancelBatch(){try{await ElMessageBox.confirm('取消本批次未完成的任务？已完成的模拟回执将保留。','取消批次');await control('cancel')}catch{}}
async function openArticle(id){await action(async()=>{article.value=await api('articles/'+id);articleOpen.value=true})}
async function openJob(id){await action(async()=>{job.value=await api('jobs/'+id);newTime.value='';jobOpen.value=true})}
async function retry(id){await action(async()=>{await api('jobs/'+id+'/retry',{});if(jobOpen.value)job.value=await api('jobs/'+id);ElMessage.success('已重新排队')})}
async function reschedule(){await action(async()=>{const t=Date.parse(newTime.value+':00+08:00')/1000;if(!Number.isFinite(t))throw Error('请选择北京时间');await api('jobs/'+job.value.job.id+'/reschedule',{time:t,expected_slot:job.value.job.slot});job.value=await api('jobs/'+job.value.job.id)})}
let timer,closed=false
async function poll(){await refresh();if(!closed)timer=setTimeout(poll,3000)}
onMounted(poll)
onBeforeUnmount(()=>{closed=true;clearTimeout(timer)})
</script>

<style scoped>
.distribution{max-width:1350px;margin:0 auto;color:var(--text-primary);padding:8px 4px}
.heading,.actions,.batchbar,.record{display:flex;align-items:center;gap:12px}
.actions{flex-wrap:wrap}.heading,.record{justify-content:space-between}.heading{margin-bottom:28px}.heading h1{font-size:30px;letter-spacing:.03em;margin:0}
p,small{color:var(--text-secondary);line-height:1.65}.heading p{margin:6px 0 0}.batchbar{flex-wrap:wrap;margin:18px 0}
.metrics{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));border-block:1px solid var(--border);padding:22px 0;gap:14px}
.metrics small,.metrics strong{display:block}.metrics strong{font-size:25px;margin-top:7px;font-variant-numeric:tabular-nums}
.progress{font-size:13px;margin:16px 0}.input-block{padding:10px 0 16px}.input-block small{display:block}
.settings{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:15px}.settings label{display:grid;gap:6px}
.record,details{padding:12px 0;border-bottom:1px solid var(--border)}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7}
.pictures{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}.pictures img{max-width:100px;max-height:100px}.el-drawer p{overflow-wrap:anywhere}
@media(max-width:800px){.metrics{grid-template-columns:repeat(3,1fr)}.heading{align-items:flex-start;flex-wrap:wrap}.settings{grid-template-columns:1fr}}
</style>
