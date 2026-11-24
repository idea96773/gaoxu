<template>
  <section aria-label="知乎草稿人工核对">
    <div v-if="latest"><p>最近核对　{{date(latest.checked_at)}}<br>{{summary(latest.checks)}}</p>
      <p v-for="line in descriptions(latest)" :key="line" class="note">{{line}}</p></div>
    <p v-else-if="record.state==='verified'">此前已记录整体核对，尚无分项结果。</p>
    <el-button v-if="!editing" @click="start">{{latest?'更新核对结果':'逐项核对平台草稿'}}</el-button>
    <form v-else @submit.prevent="save">
      <p>请在「{{record.name}}」（{{record.user_id}}）的草稿编辑页核对。</p>
      <details v-if="snapshot.html" @toggle="showPreview=$event.target.open">
        <summary>对照本次冻结稿的正文、配图和文末链接</summary>
        <ReprintFrame v-if="showPreview" :html="snapshot.html" title="草稿核对用的冻结稿原样"/>
      </details>
      <DraftClipboardCheck v-if="snapshot.html" :snapshot="snapshot"/>
      <label v-if="!record.url">找到的编辑页地址 <el-input v-model.trim="url" placeholder="https://zhuanlan.zhihu.com/p/…/edit"/></label>
      <p v-if="dismissedReceipt && !record.url && url===dismissedReceipt.edit_url" role="alert">这个地址曾被标记为与本稿不符，请在指定账号中再次核对。</p>
      <div v-for="field in fields" :key="field.key">
        <label class="item"><span>{{field.label}}<small>{{field.hint}}</small></span>
          <select v-model="checks[field.key]" :aria-label="field.label+'核对结果'"><option value="pending">待核对</option>
            <option value="pass">符合冻结稿</option><option value="issue">发现问题</option></select></label>
        <el-input v-if="checks[field.key]==='issue'" v-model="notes[field.key]" type="textarea" :rows="2" maxlength="300"
          :aria-label="field.label+'问题说明'" :placeholder="field.label+'问题说明（选填）'"/>
      </div>
      <p v-if="stale" role="alert">核对记录已变化。请重新读取后填写。 <el-button link @click="start">重新读取</el-button></p>
      <el-button type="primary" native-type="submit" :loading="busy" :disabled="stale || !url || !hasResult">保存核对结果</el-button>
      <el-button :disabled="busy" @click="editing=false">返回</el-button>
    </form>
    <el-alert v-if="error" :title="error" type="error" :closable="false"/>
    <details v-if="entries.length>1"><summary>此前核对记录</summary>
      <div v-for="entry in entries.slice(1)" :key="entry.id"><p>{{date(entry.checked_at)}}　{{summary(entry.checks)}}</p>
        <p v-for="line in descriptions(entry)" :key="line" class="note">{{line}}</p></div>
    </details>
  </section>
</template>
<script setup>
import {computed,ref} from 'vue'
import ReprintFrame from './ReprintFrame.vue'
import DraftClipboardCheck from './DraftClipboardCheck.vue'
const props=defineProps({api:Function,path:String,record:Object,receiptUrl:String,dismissedReceipt:Object,entries:Array,snapshot:Object,date:Function})
const emit=defineEmits(['refresh','recorded'])
const editing=ref(false),busy=ref(false),error=ref(''),checks=ref({}),url=ref(''),revision=ref(0),state=ref('')
const latest=computed(()=>props.entries[0])
const notes=ref({}),showPreview=ref(false)
const fields=computed(()=>{
  const {title,images,link}=props.snapshot
  const footer=link.mode==='omit'?'冻结稿未追加延伸阅读链接':(link.short?link.short+' → ':'')+link.destination
  return [{key:'title',label:'标题',hint:title},{key:'layout',label:'正文与排版',hint:'核对段落顺序、完整性和署名'},
    {key:'images',label:'配图',hint:'冻结稿正文共 '+images.length+' 处，核对显示和位置'},
    {key:'footer',label:'文末链接',hint:footer}]
})
const labels={pass:'符合',issue:'有问题',pending:'待核对'}
const summary=values=>fields.value.map(f=>f.label+' '+labels[values[f.key]]).join(' · ')
const descriptions=entry=>fields.value.filter(f=>entry.notes?.[f.key]).map(f=>f.label+'　'+entry.notes[f.key])
const hasResult=computed(()=>Object.values(checks.value).some(x=>x!=='pending'))
const stale=computed(()=>revision.value!==(latest.value?.id || 0) || state.value!==props.record.state)
function start(){
  revision.value=latest.value?.id || 0;state.value=props.record.state;url.value=props.record.url || props.receiptUrl || ''
  checks.value=Object.fromEntries(fields.value.map(f=>[f.key,latest.value?.checks[f.key] || 'pending']))
  notes.value={...latest.value?.notes};showPreview.value=false
  error.value='';editing.value=true
}
async function save(){
  if(busy.value || stale.value || !hasResult.value)return
  busy.value=true;error.value=''
  try{
    await props.api(props.path,{action:'inspect',user_id:props.record.user_id,sha:props.record.sha,
      url:url.value,dismissed_at:props.dismissedReceipt?.dismissed_at,expected_state:state.value,revision:revision.value,checks:checks.value,
      notes:Object.fromEntries(Object.entries(notes.value).filter(([field])=>checks.value[field]==='issue'))})
    editing.value=false;emit('recorded')
  }catch(e){error.value=e.message}
  finally{busy.value=false;emit('refresh')}
}
</script>
<style scoped>
.item{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:10px 0}.item small{display:block;color:var(--el-text-color-secondary);overflow-wrap:anywhere}.item select{flex-shrink:0;padding:4px}
.note{white-space:pre-wrap;overflow-wrap:anywhere}summary{cursor:pointer}
</style>
