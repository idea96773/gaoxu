<template>
  <section>
    <BridgeConnection :bridge="bridge" :account="live"/>
    <el-table :data="rows" row-key="alias" empty-text="尚无知乎分稿别名，可在下方填写">
      <el-table-column prop="alias" label="分稿别名"/>
      <el-table-column label="绑定的知乎账号"><template #default="{row}">
        {{row.name || '待绑定'}}<small v-if="row.user_id">{{row.user_id}}</small>
      </template></el-table-column>
      <el-table-column width="82"><template #default="{row}"><el-button link @click="alias=row.alias">选择</el-button></template></el-table-column>
    </el-table>
    <label class="alias">分稿别名 <el-input v-model.trim="alias" maxlength="40" placeholder="输入或选择已有别名"/></label>
    <div class="actions">
      <el-button type="primary" :disabled="loading || busy || !live || !alias || current?.user_id===live.user_id" @click="change('bind')">绑定当前登录账号</el-button>
      <el-button v-if="current?.user_id" :disabled="loading || busy" @click="change('unlink')">解除后续分稿绑定</el-button>
      <el-button :disabled="busy" :loading="loading" @click="load">刷新账号</el-button>
      <el-button @click="$emit('find-drafts',alias)">{{alias?'查看此别名的待建草稿':'查看全部待建草稿'}}</el-button>
    </div>
    <p v-if="notice" role="status">{{notice}}</p>
    <el-alert v-if="error || connectionError" :title="error || connectionError" type="error" :closable="false"/>
  </section>
</template>
<script setup>
import {ref,computed,onMounted,onBeforeUnmount} from 'vue'
import {ElMessageBox} from 'element-plus'
import BridgeConnection from './BridgeConnection.vue'
const props=defineProps({api:Function,initialAlias:String})
const emit=defineEmits(['changed','find-drafts'])
const alias=ref(props.initialAlias || ''),rows=ref([]),live=ref(null),bridge=ref(null)
const loading=ref(false),busy=ref(false),notice=ref(''),error=ref('')
const connectionError=ref('')
const current=computed(()=>rows.value.find(row=>row.alias===alias.value))
async function load(){
  const seq=++sequence
  loading.value=true
  try{
    const data=await props.api('zhihu-draft-accounts')
    if(seq!==sequence)return
    rows.value=data.items;live.value=data.account;bridge.value=data.bridge;connectionError.value=''
  }catch(e){if(seq===sequence){connectionError.value=e.message;live.value=null;bridge.value=null;rows.value=[]}}
  finally{if(seq===sequence)loading.value=false}
}
async function change(action){
  const target=action==='bind'?live.value:current.value
  const data={action,alias:alias.value,user_id:target.user_id,revision:current.value?.revision || 0}
  const description=action==='bind'?'绑定到知乎账号「'+target.name+'」（'+target.user_id+'）':'解除与知乎账号「'+target.name+'」的后续分稿绑定'
  try{await ElMessageBox.confirm('将分稿别名「'+data.alias+'」'+description+'。已经锁定接收账号的稿件继续使用原账号。','核对知乎账号',{confirmButtonText:'确认',cancelButtonText:'返回'})}catch{return}
  busy.value=true;notice.value='';error.value=''
  try{
    await props.api('zhihu-draft-accounts',data)
    notice.value='已更新「'+data.alias+'」的知乎账号绑定'
    await load();emit('changed')
  }catch(e){const reason=e.message;await load();error.value=reason}
  finally{busy.value=false}
}
let timer,sequence=0
onMounted(()=>{load();timer=setInterval(()=>{if(!busy.value && !loading.value)load()},5000)})
onBeforeUnmount(()=>{clearInterval(timer);sequence++})
</script>
<style scoped>
.alias{display:grid;gap:6px;margin:16px 0;max-width:340px}.actions{display:flex;flex-wrap:wrap;gap:8px}.actions .el-button{margin:0}small{display:block;color:var(--el-text-color-secondary);overflow-wrap:anywhere}
</style>
