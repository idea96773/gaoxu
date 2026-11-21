import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { z } from 'zod'

const server = new McpServer({ name: 'gaoxu-distribution', version: '1.0.0' })
const target = z.object({platform:z.enum(['zhihu','xiaohongshu','weixin','toutiao','weibo','baijiahao','sohu']),account:z.string().min(1).max(40)})
const admission = {account_routing:z.enum(["rotate","all"]).optional(),request_id:z.string().min(1).max(128),targets:z.array(target).min(1).max(28)}
const batchId = z.string().regex(/^[a-f0-9]{32}$/)
const jobId = z.number().int().positive()
async function api(path, body) {
  const options = body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}
  const response = await fetch('http://127.0.0.1:5419/api/distribution/'+path,{...options,signal:AbortSignal.timeout(60000)})
  const data = await response.json()
  if (!response.ok) throw new Error(data.error || response.statusText)
  return data
}
function tool(name,description,inputSchema,call) {
  server.registerTool(name,{description,inputSchema},async args=>{
    try {return {content:[{type:'text',text:JSON.stringify(await call(args))}]}}
    catch(error){return {isError:true,content:[{type:'text',text:String(error.message)}]}}
  })
}
tool('library_list','只读查看配置的本地保存目录及已入库标记',{},()=>api('library'))
tool('library_import','导入最多 100 个已列出的本地文章目录并开始模拟处理',{...admission,directories:z.array(z.string()).min(1).max(100)},a=>api('library',a))
tool('article_batch_import','以模拟正文处理手工提交的微信链接',{...admission,urls:z.array(z.string()).min(1).max(1000)},a=>api('batches',a))
tool('dashboard_summary','模拟批次及调度器心跳',{},()=>api('batches'))
tool('batch_status','查询三级任务进度及独立失败项',{id:batchId},a=>api('batches/'+a.id))
tool('batch_control','暂停、继续或取消模拟批次',{id:batchId,action:z.enum(['pause','resume','cancel'])},a=>api('batches/'+a.id+'/'+a.action,{}))
tool('article_detail','读取指定文章来源、已保存正文、版本及阶段',{id:jobId},a=>api('articles/'+a.id))
tool('publish_status','查看单个步骤、模拟回执及尝试记录',{id:jobId},a=>api('jobs/'+a.id))
tool('stage_retry','只重试选中的失败步骤',{id:jobId},a=>api('jobs/'+a.id+'/retry',{}))
tool('publish_reschedule','重新安排未领取的模拟发布任务',{id:jobId,time:z.number().int()},a=>api('jobs/'+a.id+'/reschedule',{time:a.time}))
tool('draft_list','查看分配到此文章的账号及成稿审核状态',{id:jobId},a=>api('articles/'+a.id+'/editorial'))
tool('draft_submit','提交 Agent 已生成的平台成稿供人工审核；已存在未结稿时返回其 ID',{id:jobId,platform:z.enum(['zhihu','xiaohongshu','toutiao']),account:z.string(),title:z.string().min(1),body:z.string().min(1)},a=>api('articles/'+a.id+'/editorial',{...a,action:'agent'}))
tool('draft_schedule','将已审核冻结的平台账号版本加入本地模拟交付，time 为 Unix 秒，可省略按规则排期',{id:jobId,time:z.number().int().optional()},a=>api('editorial/'+a.id+'/schedule',a.time===undefined?{}:{time:a.time}))
await server.connect(new StdioServerTransport())
