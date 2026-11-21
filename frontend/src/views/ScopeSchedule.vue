<template>
  <details class="scope">
    <summary>单独设置平台或账号<span v-if="enabled.length">（已设置 {{enabled.length}} 项）</span></summary>
    <p v-if="!scopes.length">先勾选参与分稿的账号。</p>
    <template v-else>
      <label>设置对象 <select :value="current.key" @change="selected=$event.target.value">
        <option v-for="s in scopes" :key="s.key" :value="s.key">{{s.label}}</option>
      </select></label>
      <p>留空沿用上级设置。账号时段须在所属平台的时段内。</p>
      <div class="inputs">
        <label>开始时间 <input type="text" maxlength="5" placeholder="HH:MM" :value="value.start ?? ''" @input="change('start',$event.target.value)"></label>
        <label>结束时间（不含） <input type="text" maxlength="5" placeholder="HH:MM" :value="value.end ?? ''" @input="change('end',$event.target.value)"></label>
        <label>{{current.account?'账号':'平台'}}每日上限 <input type="number" min="1" :max="current.account?1000:10000" :value="value.daily_cap ?? ''" @input="change('daily_cap',$event.target.value)"></label>
        <label>{{current.account?'同账号':'同平台'}}间隔（秒） <input type="number" min="1" max="86400" :value="value.gap ?? ''" @input="change('gap',$event.target.value)"></label>
      </div>
      <el-button v-if="present" link @click="replace(null)">清除此项规则</el-button>
    </template>
  </details>
</template>
<script setup>
import {computed,ref} from 'vue'
const props=defineProps({targets:Array,channels:Object,modelValue:Array})
const emit=defineEmits(['update:modelValue'])
const selected=ref('')
const keyOf=r=>JSON.stringify([r.platform,r.account ?? null])
const scopes=computed(()=>Object.entries(props.channels).flatMap(([platform,label])=>{
  const accounts=props.targets.filter(t=>t.platform===platform)
  return accounts.length?[{platform,label:label+'（平台）'},...accounts.map(t=>({...t,label:label+' · '+t.account}))].map(s=>({...s,key:keyOf(s)})):[]
}))
const current=computed(()=>scopes.value.find(s=>s.key===selected.value) ?? scopes.value[0])
const enabled=computed(()=>props.modelValue.filter(v=>scopes.value.some(s=>s.key===keyOf(v))))
const present=computed(()=>props.modelValue.find(v=>keyOf(v)===current.value?.key))
const value=computed(()=>present.value ?? {})
function replace(next){
  const rest=props.modelValue.filter(v=>keyOf(v)!==current.value.key)
  emit('update:modelValue',next?[...rest,next]:rest)
}
function change(field,text){
  const next={platform:current.value.platform,...value.value}
  if(current.value.account)next.account=current.value.account
  if(text==='')delete next[field]
  else next[field]=['gap','daily_cap'].includes(field)?Number(text):text.trim()
  replace(['start','end','gap','daily_cap'].some(k=>Object.hasOwn(next,k))?next:null)
}
</script>
<style scoped>
.scope{margin:10px 0}.scope summary{cursor:pointer}.inputs{display:flex;flex-wrap:wrap;gap:8px 14px;margin:8px 0}.inputs input{width:78px}
.scope p{margin:6px 0;color:var(--el-text-color-secondary)}.scope select{margin-top:8px;max-width:100%}
</style>
