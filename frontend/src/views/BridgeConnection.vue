<template>
  <div v-if="bridge" class="connection">
    <p>草稿桥　<strong>{{message}}</strong>
      <span v-if="bridge.online"> · {{bridge.check_only?'仅核对连接':'草稿交付模式'}}</span></p>
    <small v-if="bridge.port">扩展连接地址　ws://127.0.0.1:{{bridge.port}}</small>
    <small v-if="account">当前知乎账号　{{account.name}}（{{account.user_id}}）</small>
    <p v-if="bridge.phase==='offline'">首次核对可在稿序项目目录运行 <code>.\scripts\start-zhihu-drafts.ps1 -CheckOnly</code>，按提示填写扩展 ID。</p>
    <p v-else-if="['listening','disconnected'].includes(bridge.phase)">在测试账号专用浏览器中打开文章同步助手，核对扩展连接地址并连接。</p>
    <p v-else-if="bridge.phase==='unconfirmed'">最近一次连接未完成身份核对。请检查指定扩展的 ID、连接地址和两端的连接令牌。</p>
    <p v-else-if="bridge.phase==='connected' && !account">请在该浏览器配置中登录指定知乎测试账号。</p>
    <p v-else-if="account && target && account.user_id!==target.user_id">本稿指定「{{target.name}}」（{{target.user_id}}），请切换到该账号并重新核对。</p>
    <p v-else-if="bridge.check_only && account">账号已识别。准备保存草稿时，关闭核对模式，再以草稿交付模式启动。</p>
  </div>
</template>
<script setup>
import {computed} from 'vue'
const props=defineProps({bridge:Object,account:Object,target:Object})
const messages={offline:'未检测到运行心跳',listening:'已启动，等待扩展',checking:'正在核对扩展身份',
  working:'正在处理草稿任务',disconnected:'扩展已断开，等待重连',unconfirmed:'扩展身份未确认'}
const message=computed(()=>props.bridge.phase==='connected'?(props.account?'扩展已连接，账号已识别':'扩展已连接，等待知乎登录'):
  messages[props.bridge.phase])
</script>
<style scoped>
.connection small{display:block;overflow-wrap:anywhere;color:var(--el-text-color-secondary)}.connection p{margin:.55em 0}.connection code{overflow-wrap:anywhere}
</style>
