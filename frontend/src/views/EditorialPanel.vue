<template>
<section class="editor">
  <h3>成稿与审核</h3>
  <p v-if="error" role="alert">{{error}}</p>
  <template v-if="info">
    <select v-model="selected" aria-label="成稿账号" @change="switchChannel">
      <option v-for="(t,i) in info.targets" :key="t.platform+t.account" :value="i">{{names[t.platform]}} · {{t.account}}</option>
    </select>
    <template v-if="channel">
      <details><summary>此账号的延伸阅读设置</summary>
        <label>读者链接<select v-model="link.mode"><option value="omit">省略链接</option><option value="short">使用已有短链</option><option value="direct">使用公众号直链</option></select></label>
        <template v-if="link.mode!=='omit'">
          <label>目标公众号文章<input v-model="link.destination"></label>
          <label v-if="link.mode==='short'">读者可点击的 HTTPS 短链<input v-model="link.short"></label>
          <label>链接文案<input v-model="link.label"></label>
          <label><input type="checkbox" v-model="link.verified"> 已核对该账号的外链规则与实际落地文章</label>
        </template>
        <button :disabled="busy" @click="run(saveLink)">保存账号链接设置</button>
      </details>
      <div class="toolbar">
        <button v-if="!current || ['failed','approved'].includes(current.state)" :disabled="busy" @click="run(()=>create('manual'))">新建编辑稿</button>
        <button v-if="!current || ['failed','approved'].includes(current.state)" :disabled="busy||!info.ai_ready" @click="run(()=>create('model'))">DeepSeek 生成新稿</button>
        <small v-if="!info.ai_ready">后台尚未配置 DeepSeek，可录入成稿或由 Agent 提交。</small>
      </div>
      <div v-if="current">
        <p>第 {{current.rev}} 版 · {{states[current.state]}} · {{current.provider}} {{current.reason}}</p>
        <div v-if="current.state==='editing'" class="compare">
          <div><strong>原文核对</strong><pre>{{source.markdown_content}}</pre></div>
          <div><label>平台标题<input v-model="title" maxlength="200" @input="invalidate"></label>
          <label>平台正文<textarea v-model="body" rows="14" @input="invalidate"></textarea></label></div>
        </div>
        <template v-if="current.state==='editing'">
          <div class="toolbar"><button :disabled="busy" @click="run(()=>change('save'))">保存修改</button>
          <button :disabled="busy" @click="run(()=>change('preview'))">预览定稿</button></div>
          <template v-if="preview"><pre>{{preview.title}}\n{{preview.text}}</pre>
            <label><input type="checkbox" v-model="rights"> 已确认正文及配图可在此账号使用</label>
            <label><input type="checkbox" v-model="facts"> 已对照原文核实事实、医学证据及延伸阅读</label>
            <button :disabled="busy||!rights||!facts" @click="run(()=>change('approve'))">审核通过并冻结</button>
          </template>
        </template>
        <details v-for="d in approved" :key="d.id"><summary>已审核 · 第 {{d.rev}} 版</summary>
          <div v-if="d.delivery_job"><button @click="emit('view-job',d.delivery_job)">查看模拟交付任务 #{{d.delivery_job}}</button></div>
          <div v-else class="toolbar"><label>交付时间（北京时间，留空按规则排期）<input v-model="times[d.id]" type="datetime-local" step="1"></label>
            <button :disabled="busy" @click="run(()=>schedule(d))">将此冻结版加入模拟交付</button></div>
          <pre>{{d.frozen.title}}\n{{d.frozen.text}}</pre>
          <div class="toolbar"><button @click="run(()=>copy(d))">复制全文</button><a :href="'/api/distribution/editorial/'+d.id+'/export'" download>下载图文包</a></div>
        </details>
      </div>
    </template><p v-else>本批次所选平台尚未接入成稿编辑。</p>
  </template>
</section>
</template>
<script setup>
import { computed, ref, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
const emit=defineEmits(["view-job"])
const times=ref({})
const props=defineProps({rid:Number,source:Object})
const names={zhihu:'知乎',xiaohongshu:'小红书',toutiao:'今日头条'}
const states={editing:'待审核',queued:'等待生成',generating:'生成中',failed:'生成失败',approved:'已冻结'}
const info=ref(null),selected=ref(0),link=ref({}),title=ref(''),body=ref(''),preview=ref(null)
const rights=ref(false),facts=ref(false),busy=ref(false),error=ref('')
const channel=computed(()=>info.value?.targets[selected.value])
const versions=computed(()=>info.value?.drafts.filter(d=>d.platform===channel.value?.platform && d.account===channel.value?.account)||[])
const current=computed(()=>versions.value[0])
const approved=computed(()=>versions.value.filter(d=>d.state==='approved'))
const base='/api/distribution/',endpoint='articles/'+props.rid+'/editorial'
async function api(url,data){
  const response=await fetch(base+url,data?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}:{})
  const result=await response.json();if(!response.ok)throw Error(result.error||'请求失败');return result
}
function invalidate(){preview.value=null;rights.value=false;facts.value=false}
function switchChannel(){clearTimeout(timer);if(current.value && ['queued','generating'].includes(current.value.state))timer=setTimeout(()=>run(load),2000);link.value={...channel.value?.link};title.value=current.value?.title||'';body.value=current.value?.body||'';invalidate()}
let timer,disposed=false
async function load(){
  clearTimeout(timer);const result=await api(endpoint);if(disposed)return
  info.value=result;switchChannel()

}
async function run(fn){if(busy.value)return;busy.value=true;error.value='';try{await fn()}catch(e){if(e!=='cancel'&&e!=='close')error.value=e.message||'操作失败'}finally{busy.value=false}}
function target(){return {platform:channel.value.platform,account:channel.value.account}}
async function saveLink(){await api(endpoint,{...target(),action:'link',link:link.value});channel.value.link={...link.value};invalidate();ElMessage.success('已保存此账号设置')}
async function create(action){
  if(action==='model')await ElMessageBox.confirm('确认已获授权，可将这篇本地正文发送给 DeepSeek 生成此账号的成稿？','生成平台成稿')
  await api(endpoint,{...target(),action,authorized:action==='model'});await load()
}
async function change(action){
  const data={action,title:title.value,body:body.value,edit_no:current.value.edit_no,rights:rights.value,facts:facts.value,preview:preview.value}
  const result=await api('editorial/'+current.value.id,data)
  if(action==='preview'){preview.value=result;rights.value=false;facts.value=false}else await load()
}
async function schedule(d){
  const local=times.value[d.id]
  const time=local?Date.parse(local+'+08:00')/1000:null
  if(local && !Number.isInteger(time))throw Error('请选择有效的北京时间')
  const result=await api('editorial/'+d.id+'/schedule',{time})
  await load();emit('view-job',result.id)
}
async function copy(d){await navigator.clipboard.writeText(d.frozen.title+'\n\n'+d.frozen.text);ElMessage.success('已复制冻结版全文')}
onMounted(()=>run(load));onBeforeUnmount(()=>{disposed=true;clearTimeout(timer)})
</script>
<style scoped>
.editor{border-block:1px solid #d7dee5;padding:12px 0 20px}.editor h3{margin:0 0 12px}.editor label,.editor details{display:block;margin:10px 0}.editor select,.editor input:not([type=checkbox]),.editor textarea{display:block;box-sizing:border-box;width:100%;padding:8px;border:1px solid #aebac7;border-radius:4px;color:inherit;background:var(--el-bg-color);font:inherit}.editor select{width:auto;max-width:100%}.editor button{padding:6px 12px;cursor:pointer}.toolbar,.compare{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:10px 0}.compare{align-items:stretch}.compare>div{flex:1;min-width:min(250px,100%)}.editor pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:430px;overflow:auto;line-height:1.6}.editor [role=alert]{color:#b34128}
</style>
