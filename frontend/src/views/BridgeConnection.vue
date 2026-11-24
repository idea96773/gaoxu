<template>
  <div v-if="bridge" class="connection">
    <p>草稿桥　<strong>{{message}}</strong>
      <span v-if="bridge.online"> · {{bridge.check_only?'仅核对连接':'草稿交付模式'}}</span></p>
    <small v-if="typeof bridge.component_ready==='boolean'">本机组件　{{bridge.component_ready?'已就绪':'待安装或修复，可双击项目目录的「启动知乎草稿助手.bat」按提示操作'}}</small>
    <small v-if="bridge.port">扩展连接地址　ws://127.0.0.1:{{bridge.port}}</small>
    <small v-if="account">当前知乎账号　{{account.name}}（{{account.user_id}}）</small>
    <small v-if="bridge.online && !bridge.check_only">交付范围　{{bridge.job_id ? '仅任务 #'+bridge.job_id : '当前登录账号已确认的队列'}}</small>
    <p v-if="bridge.phase==='offline'">首次连接时，双击项目目录的「启动知乎草稿助手.bat」，按回车选择「仅核对扩展连接和知乎账号」。</p>
    <p v-else-if="['listening','disconnected'].includes(bridge.phase)">在测试账号专用浏览器中打开文章同步助手，核对扩展连接地址并连接。</p>
    <p v-else-if="bridge.phase==='unconfirmed'">最近一次连接未完成身份核对。请检查指定扩展的 ID、连接地址和两端的连接令牌。</p>
    <p v-else-if="bridge.phase==='connected' && !account">请在该浏览器配置中登录指定知乎测试账号。</p>
    <p v-else-if="account && target && account.user_id!==target.user_id">本稿指定「{{target.name}}」（{{target.user_id}}），请切换到该账号并重新核对。</p>
    <p v-else-if="bridge.check_only && account">账号已识别。准备保存草稿时，关闭核对模式，再以草稿交付模式启动。</p>
    <div v-if="jobId && (bridge.phase==='offline' || bridge.check_only || bridge.job_id!==jobId)">
      <p>仅处理本稿时，{{bridge.online?'先按 Ctrl+C 关闭当前草稿桥，再':''}}双击「启动知乎草稿助手.bat」，选择 2，输入交付任务编号 <strong>{{jobId}}</strong>。</p>
      <details><summary>PowerShell 命令</summary>
        <p>只查看本稿　<code>.\scripts\start-zhihu-drafts.ps1 -InspectJob {{jobId}}</code></p>
        <p>启动单篇助手　<code>.\scripts\start-zhihu-drafts.ps1 -JobId {{jobId}}</code></p>
      </details>
    </div>
  </div>
</template>
<script setup>
import {computed} from 'vue'
const props=defineProps({bridge:Object,account:Object,target:Object,jobId:Number})
const messages={offline:'未检测到运行心跳',listening:'已启动，等待扩展',checking:'正在核对扩展身份',
  working:'正在处理草稿任务',disconnected:'扩展已断开，等待重连',unconfirmed:'扩展身份未确认'}
const message=computed(()=>props.bridge.phase==='connected'?(props.account?'扩展已连接，账号已识别':'扩展已连接，等待知乎登录'):
  messages[props.bridge.phase])
</script>
<style scoped>
.connection small{display:block;overflow-wrap:anywhere;color:var(--el-text-color-secondary)}.connection p{margin:.55em 0}.connection code{overflow-wrap:anywhere}
</style>
