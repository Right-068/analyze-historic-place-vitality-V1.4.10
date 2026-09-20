# 顺序批量执行合同

本层只调度与缓存，不创建第二套正式证据或评分规则。来源准入、原文定位、A/B/C 等级、去重、语义量化、复核、审计、冻结和 Office 全部调用原发布链。宿主不得据短视图判断已满足门槛。

## 生命周期

`init → execute-next-action → DISCOVERY → PAGE_CAPTURE → NORMALIZE → TRIAGE → 用户候选抽取/编码 → 原正式准入/量化 → 可选可信复核或未确认排除计分 → 刷新 → EVIDENCE_AUDIT → GAP_ANALYSIS 或 SCORE → REPORT_BUILD → VALIDATE → DELIVER`。后续均使用同一动作入口，模块名是返回的操作说明，不是要求模型选择的 CLI 命令。

完整初始计划一次生成、登记、冻结。DISCOVERY 分窗口连续执行整轮查询；之后才进入读页。每个模块连续处理其所有批次，不能逐页返回搜索。补检只接受原正式审计的缺口和原规划器新登记的补充查询，不能按分数方向补检，不能重写初始计划。暂存 module state 不是正式门禁；`needs_iteration`、正式审计和冻结仍决定能否评分。

`init` 已立即启用动作模式并返回首份动作合同；从此旧单页公共接口拒绝，不能在首次提交前走兼容路径。恢复用 `execute-next-action`，`next` 是其别名。返回自包含 `input_contract`，字段与枚举从正式消费者加载，不需要查源码。所有判断或工具数组提交为 `{"action_id":"<返回值>","records":[...]}`，用 `execute-next-action --input <文件>`；无输入时继续机械步骤。阶段切换不复制历史、不换任务编号，不依赖特定模型或额外 API。

## 独立负载参数

只读 `assets/batch-execution.json`：查询窗口 6，首批候选 3，优先读页 2，读页批次最多 12，筛选最多 12（硬上限 16），编码最多 10（硬上限 12）；动作合同及短视图共用字符预算。提交必须覆盖本次实际分派的全部选择器，不能私自拆成逐页回合；尾批、字符预算和失败重试可以是小批。全部仅分片，不减少完整计划、来源多样性、页面与逐维目标。

## DISCOVERY

动作入口返回当前 `assigned_queries` 并持久化开始点；先执行这些真实宿主查询，再将整批结果置于动作信封的 `records`，统一提交。每条包含：

```json
{"execution_id":"<返回的尝试编号>","response_text":"<真实搜索响应>","result_count":1,"urls":["<真实结果URL>"]}
```

可选字段与既有 `record-search` 一致：`tool_name/status/error_type/login_triggered/restriction_triggered`。无结果为真实零值，不伪造成功。原始卡片可完整留盘，但宿主检索工具可控制输出时首批只请求 top 3；长响应在本地解析，不再次全量注入模型。发现阶段不做来源审计、证据抽取、七维编码或长篇说明。

`result_count` 记录真实返回条目数，不是去重 URL 数或展示限额。纯文本响应应提供逐条完整 `urls`（允许重复），或可选 `search_results` 数组：每项含 `url`，无链接项含实际 `unavailable_reason`。不能丢弃条目凑数。

`response_text` 若是含 `results` 数组的 JSON，Python 直接从每项 `url/link` 或 `unavailable_reason` 恢复完整池；数量不符或夹带其他 URL 拒绝。暂存记录保留 result_count、unique_result_url_count、persisted_result_url_count、non_url_result_count 和完整条目。缺项须从已保存响应修复，不重新搜索。此检查证明提交的一致性，不证明非结构化宿主从未遗漏结果。候选短视图只暴露首批，完整池保留。

## PAGE_CAPTURE

按返回 `items` 打开真实页面；阶段只保存原文和工具事实，不做来源或情感判断。整批通过动作信封一次登记，每项：

```json
{"execution_id":"<编号>","request_url":"<真实请求URL>","final_url":"<真实最终URL>","page_title":"<标题>","page_body":"<实际工具正文>"}
```

可选 `retrieved_at/body_format/tool_name/tool_call_id/result_ref/access_status`；完整与部分正文均沿用现有 `full/partial` 合同。时间有真实值就保留，缺失时仅用当前接收时间，不能替旧材料补造访问。不得填七维、情感、来源资格或分数。无法访问则只提交 `execution_id/request_url/error_type`，如实记录原因并停止受限路径。

每项成功立即按存储等级持久化；后续项损坏不丢失前项，修复只重交失败项。无需每页回到总编排器。相同 URL 已保存不重复联网；内容变化须明确新观察，不静默覆盖。

页面覆盖不足时，TRIAGE 后按剩余缺口和已观测有效页面产出率估算下一波；全局最多两个读页批次（当前 24 条），不是每个查询各自扩量。按查询轮转，游标持久化；去重、已读、失败和受限路径不重复派发。每波读完即规范化、筛选，再算缺口；未用候选完整保留，已完成判断不重做。此上界仅控制调度负载，不替代正式去重页面数，不改变研究门槛。登录、验证码、限流禁止借展开规避。剩余路径用完后，由真实审计授权后续查询；已分派页面须有实际结果才能结束模块。

## NORMALIZE 与短视图

调度器自动规范化已存 RAW，生成 clean_body 与有偏移短视图，清除结构明确的导航、页脚、侧栏、隐藏模板及脚本。按站点缓存结构规则，不学习删除重复研究段落。RAW 不改，正式摘录仍需通过原可见正文和定位器复核。

完全相同正文仅提示或在同协议、同内容层且已有真实判断时复用抽取/编码结果；近重复只给相似性提示，不能据此增减正式页面、合并样本或忽略矛盾。相关性和来源类型仍分别判断。短视图不足时，以 `{"action_id":"<当前值>","page_view":{"page_id":"<分派值>","offset":0}}` 请求更多本地文本，不重新联网，不因首段无信息宣布整页无关。

## TRIAGE / EVIDENCE_EXTRACTION / SEMANTIC_CODING

动作信封的判断数组每项恰含 `page_id/analysis_fingerprint/decision`；编号和指纹使用当前短视图值。已完成判断不可静默覆盖；不从旧任务导入缓存。下列名称只表示返回的 `operation`：

- `record-triage-batch`：decision 恰含布尔 `relevant`、`source_category/content_layer/entity_level/promotion_status`。枚举直接包含在动作合同，promotion 为 `unknown/suspected/not_suspected`。非用户事实进入 `context_only`，保留来源与正文供报告使用，不抽取感知样本。真实用户表达进入 `scoring_candidate`；官方页面的真实评论也按其内容判断，不能只看域名。相关性不等于计分资格。
- `record-extraction-batch`：decision 是 `[{"text":"<唯一可定位的原文片段>"}]`，不填写维度、情感分、强度、ID 或资格。没有可用片段可为空数组，如实保留来源判断。
- `record-coding-batch`：decision 按抽取单元原顺序为 `[{"primary_dimension":"<唯一正式维度名称>"}]`。这是待准入主维度判断，不是正式评分；情感、强度、置信度、评分由原语义引擎和必要可信复核处理。编码视图截断时用 page-view 查看余下本地片段，不能猜测未读单元。

全部调用带同一 `--run-dir`，必须提交完整分派批次。整批中的局部失败不丢失成功项，下一动作只要求剩余项。RAW、COMPACT、分析指纹和判断缓存位于本次 staging；宿主不直接写正式 source/evidence。批量模式旧单页公共 API 与 CLI 均拒绝；内部兼容调用不作为宿主入口。

## 提交、缺口与报告

调度器连续调用正式写入器完成本轮查询与来源采集，逐查询保存恢复游标；背景来源允许没有评分证据单元，仍执行全部来源检查。用户来源必须有真实候选单元才能晋升，不补造空评价。原语义引擎处理用户单元，无真实人工决定时将未确认记录在正式派生层标为不计分，保留原始候选和 review_required，不伪造人工确认，不等待模型自建复核。随后刷新并运行原审计，只有授权计划才能开下一轮。评分公式与门槛不变。

报告只读取冻结真值、结构化七维摘要、代表性证据、矛盾和限制，不重新读取所有网页或联网重开页面。机械步骤由程序完成，复杂消歧和最终解释才使用充分推理。

## 输出、恢复与成本

CLI 默认 compact agent view，完整结果存 `staging/pipeline/operational/machine-state.json`。调试用 `--debug`，不能把完整历史 stdout 反复喂回模型。模块摘要和状态在同目录保存；页面级持久化不等于模型级切换。只在模块边界、异常升级和任务完成时回到总调度。

`cost-metrics` 从本地事件生成 `staging/cost/operational/cost-metrics.json`。CLI次数、输出字节、已登记搜索/读页数和存储字节为可观测代理；宿主若提供真实模型回合、tokens、credits，可用 `record-cost --input <文件>` 提交 `event_id/values`。不提供则为 null / missing evidence，不能用程序调用数冒充实际模型回合或积分。

模块完成记录摘要、真实计数、下一动作及检查点；外部中断不伪装为完成。相同 action_id 的已完成输入重放不重查，内容改变拒绝；失败项修正重试不重做成功兄弟项。开发验证和成本观测在 Runtime 外隔离执行，不把历史实机材料、结果或固定研究地点写回 Skill。局部脚本计数不证明真实积分节约，也不证明宿主不会取消。成本预算只切批，不能截断正式研究。
# 输入规范化补充

`prepare_host_input.py` 从已分派动作构造输入信封，见 `host-input-adapters.md`。搜索完整响应由程序提取数量及 URL；页面身份来自固定适配器。TRIAGE 的用户内容须有 classification_basis，摘录限于 user_content_excerpt；机构编辑内容与未知来源保持 context_only。不得根据 query source target 把背景改成用户评价。
