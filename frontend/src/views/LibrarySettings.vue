<template>
  <details class="library-settings" @toggle="toggle">
    <summary>保存目录设置</summary>
    <label>Easy WeChat 保存路径
      <el-input v-model.trim="folder" :readonly="!settings?.editable" :disabled="loading || saving"
        placeholder="粘贴下载软件中显示的完整文件夹路径" />
    </label>
    <small v-if="settings && !settings.editable">当前目录由启动配置指定。</small>
    <div>
      <el-button v-if="settings?.editable" size="small" :disabled="disabled || loading || !folder"
        :loading="saving" @click="save">保存并读取目录</el-button>
      <el-button size="small" link :disabled="loading || saving" @click="read">重新读取设置</el-button>
    </div>
    <small role="status">{{message}}</small>
  </details>
</template>
<script setup>
import {ref} from 'vue'
const props=defineProps({api:Function,disabled:Boolean})
const emit=defineEmits(['saved'])
const settings=ref(null),folder=ref(''),loading=ref(false),saving=ref(false),message=ref('')
function toggle(event){if(event.target.open && !loading.value && !saving.value)read()}
async function read(){
  loading.value=true;message.value='';settings.value=null
  try{
    settings.value=await props.api('library-settings')
    folder.value=settings.value.root
  }catch(e){message.value=e.message}finally{loading.value=false}
}
async function save(){
  if(!settings.value?.editable || saving.value)return
  saving.value=true;message.value=''
  try{
    settings.value=await props.api('library-settings',{root:folder.value,revision:settings.value.revision})
    folder.value=settings.value.root
    message.value='目录已保存'
    emit('saved',folder.value)
  }catch(e){message.value=e.message}finally{saving.value=false}
}
</script>
<style scoped>
.library-settings{margin:8px 0}.library-settings summary{cursor:pointer}.library-settings label{display:grid;gap:5px;margin:8px 0}
</style>
