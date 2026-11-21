<template>
  <section class="draft">
    <h3>知乎草稿箱</h3>
    <p>当前连接账号　{{account?.name || (connected?'扩展已连接，请登录知乎':'草稿桥未连接')}}</p>
    <p v-if="record">目标账号　{{record.name}}　·　{{labels[record.state]}}
      <a v-if="record.url" :href="record.url" target="_blank" rel="noopener noreferrer">打开知乎草稿</a>
    </p>
    <p v-if="record?.reason">{{record.reason}}</p>
    <p v-if="!account && !record">启动稿序草稿桥并在文章同步助手所用的浏览器登录知乎后，可选择将本稿保存到该账号。</p>
    <el-button v-if="canSend" type="primary" :loading="busy" @click="enqueue">保存到 {{account.name}} 的草稿箱</el-button>
    <el-button v-if="record && ['queued','preparing'].includes(record.state)" :disabled="busy" @click="change({action:'cancel'})">取消草稿任务</el-button>
    <template v-if="record && ['saved','unknown'].includes(record.state)">
      <el-input v-if="record.state==='unknown'" v-model.trim="found" placeholder="在该账号草稿箱找到的知乎编辑页地址"/>
      <el-checkbox v-model="checked">我已在该账号的草稿箱核对标题、正文、配图和文末链接</el-checkbox>
      <el-button :disabled="busy || !checked || !(record.url||found)" @click="change({action:'confirm',checked:true,url:record.url||found})">记录人工核对结果</el-button>
    </template>
    <el-alert v-if="error" :title="error" type="error" :closable="false"/>
  </section>
</template>
<script setup>
import {ref,computed,onMounted,onBeforeUnmount} from 'vue'
import {ElMessageBox} from 'element-plus'
const props=defineProps({api:Function,job:Object})
const connected=ref(false),account=ref(null),record=ref(null),busy=ref(false),error=ref(''),checked=ref(false),found=ref('')
const labels={queued:'等待保存',preparing:'准备配图',sending:'正在请求保存',saved:'平台已返回草稿，待核对',verified:'已人工核对',unknown:'回执不确定，需核对草稿箱',failed:'准备失败',cancelled:'已取消'}
const path='jobs/'+props.job.job.id+'/platform-draft'
const canSend=computed(()=>account.value && props.job.job.state!=='cancelled' && (!record.value ||
    (['failed','cancelled'].includes(record.value.state) && record.value.user_id===account.value.user_id)))
let stopped=false,timer,sequence=0
async function load(){
  const n=++sequence
  try{const info=await props.api(path);if(!stopped && n===sequence){connected.value=info.connected;account.value=info.account;record.value=info.record}}
  catch(e){if(!stopped && n===sequence){connected.value=false;account.value=null;error.value=e.message}}
}
async function poll(){await load();if(!stopped)timer=setTimeout(poll,4000)}
async function change(data){
  busy.value=true;error.value=''
  try{await props.api(path,data);checked.value=false;found.value=''}catch(e){error.value=e.message}
  finally{await load();busy.value=false}
}
async function enqueue(){
  const target=account.value
  try{await ElMessageBox.confirm('将已冻结的《'+props.job.delivery.snapshot.title+'》及其配图发送至知乎账号「'+target.name+'」的草稿箱。','确认保存平台草稿',{confirmButtonText:'仅保存草稿',cancelButtonText:'返回'})}catch{return}
  await change({action:'queue',consent:true,user_id:target.user_id,sha:props.job.delivery.sha})
}
onMounted(poll)
onBeforeUnmount(()=>{stopped=true;clearTimeout(timer)})
</script>
<style scoped>
.draft{border-block:1px solid var(--el-border-color);margin:16px 0;padding:8px 0 16px}.draft p{line-height:1.6}.draft a{color:var(--el-color-primary)}.draft .el-checkbox{height:auto;white-space:normal;margin:10px 0}
</style>
