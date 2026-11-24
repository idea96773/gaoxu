import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { z } from 'zod'
import { AsyncLocalStorage } from 'node:async_hooks'

const server = new McpServer({ name: 'gaoxu-distribution', version: '1.0.0' })
const activeTool = new AsyncLocalStorage()
const target = z.object({platform:z.enum(['zhihu','xiaohongshu','weixin','toutiao','weibo','baijiahao','sohu']),account:z.string().min(1).max(40)})
const admission = {account_routing:z.enum(["rotate","all"]).optional(),request_id:z.string().min(1).max(128),targets:z.array(target).min(1).max(28)}
const batchId = z.string().regex(/^[a-f0-9]{32}$/)
const jobId = z.number().int().positive()
const day = z.string().regex(/^\d{4}-\d{2}-\d{2}$/)
const scheduleState = z.enum(['ready','running','done','failed','paused','cancelled','unconfirmed']).optional().describe('模拟排期状态，paused 表示所属批次暂停的排队任务，unconfirmed 表示冻结稿待核对回执')
const selectionKey = z.string().regex(/^(url|file)-[a-f0-9]{64}$/)
const selectionTitle = z.string().min(1).max(8192)
const selectionSource = z.string().url().max(4096).describe('目录返回的规范化公众号原文链接，用于同批跨目录去重')
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
  const headers = {...options.headers, 'X-Gaoxu-MCP-Tool':activeTool.getStore()}
  const response = await fetch('http://127.0.0.1:5419/api/distribution/'+path,{...options,headers,signal:AbortSignal.timeout(60000)})
  const data = await response.json()
  if (!response.ok) throw new Error(data.error || response.statusText)
  return data
}
function tool(name,description,inputSchema,call) {
  server.registerTool(name,{description,inputSchema},async args=>{
    try {return {content:[{type:'text',text:JSON.stringify(await activeTool.run(name,call,args))}]}}
    catch(error){return {isError:true,content:[{type:'text',text:String(error.message)}]}}
  })
}
tool('library_list','查看下载目录、原文标识与各平台已有任务。可按来源公众号、原文发布日期（含首尾）、标题或目录筛选。指定 page 可分页查看，order=newest 优先显示新稿',{
  source_name:z.string().optional(),keyword:z.string().optional(),start:day.optional(),end:day.optional(),
  order:z.enum(['directory','newest']).optional(),page:jobId.optional(),page_size:z.number().int().min(1).max(100).optional()
},args=>api('library'+(Object.keys(args).length?'?'+new URLSearchParams(args):'')))
tool('reprint_settings','读取管理台保存的常用转载账号、文末链接和排期规则',{},()=>api('reprint-settings'))
tool('library_match_links','按原文链接查找已下载文章。每行返回匹配、同源多目录、未匹配、无效或重复结果。确认目录后可将匹配项交给 reprint_prepare_saved 或 reprint_prepare。',{
  urls:z.array(z.string()).min(1).max(100),expected_root:z.string().optional()
},args=>api('library/match-links',args))
tool('reprint_prepare','按选定目录创建转载批次并返回批次编号。从 library_list 复制 title、selection_key（填入 key）及已识别的 source。相同 source 使用先选目录。batch_status 查询进度，request_id 用于重试。',{
  request_id:admission.request_id,directories:z.array(z.string()).min(1).max(100),
  expected_root:z.string().optional().describe('上次 library_list 返回的 root，用于核对选稿目录'),
  selections:z.record(z.string(),z.object({key:selectionKey,title:selectionTitle,source:selectionSource.nullish()})).optional(),
  targets:z.array(z.object({platform:z.enum(['zhihu','toutiao']),account:target.shape.account})).min(1).max(28),
  destination:z.string().url(),rules:reprintRule,
  links:z.object(Object.fromEntries(['zhihu','toutiao'].map(p=>[p,z.discriminatedUnion('mode',[
    z.object({mode:z.literal('direct')}),z.object({mode:z.literal('omit')}),
    z.object({mode:z.literal('short'),short:z.string().url(),verified:z.literal(true)})
  ]).optional()]))).optional()
},a=>api('reprints',a))
tool('reprint_prepare_saved','按管理台已保存的常用设置生成本地待审转载批次。先从 reprint_settings 取得 revision，从 library_list 取得 root 并选择 items。请求超时后用相同 request_id 和相同参数重试可找回原批次。返回管理台审核入口。',{
  request_id:admission.request_id,saved_revision:jobId,
  root:z.string().min(1),items:z.array(z.object({
    directory:z.string().min(1),title:selectionTitle,selection_key:selectionKey,source:selectionSource.nullish()
  })).min(1).max(100)
},async ({request_id,saved_revision,root,items})=>{
  const selected=new Map()
  for(const item of items){
    const earlier=selected.get(item.directory)
    if(earlier && (earlier.key!==item.selection_key || earlier.title!==item.title || earlier.source!==(item.source??undefined)))
      throw new Error('同一目录含有不同版本的选稿记录，请重新读取目录并选稿')
    selected.set(item.directory,{title:item.title,key:item.selection_key,...(item.source?{source:item.source}:{})})
  }
  const result=await api('reprints',{request_id,saved_revision,expected_root:root,
    directories:items.map(item=>item.directory),selections:Object.fromEntries(selected)})
  return {...result,saved_revision,review_url:'http://127.0.0.1:5419/#/distribution?batch='+result.id}
})
tool('reprint_preview','按平台读取转载待审稿或已有任务绑定的冻结稿；job、account、reused 标明原任务和接收账号，edition_sha 可用于核对版本。存在沿用任务或专属文末链接时须指定平台',{batch:batchId,row:jobId,platform:z.enum(['zhihu','toutiao']).optional()},async a=>{
  const {sha,snapshot,revision,edition_sha,job,account,reused}=await api('reprints/'+a.batch+'/'+a.row+(a.platform?'?platform='+a.platform:''))
  if(!snapshot)throw new Error('该条目的原始资料异常，请先查看批次详情')
  return {sha,edition_sha,revision,platform:a.platform,job,account,reused,title:snapshot.title,text:snapshot.text,images:snapshot.images,link:snapshot.link}
})
tool('publish_plan','只读试算本批所选待审转载稿的分稿账号及北京时间排期。按传入顺序计算；从 batch_status 取每行 id、sha、revision。prior_job 表示沿用已有排期，时间为 Unix 秒。审核时按当时的可用名额正式排期。',{
  batch:batchId,start_at:z.number().int().positive().optional().describe('新任务最早排期，Unix 秒；沿用任务保留原排期'),items:z.array(z.object({id:jobId,sha:z.string().regex(/^[a-f0-9]{64}$/),revision:jobId,
    expected_prior:z.record(z.string(),jobId).optional()})).min(1).max(100)
},({batch,items,start_at})=>api('reprints/'+batch+'/plan',{items,start_at}))
tool('article_search','按标题、平台、账号以及北京时间日期跨批次查询模拟记录',{
  schedule_state:scheduleState,
  title:z.string().optional(),platform:target.shape.platform.optional(),account:z.string().optional(),
  draft_recipient:z.string().optional().describe('已锁定的知乎接收账号昵称或完整 ID'),
  draft_review:z.enum(['unsubmitted_reprint','waiting','saving','failed','awaiting','uncertain','issue','verified','cancelled']).optional(),
  work:z.enum(['pending_reprint','scheduling_reprint','returned_reprint','withdrawn_reprint','frozen_delivery','queued_delivery','confirmed_delivery']).optional(),
  date_field:z.enum(['scheduled','execution','delivered','imported','draft_updated']).optional(),start:day.optional(),end:day.optional(),page:jobId.optional()
},a=>api('article-search?'+new URLSearchParams(a)))
tool('scheduler_status','读取服务心跳、活动批次积压、过期租约与待核对的草稿回执',{},()=>api('runtime-status'))
tool('account_list','按平台列出已有分稿目标的账号别名',{},()=>api('article-search/accounts'))
tool('schedule_calendar','按北京时间月份查看每天的任务状态及数量，可选原排期或预计执行日',{
  schedule_state:scheduleState,
  work:z.literal('frozen_delivery').optional().describe('仅统计冻结稿交付'),
  month:z.string().regex(/^\d{4}-(0[1-9]|1[0-2])$/),date_field:z.enum(['scheduled','execution']).optional(),
  platform:target.shape.platform.optional(),account:target.shape.account.optional()
},args=>api('schedule-calendar?'+new URLSearchParams(args)))
tool('agent_runs','查看最近到达管理台的 Agent 调用记录，使用 next 向前翻页',{
  before:jobId.optional()
},args=>api('agent-runs?'+new URLSearchParams(args)))
tool('library_import','导入最多 100 个已列出的本地文章目录并开始模拟处理',{
  ...admission,directories:z.array(z.string()).min(1).max(100),
  expected_root:z.string().optional().describe('上次 library_list 返回的 root')
},a=>api('library',a))
tool('article_batch_import','以模拟正文处理手工提交的微信链接',{...admission,urls:z.array(z.string()).min(1).max(1000)},a=>api('batches',a))
tool('dashboard_summary','模拟批次、全局待审转载数、各平台冻结稿交付状态和成功率、今日冻结稿回执数、失败步骤及调度器心跳。compact 可只取汇总和批次索引',{compact:z.boolean().optional()},a=>api(a.compact?'batches?summary=1':'batches'))
tool('batch_status','查询三级任务进度及独立失败项',{id:batchId},a=>api('batches/'+a.id))
tool('batch_control','暂停、继续或取消模拟批次',{id:batchId,action:z.enum(['pause','resume','cancel'])},a=>api('batches/'+a.id+'/'+a.action,{}))
tool('article_detail','读取指定文章来源、已保存正文、版本及阶段',{id:jobId},a=>api('articles/'+a.id))
tool('publish_status','查看单个步骤、模拟回执及尝试记录',{id:jobId},a=>api('jobs/'+a.id))
tool('publish_cancel','取消指定平台账号的一项排期。先调用 publish_status，将返回的 cancel_guard 原样作为 expected。已收到回执时返回 done',{
  id:jobId,expected:z.object({state:z.enum(['ready','running','failed']),attempts:z.number().int().nonnegative(),
    slot:z.number().int(),due:z.number().int()})
},({id,expected})=>api('jobs/'+id+'/cancel',{expected}))
tool('failed_jobs','按批次、步骤或北京时间失败日期查看当前失败的后台任务',{
  batch:batchId.optional(),stage:z.string().optional(),day:day.optional(),page:jobId.optional()
},args=>api('failed-jobs?'+new URLSearchParams(args)))
tool('failed_jobs_retry','只将选中的失败尝试重新排队，可按返回结果逐项核对',{
  items:z.array(z.object({id:jobId,batch:batchId,expected_attempt:z.number().int().nonnegative()})).min(1).max(100)
},args=>api('failed-jobs/retry',args))
tool('stage_retry','只重试选中的失败步骤',{id:jobId},a=>api('jobs/'+a.id+'/retry',{}))
tool('publish_available_slot','查询此任务按账号、平台和其他预约计算的可用改期时间。time 为请求的 Unix 秒，查询不会改动排期',{
  id:jobId,time:z.number().int()
},({id,time})=>api('jobs/'+id+'/available-slot?time='+time))
tool('publish_reschedule','调整排队任务。采用查询建议时将 available 同时作为 time 和 expected_available，并传查询返回的 expected_slot、expected_execution；排期变化会报错',{
  id:jobId,time:z.number().int(),expected_slot:z.number().int().optional(),expected_execution:z.number().int().optional(),
  expected_available:z.number().int().optional()
},({id,...data})=>api('jobs/'+id+'/reschedule',data))
tool('draft_list','查看分配到此文章的账号及成稿审核状态',{id:jobId},a=>api('articles/'+a.id+'/editorial'))
tool('draft_submit','提交 Agent 已生成的平台成稿供人工审核；已存在未结稿时返回其 ID',{id:jobId,platform:z.enum(['zhihu','xiaohongshu','toutiao']),account:z.string(),title:z.string().min(1),body:z.string().min(1)},a=>api('articles/'+a.id+'/editorial',{...a,action:'agent'}))
tool('draft_schedule','将已审核冻结的平台账号版本加入本地模拟交付，time 为 Unix 秒，可省略按规则排期',{id:jobId,time:z.number().int().optional()},a=>api('editorial/'+a.id+'/schedule',a.time===undefined?{}:{time:a.time}))
await server.connect(new StdioServerTransport())
