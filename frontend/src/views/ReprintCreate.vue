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
    </fieldset>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <template #footer><el-button @click="visible=false">关闭</el-button><el-button type="primary" :disabled="!dirs.length || !targets.length" :loading="busy" @click="submit">生成待审核转载稿</el-button></template>
  </el-dialog>
</template>
<script setup>
import {ref, computed, reactive} from 'vue'
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
const rules=reactive({gap:900,account_cap:3,platform_cap:6,start_hour:9,end_hour:18})
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
async function open(){
  visible.value=true;loading.value=true;error.value=''
  try{const result=await props.api('library');root.value=result.root;items.value=result.items}
  catch(e){items.value=[];error.value=e.message}finally{loading.value=false}
}
let signature='',key=''
async function submit(){
  busy.value=true;error.value=''
  try{
    const data={directories:dirs.value,targets:targets.value,destination:destination.value,rules}
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
fieldset{border:1px solid var(--border);border-radius:6px;margin:10px 0;padding:8px 14px}legend{font-weight:600}.rules input{width:72px}.pool label,.rules label{white-space:nowrap}
</style>
