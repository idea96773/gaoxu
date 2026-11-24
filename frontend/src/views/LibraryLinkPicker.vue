<template>
  <details class="link-picker">
    <summary>按公众号原文链接选稿</summary>
    <label>公众号文章链接，每行一条，最多 100 条
      <el-input v-model="text" type="textarea" :rows="4" placeholder="https://mp.weixin.qq.com/s/…" />
    </label>
    <el-button :loading="loading" :disabled="!text.trim()" @click="match">匹配下载目录</el-button>
    <p v-if="error" role="alert">{{error}}</p>
    <template v-if="rows.length">
      <ol aria-label="链接匹配结果">
        <li v-for="row in rows" :key="row.line" :value="row.line">
          <template v-if="row.status==='matched'">{{row.candidates[0].title}}</template>
          <template v-else-if="row.status==='ambiguous'">
            <label>同一原文有多份下载
              <select v-model="choices[row.line]"><option value="">选择要使用的目录</option>
                <option v-for="c in row.candidates" :key="c.directory" :value="c.directory">{{c.directory}}</option>
              </select>
            </label>
          </template>
          <template v-else-if="row.status==='duplicate'">与第 {{row.first_line}} 行重复</template>
          <template v-else-if="row.status==='missing'">下载目录未匹配到此原文</template>
          <template v-else>链接格式无效</template>
          <small v-if="selectedSources.has(row.source)">已在选稿清单</small>
          <small v-if="row.source" class="source">{{row.source}}</small>
        </li>
      </ol>
      <p v-if="overLimit" role="alert">本批还可选 {{100-dirs.length}} 篇，请缩短链接列表或移出部分已选文章。</p>
      <el-button :disabled="!additions.length || overLimit" @click="$emit('add',additions)">加入选稿（{{additions.length}} 篇）</el-button>
    </template>
  </details>
</template>
<script setup>
import {computed,onBeforeUnmount,ref,watch} from 'vue'
const props=defineProps({api:Function,root:String,dirs:Array,byDirectory:Map})
const emit=defineEmits(['catalog','add'])
const text=ref(''),error=ref(''),loading=ref(false),rows=ref([]),choices=ref({})
const selectedSources=computed(()=>new Set(props.dirs.map(dir=>props.byDirectory.get(dir)?.source).filter(Boolean)))
const additions=computed(()=>{
  const result=[]
  for(const row of rows.value){
    if(!row.candidates?.length || selectedSources.value.has(row.source))continue
    const entry=row.status==='matched'?row.candidates[0]:row.candidates.find(c=>c.directory===choices.value[row.line])
    if(entry && props.byDirectory.get(entry.directory)?.selection_key===entry.selection_key)result.push(entry.directory)
  }
  return result
})
const overLimit=computed(()=>props.dirs.length+additions.value.length>100)
let request=0
watch(text,()=>{request++;rows.value=[];choices.value={};error.value='';loading.value=false},{flush:'sync'})
onBeforeUnmount(()=>{request++})
async function match(){
  const seq=++request
  loading.value=true;rows.value=[];choices.value={};error.value=''
  try{
    const data=await props.api('library/match-links?catalog=1',{urls:text.value,expected_root:props.root})
    if(seq!==request)return
    emit('catalog',data)
    rows.value=data.matches
  }catch(e){if(seq===request)error.value=e.message}
  finally{if(seq===request)loading.value=false}
}
</script>
<style scoped>
.link-picker li .source{display:block;margin-left:0}
.link-picker{margin:10px 0}.link-picker summary{cursor:pointer}.link-picker>label{display:grid;gap:5px;margin:8px 0}.link-picker ol{max-height:180px;overflow:auto;padding-left:3em}.link-picker li{padding:3px 0;overflow-wrap:anywhere}.link-picker small{margin-left:8px;color:var(--el-text-color-secondary)}.link-picker select{max-width:100%}
</style>
