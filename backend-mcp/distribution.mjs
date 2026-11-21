import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { z } from 'zod'

const server = new McpServer({ name: 'gaoxu-distribution', version: '1.0.0' })
const target = z.object({platform:z.enum(['zhihu','xiaohongshu','weixin','toutiao','weibo','baijiahao','sohu']),account:z.string().min(1).max(40)})
const admission = {account_routing:z.enum(["rotate","all"]).optional(),request_id:z.string().min(1).max(128),targets:z.array(target).min(1).max(28)}
const batchId = z.string().regex(/^[a-f0-9]{32}$/)
const jobId = z.number().int().positive()
const day = z.string().regex(/^\d{4}-\d{2}-\d{2}$/)
const reprintRule = z.object({
  gap:z.number().int().min(1).max(86400), account_cap:z.number().int().min(1).max(1000),
  platform_cap:z.number().int().min(1).max(10000), start_hour:z.number().int().min(0).max(23),
  end_hour:z.number().int().min(1).max(24), weekdays_only:z.boolean().optional(),
  holidays:z.array(day).max(366).optional(), workdays:z.array(day).max(366).optional(),
  overrides:z.array(z.object({
    platform:z.enum(['zhihu','toutiao']),account:target.shape.account.optional(),
    start:z.string().optional(),end:z.string().optional(),daily_cap:z.number().int().positive().optional(),
    gap:z.number().int().min(1).max(86400).optional()
  })).max(35).optional()
})
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
tool('reprint_settings','读取管理台保存的常用转载账号、文末链接和排期规则',{},()=>api('reprint-settings'))
tool('reprint_prepare','按选定目录创建转载批次并返回批次编号。后台逐篇读取资料，使用 batch_status 查询进度，request_id 用于去重',{
  request_id:admission.request_id,directories:z.array(z.string()).min(1).max(100),
  targets:z.array(z.object({platform:z.enum(['zhihu','toutiao']),account:target.shape.account})).min(1).max(28),
  destination:z.string().url(),rules:reprintRule,
  links:z.object(Object.fromEntries(['zhihu','toutiao'].map(p=>[p,z.discriminatedUnion('mode',[
    z.object({mode:z.literal('direct')}),z.object({mode:z.literal('omit')}),
    z.object({mode:z.literal('short'),short:z.string().url(),verified:z.literal(true)})
  ]).optional()]))).optional()
},a=>api('reprints',a))
tool('reprint_preview','读取转载预览。使用平台专属文末链接时请指定平台，edition_sha 可用于核对该平台冻结稿',{batch:batchId,row:jobId,platform:z.enum(['zhihu','toutiao']).optional()},async a=>{
  const {sha,snapshot,revision,edition_sha}=await api('reprints/'+a.batch+'/'+a.row+(a.platform?'?platform='+a.platform:''))
  if(!snapshot)throw new Error('该条目的原始资料异常，请先查看批次详情')
  return {sha,edition_sha,revision,platform:a.platform,title:snapshot.title,text:snapshot.text,images:snapshot.images,link:snapshot.link}
})
tool('article_search','按标题、平台、账号以及北京时间日期跨批次查询模拟记录',{
  title:z.string().optional(),platform:target.shape.platform.optional(),account:z.string().optional(),
  date_field:z.enum(['scheduled','delivered','imported']).optional(),start:day.optional(),end:day.optional(),page:jobId.optional()
},a=>api('article-search?'+new URLSearchParams(a)))
tool('scheduler_status','读取服务心跳、活动批次积压、过期租约与待核对的草稿回执',{},()=>api('runtime-status'))
tool('library_import','导入最多 100 个已列出的本地文章目录并开始模拟处理',{...admission,directories:z.array(z.string()).min(1).max(100)},a=>api('library',a))
tool('article_batch_import','以模拟正文处理手工提交的微信链接',{...admission,urls:z.array(z.string()).min(1).max(1000)},a=>api('batches',a))
tool('dashboard_summary','模拟批次及调度器心跳',{},()=>api('batches'))
tool('batch_status','查询三级任务进度及独立失败项',{id:batchId},a=>api('batches/'+a.id))
tool('batch_control','暂停、继续或取消模拟批次',{id:batchId,action:z.enum(['pause','resume','cancel'])},a=>api('batches/'+a.id+'/'+a.action,{}))
tool('article_detail','读取指定文章来源、已保存正文、版本及阶段',{id:jobId},a=>api('articles/'+a.id))
tool('publish_status','查看单个步骤、模拟回执及尝试记录',{id:jobId},a=>api('jobs/'+a.id))
tool('failed_jobs','按批次、步骤或北京时间失败日期查看当前失败的后台任务',{
  batch:batchId.optional(),stage:z.string().optional(),day:day.optional(),page:jobId.optional()
},args=>api('failed-jobs?'+new URLSearchParams(args)))
tool('failed_jobs_retry','只将选中的失败尝试重新排队，可按返回结果逐项核对',{
  items:z.array(z.object({id:jobId,batch:batchId,expected_attempt:z.number().int().nonnegative()})).min(1).max(100)
},args=>api('failed-jobs/retry',args))
tool('stage_retry','只重试选中的失败步骤',{id:jobId},a=>api('jobs/'+a.id+'/retry',{}))
tool('publish_reschedule','重新安排未领取的模拟任务；冻结稿交付须传上次读取的 slot 作为 expected_slot',{
  id:jobId,time:z.number().int(),expected_slot:z.number().int().optional()
},({id,...data})=>api('jobs/'+id+'/reschedule',data))
tool('draft_list','查看分配到此文章的账号及成稿审核状态',{id:jobId},a=>api('articles/'+a.id+'/editorial'))
tool('draft_submit','提交 Agent 已生成的平台成稿供人工审核；已存在未结稿时返回其 ID',{id:jobId,platform:z.enum(['zhihu','xiaohongshu','toutiao']),account:z.string(),title:z.string().min(1),body:z.string().min(1)},a=>api('articles/'+a.id+'/editorial',{...a,action:'agent'}))
tool('draft_schedule','将已审核冻结的平台账号版本加入本地模拟交付，time 为 Unix 秒，可省略按规则排期',{id:jobId,time:z.number().int().optional()},a=>api('editorial/'+a.id+'/schedule',a.time===undefined?{}:{time:a.time}))
await server.connect(new StdioServerTransport())
