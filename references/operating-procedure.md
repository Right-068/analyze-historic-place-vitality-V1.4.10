# 低层操作与方法手册

本手册保留发布流程的低层命令与方法细则。默认入口为 `run_research.py`；不要在启动时完整装载本手册，也不要据此重新手排整链。仅在进入相关阶段或受控专项操作时读取对应章节。命令路径相对于 Skill 根目录。

## 执行工作流

唯一正式阶段顺序为：`INITIALIZE → SEARCH → EVIDENCE_BUILD → SEMANTIC_QUANTIFICATION → EVIDENCE_AUDIT → SCORE → REPORT_BUILD → VALIDATE → DELIVER`。以下小节用于解释各环节职责，不构成另一套执行顺序。原始证据尚未完成语义量化、必要人工复核和正式资格派生时，不得运行正式证据审计；审计返回 `needs_iteration` 时，状态机必须从 `EVIDENCE_AUDIT` 回到 `SEARCH`，重新完成证据构建、量化、复核与审计，不能越过任何阶段。

正式数据链固定为：公开页面实际读取 → 不可变来源采集 → 可修改候选暂存 → 准入前审计 → 批次原子提升 → 追加式正式来源与正式原始证据 → 可再生成的语义及评分派生层 → 单条与整批审计 → 整体真值冻结 → 机器报告事实 → 定性叙事 → Office 成果 → 只读联合验收。事实不得为了适应解释、门槛或报告而修改；正式记录纠错只能追加撤销或替代事件。

其中“实际读取”通过本次真实页面工具引用与原始结果，绑定当前任务、计划、执行、采集编号、请求和最终 URL、访问时间与内容哈希；`tool_traceable` 是普通正式路径。可信宿主收据属于可选增强 `host_verified`，缺少签章或 IP 不阻止普通路径。采集器确定性提取正文并等长脱敏，正文块使用真实布尔值、严格整数和互不重叠叶子块；晋升、核心审计和最终验收独立复验该来源链。原始结果只存权限受限的私有工件，不进入交付物；联系方式不进入标题、摘要、报告、工作簿或运行日志。

优先使用页面工具实际返回的可见纯文本。只有 HTML 源码时，由采集器派生 `body_provenance` 和 `visibility_proof_level`；依赖样式表、选择器或动态渲染的内容保留为未确认候选，使用真实渲染工具重读，不能作为用户评论或取得评分资格。正文格式、可见性、正文块和原始结果绑定贯穿准入及最终验收，具体合同见 `host-page-receipts.md`；普通可见文本不增加 CSS、签章或 IP 前置要求。

### 正式执行权限、真值冻结与有限验收

所有正式写入均受 `运行状态.json`、发布版完整性清单和 `protected_artifact_write_ledger.jsonl`（受保护工件写入台账）共同授权。正式生成器即使被直接调用，也必须核对同一 `task_run_id`、当前阶段、允许的脚本身份、输出角色、上游哈希和发布清单哈希；未登记脚本产生的台账、评分 JSON、DOCX 或 XLSX 只能是工作文件，最终验收固定返回 `untrusted_artifact_provenance`，不得作为正式成果。

机器审计通过后，使用 `artifact_provenance.py freeze` 冻结来源、原始证据、语义证据、必要的可信人工复核工件、正式证据、检索日志、维度审计、评价协议和语义代码簿；评分完成后再用 `extend-scores` 追加平台评分输入与综合评分输出。进入 `REPORT_BUILD` 后，全部下游必须从这一冻结评分真值整体生成；进入 `VALIDATE` 后，验证器严格只读，冻结上游任何哈希变化立即形成 `post_validate_upstream_mutation` 非完成终态，禁止修改后再次验收。

正式人工确认只接受固定复核合同及受信任外部签章。执行 Agent、语言模型或普通脚本自行填写的 `human_confirmed` 不具备信任资格；宿主不能提供可信人工确认时，记录保持 `review_required`。人工复核只能确认主维度、情感、强度与裁决信息，后续置信度、可靠性、聚合权重和正式资格仍由发布版算法确定。证据平台唯一经 `source_id` 追溯到验证过的来源 URL、最终跳转和固定平台映射，显示缓存不得覆盖机器身份；页面数量使用规范化 URL 哈希，正式证据去重使用规范化语义文本、唯一主维度和来源谱系的机器哈希，可编辑缓存字段不能改变正式计数。

审核人、密钥及语义/碰撞权限只读发布前 `assets/review-trust.json`，两类复核共用 `review_trust.py`。任意本地密钥不能取得身份，运行中不得登记或替换；`review_trust_proof` 使下游无需秘密密钥即可复验决定身份及内容。空配置只影响需要人工处理的记录，不阻止检索和自动合格证据；旧人工状态缺少有效身份证明时保留历史、在派生层回到未验证/待复核，不补造确认。

进入报告生成前必须运行全链 `preflight_validate.py`。联合验收一次尽可能返回完整结构化错误清单；失败后只允许一次按最上游根因合并的集中修复，并从冻结正式评分真值整体重建全部下游。正式 validation 最多两轮、自动修复最多一轮、验收后整链重建最多一次；第二轮仍失败、相同稳定错误复现、错误数不下降或新增上游错误时，状态固定为 `validation_nonconvergent` 并停止，不得运行第三轮或无界修复循环。最终交付封印和 `assert-final` 必须同时验证状态、零错误验收、真值冻结、日志、四份成果及全部正式哈希。

任何修复都不得改写原始摘要、原始引文、主维度、情感或置信度来迎合语义门槛、证据门槛、评分或报告。算法不能可靠判断时，只能进入可信复核、继续检索或保持数据不足；禁止通过修改研究真值来修复审计结果。

### 1. 定义对象和证据口径

使用三层对象模型：

- `poi`：商户、景点、建筑或设施单体；
- `area_direct`：具有独立评价页或内容明确指向街区、步行街、片区整体的直接证据；
- `area_aggregate`：以区内多个 POI 聚合构造的周边活力背景。

地点直接评分只使用 `area_direct` 或明确指向地点整体的证据。`area_aggregate` 只作背景，不得冒充街区直接评价，也不得与直接证据重复计数。

### 2. 生成覆盖多来源的检索计划

先通过正式 checkpoint 进入 `SEARCH` 再生成计划。观察字段白名单、机器路线身份、时间谱系、公开连接事实和跨类事务恢复统一遵循 `references/execution-facts.md`，不得由宿主另造宽松合同。待恢复事务未完成时不得生成新计划；先调用正式恢复入口。

有 Python 时运行：

```text
python scripts/build_query_plan.py --state "运行状态.json" --task-run-id "本次运行编号" --place "<分析对象>" --alias "<对象别名>" --building "<代表节点>" --local-term "<文化检索词>" --output "检索计划.csv"
```

基础计划同时覆盖中性、正面、负面、七维主题、时间变化和十六类来源目标。大众点评和小红书默认剔除：两者在未登录状态下通常触发访问限制，且研究另有独立获取路径；本 Skill 的结果用于后续与两类独立获取结果综合，因此默认排除还可避免重复样本、重复计数和证据层混淆。只有用户明确要求且当前路径合法、公开、可正常访问时才纳入，仍不得绕过登录、验证码或反爬限制。

检索时尽可能覆盖所有适用于该地点的公开来源类型，包括官方、政府开放数据、遗产名录、场馆、新闻、专业、学术、百科、其他用户评价、地图评价、旅游 UGC、其他社交内容、博客游记、地方论坛、社区内容和商业目录。缺失某一类型时记录实际尝试、无结果或访问限制，不用弱来源凑数。

### 3. 执行不少于 150 个页面并按逐维置信目标取得实际计分证据

来源必须保留本次真实页面工具结果及引用、请求与最终 URL、标题、访问时间和响应快照。普通 `tool_traceable` 来源不要求解析或连接 IP；宿主签章只作可选增强。来源及证据的平台显示值须与验证过的 URL 和固定映射派生身份一致；冲突是硬错误，不得通过改名增加平台覆盖。

先读取 `references/execution-facts.md`。计划提交后，依据真实工具调用填写 `assets/execution-observation-template.jsonl`，由 `execution_facts.py --state ... --writer-ledger ... --observations ... --output ...` 形成正式执行日志。查询定义由已提交计划恢复，事件序号与绑定哈希由程序生成；不可直接拼写正式查询身份或把计划当成已执行。页面采集必须复制该次实际执行的完整身份绑定，并在其真实时间窗口内完成读取。

硬性门槛是至少 150 个去重后的相关网页记录，而不是 150 条搜索结果摘要、150 次查询或同一页面内的 150 条评论。每个页面均须有真实 URL、页面标题、平台或网站、域名、来源类型、实际检索词、检索记录编号、检索日期、读取状态、选择机制、去重组和相关性判断。

页面计数与证据单元计数严格分开：

- 页面数：由发布版 URL 规范化、最终跳转 URL、内容指纹和可信独立出现复核共同形成的去重页面实体数；`source_id` 数量、同一页面的参数变体和不同 URL 下的同内容碰撞均不能直接增加页面数；
- 可读页面数：`full` 或 `partial`；
- 受限页面数：`snippet_only`、`metadata_only` 或 `inaccessible`；
- 检索有效证据数：已经进入证据台账、有效、直接且按主维度去重的广义研究单元；
- 评分候选证据数：通过单条正式评分资格检查并按 `primary_dimension` 去重的单元；
- 实际计分证据数：候选证据所在“平台×维度”组合达到发布协议最低样本量后，真正进入该平台维度 `tendency`（倾向值）计算的单元。

至少 150 个页面可包含不同读取状态，但搜索摘要和不可访问页面只作发现线索或有限元数据，不得伪装成已读正文，也不得进入用户情感评分。

实际计分证据目标由锁定协议和所选置信等级派生；默认中高为每维 40 个、七维理论最低 280 个，100 仅作历史兼容警戒下限。页面数、检索有效证据数和评分候选证据数都不能代替实际计分证据数；官方事实、宣传内容、搜索摘要、仅有总体评分而无可核验评价单元、间接 POI 聚合和重复内容均不能用来凑足样本。

### 4. 采集、候选暂存与正式准入

每个任务目录按逻辑分为 `capture/`、`staging/`、`canonical/`、`derived/`、`audit/` 与 `artifacts/`；可直接读取 `assets/run-directory-layout.json` 建立目录。三类研究记录保持关联但职责不同：检索日志记录真实查询；来源采集层保存工具当时真正可见的页面内容；候选层保存可拆分、合并、删除和重编码的研究判断。候选层不计入正式页面、证据或评分数量。

每次打开页面后立即使用 `source_capture.py` 保存不可变内容快照和追加式采集清单。普通工具结果文件仅含实际工具返回的 `response_text` 字符串；入口自动登记并绑定工具引用、查询执行及内容哈希，不接受任意本地文件充当已登记响应。首次省略采集输入的 `raw_response_file` 和 `tool_artifact`，程序自动生成；`visible_body` 可省略，提交时须与派生正文严格一致，包括空字符串。原生数值及可选签章仍按各自合同执行。再次访问同一 URL 只能新增版本，不能覆盖旧快照：

```text
python scripts/source_capture.py capture --state "运行状态.json" --writer-ledger "protected_artifact_write_ledger.jsonl" --manifest "capture/source-capture-manifest.jsonl" --snapshots-dir "capture/source_snapshots" --record "capture/本次采集记录.json" --tool-result "capture/本次页面工具结果.json"
```

采集记录必须包含同一 `task_run_id`、真实 `query_id` 与实际检索词、实际打开的 HTTP(S) URL、标题、读取状态、来源类型、内容层级、可见正文或受支持的原生数值观察。脚本分别维护宿主收据的严格传输身份和页面去重身份：前者保留路径参数、查询顺序、重复键与编码并逐跳精确绑定，后者才可按发布规则删除明确跟踪参数；二者不得互相替代。脚本保守规范化页面 URL、机器派生域名和稳定 `platform_id`，仅在实际跳转、可信 canonical 信息或受控平台规则支持时合并页面，并保存归并或不归并依据；计算正文快照 SHA-256、建立正文块及字符范围，并形成事件哈希链。平台身份统一读取 `assets/platform-aliases.json`，无法确认的主机保守保持独立。`snippet_only`、不可访问或受限状态不得伪装为已读取正文；仅有结构化原生评分时使用 `source_native_numeric` 专用路径，并在无正文情况下保存原始数值、量表上下界、评价对象、平台、页面实体、采集时间、可复验来源与内容寻址快照。缺少任一必要字段的原生数值观察不得进入正式评分，也不得补造摘要原文。

一次正式采集提交必须把原始响应的同目录临时写入、刷新与 `fsync`、大小及 SHA-256 复验、原子改名，以及 `capture_event_id` 与 `transaction_id` 分配、采集事件追加、提交后确定性清单哈希、受保护写入台账登记和提交标记置于同一可恢复事务。事务日志固定目标、临时文件、预期大小、预期哈希、写入阶段与所有权；进程中断、尾行损坏或放弃互斥量出现时，下一次提交只清理本事务拥有的临时文件，保留哈希一致的共享内容寻址成品，对未知冲突失败封闭。只有原始响应、清单与写入台账均完成才返回成功；正式验证按具体采集事件和原始响应绑定核验，不能把台账最后一行当作无条件真值。

从 `assets/candidate-source-template.jsonl` 与 `assets/candidate-evidence-template.jsonl` 建立候选来源和候选证据。候选文本的 `original_visible_text` 必须逐字存在于对应不可变快照；研究者摘要只能写入 `researcher_summary`，不能冒充原文。一个页面中的多维内容应拆成多个独立候选分析单元，每个单元最终只有一个 `primary_dimension`；`dimension_tags` 仍只用于主题覆盖。

整批候选完成后运行唯一正式准入入口。任何普通脚本、执行 Agent、语义脚本、Office 生成器或验证器均不得新增或覆盖 canonical（正式不可变）记录：

```text
python scripts/pre_admission_audit.py --state "运行状态.json" --writer-ledger "protected_artifact_write_ledger.jsonl" --capture-manifest "capture/source-capture-manifest.jsonl" --candidate-sources "staging/candidate_sources.jsonl" --candidate-evidence "staging/candidate_evidence.jsonl" --canonical-sources "canonical/canonical-source-ledger.csv" --canonical-evidence "canonical/canonical-evidence-ledger.csv" --corrections "canonical/correction-events.jsonl" --active-sources "canonical/active-source-ledger.csv" --active-evidence "canonical/active-evidence-ledger.csv" --locator-audit "audit/locator-audit.json" --collision-audit "audit/collision-audit.json" --admission-audit "audit/pre-admission-audit.json"
```

准入器在任何正式写入前统一检查采集链、快照哈希、URL 与平台血缘、采集层权威来源类型、内容类型、正文定位、文本哈希、`unit_type` 与 `content_layer`、主维度、同批及跨批去重、完全相同/高度近似/模板尾句碰撞。正文叶块语义由 `content_blocks.py` 的唯一矩阵约束：`official_fact`、新闻、学术与元数据块只能是非用户内容，`user_post`、`user_review`、`comment`、`reply` 只能是用户内容，`page_body` 只作上下文容器；布尔标记不能覆盖块类型。官方或机构页面中经独立定位的真实用户叶块可以形成用户证据，但同页官方背景仍不得评分。候选 `source_category` 不得覆盖采集记录的 `source_type`；冲突时由机器恢复采集值并登记复核，非法值直接拒绝。文本证据没有 snapshot-local locator（快照内定位）即拒绝；候选文本在快照中找不到即返回 `text_not_found_in_source_snapshot`。候选自身声明“独立出现”没有效力；完全一致文本不再重复标作高度近似，只有不同页面实体且通过受信任签章复核的碰撞记录才能标为 `verified_independent_occurrence`，其余碰撞标为 `unresolved_collision` 并排除计分。整个批次全部通过才提升，任一记录失败则本批不产生正式提交。提升输入同时按内容哈希归档，提交文件记录批次哈希链。

canonical 来源与证据写入后禁止原地更新或删除。确需纠错时只能运行 `canonical_correction.py` 追加 `retract` 或 `supersede` 事件并重新物化 active（当前有效）视图；原记录、旧快照及旧哈希永久保留。语义量化、评分、报告和 Office 只读 active 视图或其派生物，绝不能反写 canonical。

检索日志字段由 `assets/search-log-template.csv` 声明，实际观察通过 `execution_facts.py` 校验并提交。计划记录与执行记录必须由 `record_type` 和独立的 `plan_id`/`execution_id` 区分；只有具有执行凭据且状态表明确认为已尝试的执行记录才计入查询数、轮次、预算、覆盖、低增益、阻断和穷尽。`not_run`、`planned`、`queued`、`cancelled`、`skipped` 或无执行凭据记录全部排除，其 `next_action` 也没有终止效力。`query_dimension_targets` 必须是无重复规范维度名的合法 JSON 数组；一条真实查询最多声明三个维度目标。四个及以上目标、重复目标、未知目标或非法结构均为硬错误，该记录从全局、逐轮、逐维、增强、低增益和终止统计全部排除，并阻止正式审计与交付；生成器不得产生这种记录。共享查询优先覆盖多个真实缺口，随后再补各维剩余缺口；同一已执行查询在全局查询数中只计一次，可按声明目标分别记入各维覆盖。查询意图签名先做全半角、空白和安全标点规范化，但保留否定、因果、主体、客体及中英文词序；排序词袋只能作为相似提示，不能作为硬去重键。查询意图跨轮次和恢复持久去重；只有明确瞬时错误理由、原执行引用、受控间隔和未超过上限时允许重试，重试计入资源消耗但不重复增加独立意图覆盖。所有正式来源经 `execution_id → query_id → plan_id` 及匹配的定义、执行哈希和时间窗口关联真实执行，所有正式证据经 `source_id` 和 `source_capture_id` 反向关联真实快照。

### 5. 审计页面、逐维正式证据目标与迭代状态

执行统计、规划、计分数量刷新、逐维审计和最终验收共用 `execution_facts.py`、`execution_semantics.py` 及实际运行状态派生的显式 Schema 上下文。上下文、计划登记或来源执行链不完整时失败封闭。接收序号仅表示提交顺序；路线最新状态按带时区时间、查询身份和重试因果顺序重建，不能按 CSV 行位置判断，合法独立事件迟到不应被拒绝。来源增益按 `execution_id` 归属。失败尝试计资源但不满足每轮有效覆盖；状态、动作、瞬时重试原因及实际页面—来源—证据数量必须通过统一矩阵，非法关联排除后重新审计，不能被 `target_met` 覆盖。目标达成动作须有受保护机器审计依据。具体字段和恢复合同见 `references/execution-facts.md`。独立意图预算只扣减新的独立意图，合法重试仅消耗实际尝试与重试额度；下一轮同时验证两类余量。

本节定义正式审计规则，但首次正式执行必须等到第 6 节完成语义量化、必要人工复核、正式资格派生和检索日志计分数量刷新之后。归并得到的原始候选证据或尚缺 `primary_dimension`、语义方法、复核状态的记录不能直接用于正式审计，也不能据此生成七维缺口结论。

默认少于 150 个去重相关页面即为错误。审计状态为 `needs_iteration` 时不得停下或交付，立即根据缺失维度、平台、正负主题、群体和时间情境生成下一轮计划：

```text
python scripts/build_query_plan.py --task-run-id "本次运行编号" --place "<分析对象>" --iteration-round 1 --audit "证据审计.json" --history "刷新后检索日志.csv" --state "运行状态.json" --output "迭代检索计划.csv"
```

当前发布检索控制由机器校验，并把“独立查询意图”、“实际执行尝试”与“受控重试”分别计数：第 0 轮基础检索的独立意图/实际尝试/重试储备为 120/144/24，深检全局为 300/360/60，全流程重试储备为 84，实际尝试安全上限为 504。深检单轮的独立意图/实际尝试/重试上限为 60/72/12，单维累计为 48/62/14，单维单轮为 10/13/3，最多 5 轮。同一意图的合法重试增加实际尝试和重试数，但绝不增加独立意图、轮次覆盖或穷尽证明。任一预算超限均是先于目标达成或终态判定的硬错误，必须阻断审计、评分和交付。基础预算不得挤占深检预算；第 0 轮只表示基础检索，深检轮次只能为 1 至 5。对从首轮起持续未解的维度，确定性计划保留完整五轮合同：前两轮为 `evidence_gap_fill`，每维每轮至少 8 条独立查询；后三轮为 `medium_enhancement`，每维每轮至少 10 条独立查询。维度在中途改变状态时，生成器依据已完成轮次和增强轮数选择模式，并为剩余必需轮次及重试储备预留预算。某维超额、全局总量或其他维度数量不能补偿缺口。配置加载时先完成严格类型和数学可行性检查；检索控制、状态恢复、正式证据审计和最终验收均使用同一合法执行记录集及稳定哈希，禁止用行数、最大轮次编号或跨维补偿替代逐维完成证明。

`normalized_query_intent` 的落盘值只是完整性断言，所有统计模块必须从查询原字段重新派生规范意图；缺失时使用机器派生值，存在但与派生值不一致时整条记录无效，不得把存储值当作权威输入。持久化查询计划必须绑定 `task_run_id`、对象身份、轮次、当前审计事务、目标置信度、运行状态、已执行历史、检索控制和生成器哈希，并在跨进程锁内通过日志式事务原子提交计划及其绑定 sidecar（伴随文件）。中断恢复只能依据同一事务日志重放；无日志时计划或伴随文件单方存在必须失败封闭。已有计划只有在全部绑定和内容哈希一致时才可复用；跨任务、跨对象、过期审计、历史变化、内容篡改或不完整提交均失败封闭。

阻断、无结果、低增益与穷尽均按维度、平台、来源类型、检索路径、查询意图、正负路径、主体和时间情境限定作用域。同一路线存在多条记录时只读最新有效状态，旧的 `dimension_terminal` 不得覆盖新的活跃状态；任一路线最新状态为 `continue`、`retry`、`pending`、`running`，或刚取得正式计分证据且仍要求继续时，该维度保持非终止。只有已尝试路线的最新状态全部终止且 `terminal_route_count` 等于 `attempted_route_count`，才可形成路线层描述。单条查询、单个平台、单类来源或单一维度的状态不能终止其他维度；预算、轮次、路线阻断和低增益均只是资源与审计事实，不能独立形成正式终态或授权受限交付。只有所有未达目标维度分别通过发布版逐维独立穷尽审计及其机器绑定，才能形成全局短缺终态。

每轮按配置实际执行并记录缺口导向查询，再更新三层台账、去重、编码并审计。仍有明显合法公开路径时继续后续轮次，但必须遵守基础、深检全局、合计安全、单轮、单维查询预算、最大轮数、连续低增益和重复来源失败边界。查询规划器自身读取正式状态、最新机器审计和历史执行意图；超出范围、预算到达、上一轮未完成、绑定不一致、真值已冻结或交付已开始时返回结构化禁止规划结果，不抛出未处理异常，也不依赖执行 Agent 记住停止。禁止继续规划不等于允许交付：若任一未达目标维度缺少有效的机器绑定穷尽决定，任务仍保持可审计的非完成状态。

正式检索终态只允许 `target_met`（全部逐维目标达成）、`dimension_exhausted_with_shortfall`（全部未达目标维度均通过机器绑定的独立穷尽审计）或 `unrecoverable_error`（真实不可恢复错误）。`query_budget_reached_without_dimension_exhaustion`、`round_budget_reached_without_dimension_exhaustion`、`dimension_query_budgets_reached_without_exhaustion`、`source_blocked_pending_dimension_audit`、`route_exhausted_pending_dimension_audit`、`round_sequence_incomplete` 和 `evidence_shortfall` 均为描述性非终态；它们不得进入评分或交付，也不得由 Agent 改写成穷尽。

全部未达目标维度经机器绑定的独立审计确认穷尽后，审计才可使用 `retrieval_terminated_with_shortfall` 受限终态并冻结实际原因与剩余缺口。每个维度的决定必须包含发布版生成的 `exhaustion_machine_binding`，由该维的状态、逐轮真实查询数、强审计检查项、跨台账事实和查询完整性错误重新计算 SHA-256；缺失、复制或篡改的绑定一律维持 `needs_iteration`。受限终态绝不表示证据充分或默认目标达成：仅满足正式资格的维度可按既有规则保留结果，不满足资格的维度不得生成正式分数，报告必须披露停止原因、缺口、当前置信度、可保留结果和不可形成正式结论的维度。断点恢复不得自动把该状态改回可检索状态；只有显式、受控并有审计记录的重开操作才可继续。`--allow-incomplete` 只允许开发测试或明确标为未完成的页面检查，不能代替深度迭代。

七维另行逐维审计，且任何任务级总量（包括历史兼容警戒值 100）都不能替代本维门槛。以下证据数一律指实际计分证据数，来源页面和来源类型也只从同一实际计分集合统计：

- “中”：至少 25 个按唯一主维度归属、通过平台最低样本门槛并实际进入计分的去重证据单元、12 个来自同一计分集合的独立来源页面、3 类实际来源；这是允许正式评价的最低门槛，但不是正常终止目标；
- “中高”：至少 40 个证据单元、20 个独立来源页面、4 类实际来源；
- “高”：至少 60 个证据单元、30 个独立来源页面、5 类实际来源。

七维目标按逐维门槛动态联动：中、中高、高分别对应理论最低实际计分证据总量 175、280、420；该总量只用于资源计划和完整性检查，不能替代逐维门禁。默认优先目标为“中高”，因此在七维均衡达到 40 个实际计分证据前不得仅因总量达到 280 而写 `target_met`；总量 175、190、210、279 或更高，只要任一维仍低于本次目标，均继续显示该维缺口。

低于“中”的维度状态必须为 `needs_iteration`，审计 JSON 会把它写入 `dimension_evidence_gap_targets`；依次生成、执行并记录该维专属查询，不得改检其他维度后直接给本维评分。已达“中”但未达“中高”的维度仍保持 `needs_iteration` 并自动列入 `dimension_evidence_enhancement_targets`；审计驱动的查询计划会同时载入硬缺口与增强目标：

```text
python scripts/build_query_plan.py --task-run-id "本次运行编号" --place "<分析对象>" --iteration-round 2 --audit "证据审计.json" --history "刷新后检索日志.csv" --state "运行状态.json" --output "维度增强检索计划.csv"
```

达到本次运行状态锁定的目标置信度时，维度才可写为 `sufficient` 或显式中目标对应的 `sufficient_at_requested_target`；目标置信度必须在运行状态、审计来源绑定、计划伴随文件、评分输入和最终验收中一致。在默认中高目标下，仅达到“中”时原则上继续本维增强；只有累计至少五轮同维定向检索，其中至少三轮明确为 `medium_enhancement` 且每轮至少十条独立查询意图，并覆盖至少六类目标来源、正负路径、三类主体和三类时间情境，最终增强轮记录 `exhausted`，且跨台账满足最近三轮低增益追踪或最终轮全部阻断追踪，才可写 `sufficient_at_medium_after_audit`。该状态是可数值评价的独立审计终态，但不是默认中高目标已达成，也不是低置信穷尽。详细页数、域名和实际来源类型门槛见 `references/seven-plus-one-report-style.md`；模型自述或手填审计字段不能放行。

低于“中”时，不接受模型自由陈述“已穷尽”。`exhausted_with_shortfall` 必须同时通过以下独立复算条件：至少四轮针对同一维度的深检；每轮至少 8 条实际执行查询；目标覆盖至少 5 类来源；最终轮记录 `exhausted`；并满足跨台账低增益路径或阻断路径之一。低增益路径要求最近两轮各新增本维有效证据少于 2 个，且检索记录实际关联至少 12 个相关来源页面、3 个域名和 3 类实际来源；阻断路径要求最终轮所有查询均为 `blocked` 或 `no_results`，且仍有至少 5 个相关来源页面和 2 个域名可追踪。审计脚本必须生成并验证该维 `exhaustion_machine_binding`，Office 验收器再从底层台账独立复算；任一不通过即保持 `needs_iteration`。

独立审计确认为穷尽后仍不足时，保留该维并显著写“低置信度”或“数据不足”，不得删除维度、虚构证据或将权重重分配给其他维度。相应任务审计状态为 `valid_with_dimension_shortfall`，若同时存在总体样本缺口则为 `valid_with_sample_and_dimension_shortfall`。

### 6. 量化缺少原生数值的文字

正式任务直接读取 `assets/evaluation-protocol.json` 和 `assets/semantic-quantification-codebook.json`。二者已在 Skill 版本发布时完成在线方法检索、人工评审、回归与基准测试并锁定；任务开始前核对评价协议版本、语义代码簿版本和代码簿 SHA-256。单次地点任务不得再次联网改写规则，不得临时改变权重、阈值、词表、上下文规则、置信度规则或编码解析政策，也不得生成与 `task_run_id` 绑定的临时代码簿。

发布维护专用的 `scripts/calibrate_evaluation_protocol.py` 只可在修订 Skill 时使用，执行顺序固定为：联网核验方法与规则 → 记录来源和决策 → 人工评审 → 回归与基准测试 → 锁定代码簿及 SHA-256 → 发布新版本。正式地点研究不得调用该脚本。评分规则、权重或代码簿实质变化必须提升评价协议或代码簿版本；仅改报告模板、工具兼容性或非评分实现时可保持评价协议版本不变，但仍须回归验收。

锁定代码簿同时包含中文词项、英文词项、中文与英文近义释义、双语定义、稳定概念编号和机器读取路径。所有具备评分资格但没有可直接使用数值的文字，按 `references/semantic-quantification-methods.md` 执行分析单位切分、主题归一、七维方面归属、极性与强度计算、上下文修正和置信度判断。原生星级或量表值写入 `native_rating_*`，规则推断写入 `semantic_*`；两者不得混写。

单条正式评分资格由 `scripts/formal_scoring.py` 与发布协议共同确定。必须同时满足 `used_for_scoring=true`、`is_valid=true`、`is_direct_place_evidence=true`、`score_scope=direct`、唯一有效主维度、同维去重、允许的证据类型、经定位复验的用户内容语义、合格语义或原生数值、可靠性阈值、语义与方面置信度阈值及已完成复核等条件。评分入口重新执行正文块类型—用户属性矩阵；缓存的资格、内容层或布尔标记不能绕过冲突。`coding_parse_fallback`、未确认开放或辅助编码、`review_required`、低可靠性、低置信度、未人工确认的反讽风险记录不得进入候选集合，禁止为提高样本量降低准入标准。

有代码环境时运行：

```text
python scripts/manage_run_state.py checkpoint --state "运行状态.json" --phase SEMANTIC_QUANTIFICATION --last-action "候选批次已通过正式准入" --next-action "语义量化与必要复核"
python scripts/quantify_text_semantics.py --input "canonical/active-evidence-ledger.csv" --sources "canonical/active-source-ledger.csv" --codebook "assets/semantic-quantification-codebook.json" --protocol "assets/evaluation-protocol.json" --output "derived/semantic-evidence.csv" --audit-output "audit/semantic-audit.json" --coding-queue "staging/coding-queue.jsonl" --place "<分析对象>" --apply-auto-codes --state "运行状态.json" --writer-ledger "protected_artifact_write_ledger.jsonl"
```

将输出的语义量化台账作为后续证据台账。脚本保留语言、否定、程度、转折、弱化、维度命中和逐线索贡献轨迹，只对中英词表完整命中且同时达到语义置信度、维度置信度和证据可靠性阈值的记录回填候选编码。

维度或极性任一层未命中即启动编码解析；两层均未命中时必须启动，不得直接判中性、填零或停止。解析器使用双语词项、近义释义和定义画像生成候选及稳定 `OPEN-*` 编号，写入 `coding_parse_*`，并强制 `semantic_method=coding_parse_fallback`、`review_required` 和 `formal_scoring_eligible=false`。上游 `used_for_scoring` 只是研究选择意图，不能绕过复核与正式资格门禁，也不应由编码解析器覆盖。随后通过固定人工复核决定合同确认 `assisted_semantic` 或 `manual_code`，保留编码者编号、理由、时间和裁决状态；人工决定不得填写正式资格、倾向值或最终分数。仍未解的记录保留在队列但不得进入评分；本次发现的新词、俚语或候选表达只进入未来版本的发布校准候选清单，不得反写当前锁定代码簿。

需要人工或辅助编码时，复制 `assets/人工复核决定模板.csv`，只填写合同允许字段，再运行：

```text
python scripts/apply_review_changes.py --evidence "derived/semantic-evidence.csv" --decisions "staging/人工复核决定.csv" --output "derived/reviewed-evidence.csv" --change-ledger "audit/人工复核变更台账.csv" --trusted-review-key "<宿主提供的受信任复核密钥文件>" --state "运行状态.json" --writer-ledger "protected_artifact_write_ledger.jsonl"
python scripts/quantify_text_semantics.py --input "derived/reviewed-evidence.csv" --sources "canonical/active-source-ledger.csv" --codebook "assets/semantic-quantification-codebook.json" --protocol "assets/evaluation-protocol.json" --output "derived/formal-scoring-evidence.csv" --output-role "formal_evidence" --audit-output "audit/formal-semantic-audit.json" --place "<分析对象>" --apply-auto-codes --state "运行状态.json" --writer-ledger "protected_artifact_write_ledger.jsonl"
```

第一条命令只写入人工确认判断和追加式变更台账；第二条命令必须使用发布版确定性算法补齐语义分、置信度、可靠性、聚合权重和正式评分资格。不得另写临时脚本直接修改 `formal_scoring_eligible`、`included_in_platform_score`、倾向值、转换分、加权分或综合分。

无需人工复核或当前没有可信人工决定时，不运行复核写入命令；第二条量化命令的输入改为 `derived/semantic-evidence.csv`，仍以 `formal_evidence` 角色生成正式派生台账。未确认记录继续保留待复核状态并按既有资格规则排除，不得伪造确认或漏掉正式派生步骤。

`review_required`、反讽风险、维度冲突、编码解析候选和无方向线索记录不得进入正式评分。没有代码执行能力时仍须读取并记录同一锁定评价协议，按相同规则人工填写全部字段，不能省略版本、SHA-256、轨迹和复核状态。

若采用两名或多名独立编码者，把 `evidence_id,coder_id,primary_dimension,sentiment,stance_strength` 写入双编码表并通过 `--agreement-input` 计算一致性。报告代码簿试编码、分歧、裁决和一致性结果；不能只给最终分数。

语义量化与必要复核完成后，先由正式计分字段刷新检索日志，再首次运行正式证据审计：

```text
python scripts/refresh_search_log.py --search-log "检索日志.csv" --sources "canonical/active-source-ledger.csv" --evidence "derived/formal-scoring-evidence.csv" --output "刷新后检索日志.csv" --state "运行状态.json" --writer-ledger "protected_artifact_write_ledger.jsonl"
python scripts/manage_run_state.py checkpoint --state "运行状态.json" --phase EVIDENCE_AUDIT --last-action "量化及正式资格派生完成，计分数量已刷新" --next-action "运行正式证据审计"
python scripts/audit_evidence.py --sources "canonical/active-source-ledger.csv" --evidence "derived/formal-scoring-evidence.csv" --search-log "刷新后检索日志.csv" --target-confidence "中高" --output "audit/evidence-audit.json" --state "运行状态.json" --writer-ledger "protected_artifact_write_ledger.jsonl"
python scripts/manage_run_state.py sync-audit --state "运行状态.json" --audit "audit/evidence-audit.json"
```

审计仍有 `needs_iteration` 时，状态机强制返回 `SEARCH`，只根据机器审计缺口继续检索，然后重新执行证据构建、语义量化、必要复核、数量刷新和正式审计。七维均达到最低门槛或通过严格独立穷尽审计后，再生成平台评分输入：

```text
python scripts/artifact_provenance.py freeze --state "运行状态.json" --writer-ledger "protected_artifact_write_ledger.jsonl" --output "truth_freeze_manifest.json" --artifact "search_log=刷新后检索日志.csv" --artifact "source_capture_manifest=capture/source-capture-manifest.jsonl" --artifact "canonical_source_ledger=canonical/canonical-source-ledger.csv" --artifact "canonical_evidence_ledger=canonical/canonical-evidence-ledger.csv" --artifact "correction_event_ledger=canonical/correction-events.jsonl" --artifact "locator_audit=audit/locator-audit.json" --artifact "collision_audit=audit/collision-audit.json" --artifact "pre_admission_audit=audit/pre-admission-audit.json" --artifact "source_ledger=canonical/active-source-ledger.csv" --artifact "raw_evidence=canonical/active-evidence-ledger.csv" --artifact "semantic_evidence=derived/semantic-evidence.csv" --artifact "formal_evidence=derived/formal-scoring-evidence.csv" --artifact "evidence_audit=audit/evidence-audit.json"
python scripts/manage_run_state.py checkpoint --state "运行状态.json" --phase SCORE --truth-freeze "truth_freeze_manifest.json" --last-action "正式研究真值已冻结"
python scripts/quantify_text_semantics.py --input "derived/formal-scoring-evidence.csv" --output "derived/formal-scoring-evidence.csv" --output-role "formal_evidence" --sources "canonical/active-source-ledger.csv" --search-log "刷新后检索日志.csv" --codebook "assets/semantic-quantification-codebook.json" --protocol "assets/evaluation-protocol.json" --platform-scores "derived/platform-scores.json" --dimension-audit "audit/evidence-audit.json" --place "<分析对象>" --apply-auto-codes --state "运行状态.json" --writer-ledger "protected_artifact_write_ledger.jsonl"
```

### 7. 编码七维并计算跨平台结果

按照 `references/scoring-rubric.md` 从正式评分证据台账自动生成平台输入，不允许模型根据报告叙述手工填写 `tendency`（倾向值）或最终分数。统一数据链为：原始网络证据 → 证据台账 → 语义量化与复核 → 单条评分候选 → 平台×维度最低样本门禁 → 实际计分证据 → 平台倾向值 → 跨平台七维得分 → 综合评价 → Office 成果。平台评分输入必须携带同一运行的 `dimension_evidence_audit`。审计中仍有 `needs_iteration` 的维度时计算器拒绝评分。无可识别证据且已通过独立穷尽审计的维度写 `null` 或“数据不足”，不得机械填写 0 倾向或 50 转换分；任何缺维结果只保留为局部结果，平台总分和跨平台总分不得通过重分配缺失权重生成。

每个平台每个维度必须同时输出 `eligible_evidence_units`、`scored_evidence_units`、计分正中负数量、发布协议中的最低样本门槛、`minimum_sample_met`、`platform_sample_status` 和 `tendency`。未达门槛时状态固定为 `insufficient_platform_samples`，实际计分数和正中负计分数均为 0，倾向值为 `null`。正中负计数必须来自与倾向值完全相同的记录集合。正式评分置信度以实际计分证据和同一集合的来源广度为基础；检索覆盖置信度可以另行说明，但不能替代正式评分置信度。

将平台输入填入 `assets/platform-scores-template.json`，运行：

```text
python scripts/calculate_scores.py --input "平台评分.json" --output "综合评分.json" --state "运行状态.json" --writer-ledger "protected_artifact_write_ledger.jsonl" --truth-freeze "truth_freeze_manifest.json"
python scripts/artifact_provenance.py extend-scores --state "运行状态.json" --writer-ledger "protected_artifact_write_ledger.jsonl" --manifest "truth_freeze_manifest.json" --artifact "platform_scores=平台评分.json" --artifact "scoring_output=综合评分.json"
python scripts/compile_report_truth.py --state "运行状态.json" --writer-ledger "protected_artifact_write_ledger.jsonl" --truth-freeze "truth_freeze_manifest.json" --sources "canonical/active-source-ledger.csv" --raw-evidence "canonical/active-evidence-ledger.csv" --formal-evidence "derived/formal-scoring-evidence.csv" --search-log "刷新后检索日志.csv" --dimension-audit "audit/evidence-audit.json" --platform-scores "derived/platform-scores.json" --scores "derived/composite-scores.json" --protocol "assets/evaluation-protocol.json" --codebook "assets/semantic-quantification-codebook.json" --output "derived/report_truth.json"
python scripts/preflight_validate.py --state "运行状态.json" --writer-ledger "protected_artifact_write_ledger.jsonl" --truth-freeze "truth_freeze_manifest.json" --sources "canonical/active-source-ledger.csv" --raw-evidence "canonical/active-evidence-ledger.csv" --semantic-evidence "derived/semantic-evidence.csv" --formal-evidence "derived/formal-scoring-evidence.csv" --search-log "刷新后检索日志.csv" --dimension-audit "audit/evidence-audit.json" --platform-scores "derived/platform-scores.json" --scores "derived/composite-scores.json" --source-capture-manifest "capture/source-capture-manifest.jsonl" --canonical-sources "canonical/canonical-source-ledger.csv" --canonical-evidence "canonical/canonical-evidence-ledger.csv" --corrections "canonical/correction-events.jsonl" --locator-audit "audit/locator-audit.json" --collision-audit "audit/collision-audit.json" --admission-audit "audit/pre-admission-audit.json" --report-truth "derived/report_truth.json" --output "audit/pre-report-preflight.json"
```

七维初始研究权重固定为：历史文化感知 15%、在地文化特征 15%、生活文化延续 20%、文化实践体验 15%、场所氛围体验 10%、保护活化感知 15%、承载治理体验 10%。综合公式为 `S = 0.15H + 0.15L + 0.20C + 0.15P + 0.10A + 0.15R + 0.10G`。除非任务明确提供经正式披露的专家赋权、层次分析、德尔菲法或实证替代权重，否则不得改动或静默覆盖；即使替换，也必须同时保留初始权重结果作基线。“历史文化活态传承感知评价”只作为七维结果层，不另设并列权重，也不再次参加聚合。

分别保留平台等权分、受单平台 50% 上限约束的样本加权分、有效样本数、覆盖权重和排除项。“平台等权”是平台之间的合并方式，不表示七个分析维度等权。样本量或计数口径不可比时，优先把平台等权分作为主结果，把样本加权分作为敏感性参照，并解释选择。

### 8. 编写十五部分结构化报告数据

报告机器事实只能来自 `compile_report_truth.py` 生成并登记的 `derived/report_truth.json`。页面、来源、用户来源、三层证据数量、正中负计分数、七维计数、来源页及类型、三个平台计数字段、定向深检轮次列表与数量、置信度、倾向、转换分、加权分、综合分、协议、代码簿、阈值、固定方法披露和“+1”结果层结构均禁止执行 Agent 或语言模型填写。

语言模型只填写 `assets/report-narrative-template.json` 所允许的定性字段：七维解释、正负主题、主体/时间/情境与平台差异、机制、反证、替代解释、不确定性边界、综合判断及结论性文字。叙事不得包含机器数值字段或以自然语言覆盖机器事实。运行 `assemble_report_data.py` 后，脚本将机器事实、固定 `assets/report-data-template.json` 与合格叙事组合成唯一 `derived/report_data.json`；叙事最多两轮、正式报告数据最多两次提升，仍不收敛时进入 `report_narrative_nonconvergent`。

DOCX 架构固定为封面、单页目录、研究口径说明和十五章。封面及目录之后各有一个分页符；报告恰好十二张表，其章节、表头、数据行下限和顺序见 `references/report-schema.md`。第 1、2、4、6、9、11 节的二级标题顺序固定，第 12 节使用六项 `judgments`。

每个维度恰好三个三级标题：主要正面评价、主要负面评价和覆盖主体、时间或场景差异的第三标题。数据层分别填写 `positive`、`negative`、`differences`、`fact_perception`、`mechanism_analysis`、`counterevidence`、`uncertainty_boundary` 与 `synthesis_judgment`。每维论证必须按“证据 → 感知 → 作用机制 → 反证或替代解释 → 不确定性与适用边界 → 综合判断”推进；结论只能综合提升，不能复制前文。每维定义与编码边界必须严格来自唯一“7+1”框架，并显示固定二十九项标签，包括三层证据数量、计分构成、有效平台、初始研究权重、优先置信度目标及中置信度终止审计。不得只复述表格或分数。

第 2、4、13 节分别报告 150 个独立页面门槛、所选逐维置信目标、对应理论最低正式证据规模、历史兼容警戒下限、实际量、计划记录与执行记录数量、迭代结果和合法短缺状态；不得把 100 写成默认完整终止规模。第 13 节至少七段且不少于 1500 个中文字符。来源清单之前的叙事和分析表格不少于 12500 个中文字符，建议控制在 13500—16500；篇幅只能来自实质分析，不能来自重复句、近义改写、前后套用、跨维模板、无关背景或来源清单。满足字数不抵消重复、浅表或低质量分析，生成器和验收器会检查完全重复与近重复段落。第 15 节恰好一行十九字段。

DOCX 全文只允许使用黑体、宋体和 Times New Roman 三种字体：标题中文使用黑体，正文中文使用宋体，拉丁字符使用 Times New Roman。报告正文中的任何英文词、英文缩写或英文短语必须就近写出中文释义，格式使用 `English（中文释义）` 或 `中文释义（English）`；网址、任务编号和第 15 节机器行不适用。不得用未附中文释义的英文装饰标题或替代中文分析。

结构化 JSON 是跨平台中间格式，不是最终报告。第 14 节的完整来源表由 DOCX 生成脚本从来源台账自动写入并添加可点击链接。

预报告预检有效后迁移到 `REPORT_BUILD`，再对叙事进行机器 lint（校验）并一次性组装正式报告数据；Office 生成器只读取这一登记版本：

```text
python scripts/manage_run_state.py checkpoint --state "运行状态.json" --phase REPORT_BUILD --preflight "audit/pre-report-preflight.json" --last-action "全链预检通过"
python scripts/assemble_report_data.py --state "运行状态.json" --writer-ledger "protected_artifact_write_ledger.jsonl" --report-truth "derived/report_truth.json" --narrative-input "staging/report_narrative.json" --narrative-output "derived/report_narrative.json" --template "assets/report-data-template.json" --output "derived/report_data.json"
```

### 9. 生成两份固定用途 XLSX 与正式 DOCX

先生成 XLSX：

```text
python scripts/build_research_workbook.py --sources "canonical/active-source-ledger.csv" --evidence "derived/formal-scoring-evidence.csv" --search-log "刷新后检索日志.csv" --limitations "数据限制.csv" --scores "derived/composite-scores.json" --place "<分析对象>" --retrieval-date "YYYY-MM-DD" --output "网络检索与量化编码_<分析对象>_YYYYMMDD.xlsx" --state "运行状态.json" --writer-ledger "protected_artifact_write_ledger.jsonl" --truth-freeze "truth_freeze_manifest.json" --preflight "audit/pre-report-preflight.json"
```

工作簿固定包含：`样本明细`、`来源页面`、`主题编码`、`七维评分`、`检索日志`、`数据限制说明`、`机器可读汇总`。全部检索页面进入“来源页面”，全部实际查询和迭代轮次进入“检索日志”。“样本明细”必须包含“摘要原文”、原生数值、语义语言、词表命中状态与轨迹、编码解析状态与候选、开放编码编号、语义方法、维度依据、主题标签、规则命中、连续与整数分、语义置信度、证据可靠性、平台内聚合权重、复核状态、正式候选资格、排除理由、去重键、平台样本状态、实际计分标记、正式计分方向、规则轨迹和任务运行编号。“七维评分”同一行的候选数、实际计分数、正中负数、平台状态、倾向值、转换分和加权分必须全部读取同一个正式评分结果对象；不得从主题覆盖统计补填计分字段。雷达图只读取七维正式转换分，缺失值保留为空点并明确为数据不足，不能静默绘成 0 分。“检索日志”必须包含迭代轮次、缺口目标、新增与累计有效样本和下一动作。所有面向用户的表头、状态、枚举和说明只要存在准确中文表达就必须显示中文；稳定机器字段和值仅保留在机器可读层，不能以英文替代可用中文。视觉规范为深蓝表头、Carlito 10 号白色粗体、居中、自动换行、34 点表头行和带筛选表格；模板见 `assets/网络检索与量化编码_模板.xlsx`。

再生成本次规则 XLSX：

```text
python scripts/build_semantic_rules_workbook.py --codebook "assets/semantic-quantification-codebook.json" --protocol "assets/evaluation-protocol.json" --task-run-id "本次运行编号" --place "<分析对象>" --retrieval-date "YYYY-MM-DD" --output "非量化文本量化评价规则_<分析对象>_YYYYMMDD.xlsx" --state "运行状态.json" --writer-ledger "protected_artifact_write_ledger.jsonl" --truth-freeze "truth_freeze_manifest.json" --preflight "audit/pre-report-preflight.json"
```

文件名固定为 `非量化文本量化评价规则_<地点>_<YYYYMMDD>.xlsx`；工作表固定且只能按以下顺序出现：`评价协议摘要`、`发布校准来源`、`发布校准决策`、`极性词表`、`七维词表`、`上下文规则`、`编码解析规则`。每张表的表头、列序、样式和数据含义由生成器与验证器共同固化，不得按任务自由改名、增删或换序。该文件必须导出发布时在线校准并锁定的评价协议，记录本次地点任务编号和检索日期，同时明确任务期没有改写规则；还要包含中英词项、机器可读近义释义、双语定义、发布校准来源与决策、阈值和零命中解析政策。空白模板见 `assets/非量化文本量化评价规则_模板.xlsx`。

再生成 DOCX：

```text
python scripts/build_docx_report.py --input "derived/report_data.json" --sources "canonical/active-source-ledger.csv" --scores "derived/composite-scores.json" --output "历史文化活力分析报告_<分析对象>_YYYYMMDD.docx" --state "运行状态.json" --writer-ledger "protected_artifact_write_ledger.jsonl" --truth-freeze "truth_freeze_manifest.json" --preflight "audit/pre-report-preflight.json"
```

生成器会先校验封面日期、研究口径、十五章、十一张分析表、七维对象、七维初始研究权重、三个正文标题、逐维二十九项摘要与审计字段、深层论证字段、反重复规则、英文中文释义、篇幅、转换分公式和来源 URL；任何字段缺失、顺序错误、正文不完整、近重复或分析浅表时停止生成。第十二张来源表由来源台账自动生成。

### 10. 重新打开并联合验证四份成果

运行：

```text
python scripts/validate_deliverables.py --report "历史文化活力分析报告_<分析对象>_YYYYMMDD.docx" --workbook "网络检索与量化编码_<分析对象>_YYYYMMDD.xlsx" --rules-workbook "非量化文本量化评价规则_<分析对象>_YYYYMMDD.xlsx" --sources "canonical/active-source-ledger.csv" --evidence "derived/formal-scoring-evidence.csv" --search-log "刷新后检索日志.csv" --scores "derived/composite-scores.json" --report-data "derived/report_data.json" --raw-evidence "canonical/active-evidence-ledger.csv" --semantic-evidence "derived/semantic-evidence.csv" --dimension-audit "audit/evidence-audit.json" --detailed-log "详细运行日志_<分析对象>_<YYYYMMDD_HHMMSS>.txt" --skill-root "<正式Skill目录>" --release-manifest "发布版完整性清单.json" --state "运行状态.json" --writer-ledger "protected_artifact_write_ledger.jsonl" --truth-freeze "truth_freeze_manifest.json" --preflight "audit/pre-report-preflight.json" --source-capture-manifest "capture/source-capture-manifest.jsonl" --canonical-sources "canonical/canonical-source-ledger.csv" --canonical-evidence "canonical/canonical-evidence-ledger.csv" --corrections "canonical/correction-events.jsonl" --locator-audit "audit/locator-audit.json" --collision-audit "audit/collision-audit.json" --admission-audit "audit/pre-admission-audit.json" --report-truth "derived/report_truth.json" --report-narrative "derived/report_narrative.json" --output "交付验收.json" --error-manifest "validation_error_manifest.json"
```

验证器重新解析三个 Office 包和实时详细运行日志，并从原始、语义与正式三层证据独立执行一次完整复算；验收器只与生成器共享单条评分资格规则，不能把生成后的聚合结果当作真值。它必须先验证发布版哈希、单一任务运行编号、证据 ID 子集链、机器绑定审计和日志终态，再重新按主维度、平台、同维机器去重键、可靠性、置信度和复核状态筛选候选，重新执行平台最低样本门禁，重算实际计分数、正中负数量、平台倾向值、跨平台七维分、转换分、固定权重加权分和综合分，最后逐项比对评分 JSON、报告数据 JSON、DOCX 与 XLSX。任一“证据状态充分但分数为空”“计分证据不足却存在正式分数”“正中负之和不等于实际计分数”“平台或跨平台结果无法复算”“加权分无法按固定权重复算”“雷达图把缺失写成 0”“日志或发布版完整性不一致”均为硬失败。除此之外继续检查主工作簿七表顺序、表头样式、摘要原文、语义与编码字段、单一运行编号、150 页与总体深检、逐维证据、全部来源 ID、公式和图表；规则工作簿的固定结构与协议完整性；DOCX 的固定架构、分析深度、反重复、限定字体、英文中文释义、代表性链接、来源 URL、OOXML 完整性和敏感参数。只有在两轮上限内消除全部错误才能宣布完成；警告必须人工核查并在报告限制部分解释。

若第一轮验收失败，先登记结果，再用确定性修复计划按 `root_cause_layer` 合并全部下游症状。只允许修复一个可修复的最上游根因；若冻结研究真值自身有错，本次运行失效，必须保留现场并重新启动独立运行，不得在验收后改写。若仅为下游生成问题，废弃旧报告数据、DOCX 和两份 XLSX，从同一冻结评分真值整体重建一次：

```text
python scripts/manage_run_state.py record-validation --state "运行状态.json" --validation "交付验收.json" --error-manifest "validation_error_manifest.json"
python scripts/validation_repair_plan.py --validation "交付验收.json" --task-run-id "本次运行编号" --error-manifest "validation_error_manifest.json" --repair-plan "集中修复计划.json"
```

第二轮验收仍失败、稳定错误复现、错误数量不下降或出现新的上游真值错误时，状态机直接进入 `validation_nonconvergent`。不得生成第三轮验收，也不得创建循环式自动修复程序。

联合验证无错误后登记终态并检查最终回复资格：

```text
python scripts/manage_run_state.py record-validation --state "运行状态.json" --validation "交付验收.json" --error-manifest "validation_error_manifest.json"
python scripts/manage_run_state.py finish --state "运行状态.json" --docx "历史文化活力分析报告_<分析对象>_YYYYMMDD.docx" --xlsx "网络检索与量化编码_<分析对象>_YYYYMMDD.xlsx" --rules-xlsx "非量化文本量化评价规则_<分析对象>_YYYYMMDD.xlsx" --detailed-log "详细运行日志_<分析对象>_<YYYYMMDD_HHMMSS>.txt" --validation "交付验收.json" --validation-error-manifest "validation_error_manifest.json" --delivery-provenance "formal_delivery_provenance.json"
python scripts/manage_run_state.py assert-final --state "运行状态.json"
```

`finish` 使用同一进程锁内的可恢复最终化事务，依次固定输入哈希、写入并绑定最终日志事件与摘要、生成交付封印、登记成果，最后才原子提交 `status=complete`。封印绑定受保护写入台账在登记前的不可变字节前缀及其事件计数、尾事件和 SHA-256；该前缀之后必须且只能存在一条与同一事务、封印路径和封印哈希完全一致的 `delivery_provenance` 登记事件，任何额外写入、缺失登记或第二条登记均失败。每一步使用同一最终化事务编号；任一中断都保持非完成状态或留下可核验事务，恢复不得重复摘要、封印、事件或修订。临时状态文件写入中断、正式状态替换后清理中断及重复恢复都必须收敛到同一终态。`assert-final` 未通过时不得发送完成式最终答复；根据状态中的 `next_action` 继续调用工具。

## 完成标准

执行字段以 `execution_schema.py` 为唯一合同；使用观察模板前核对同源 Schema。深检缺口和轮次前置信度必须从已绑定机器审计的计划继承，写入者不得补造。执行更正只能走 `execution_facts.py --amendments` 的追加事件路径，详见 `references/execution-facts.md`；更正或废止后重新量化、刷新和审计，不沿用旧结论。来源域名从受保护 URL 的完整主机名复算，日期按共享严格解析器验证，不以警告替代硬门禁。

只有同时满足以下条件才宣布完成：

- 本次运行实际联网并记录所有查询、页面和访问限制；
- 本次事实、样本、链接、评分和结论均来自同一 `task_run_id`，未使用模型记忆、旧对话或旧任务内容；
- 去重后的相关网页不少于 150 个，且来源类型已尽可能扩展；
- 实际计分证据达到所选逐维置信目标对应的理论最低规模，且逐维分布均通过门禁；默认中高目标的理论最低总量为 280，历史兼容警戒值 100 不作为完整终止条件；若仍有缺口，则所有不足维度均已通过机器绑定的独立穷尽审计，审计才写 `retrieval_terminated_with_shortfall` 并以实际最大样本形成受限成果；
- 七个维度各有至少 25 个按唯一主维度归属的实际计分证据并达到不低于“中”的正式评分置信度，且优先达到“中高”或“高”；停在“中”已经五轮累计、三轮增强、每轮十条、六类目标来源、正负路径、主体和时间覆盖及跨台账追踪的强终止审计，低于“中”的例外已经严格穷尽审计，不存在模型自报穷尽；
- 大众点评和小红书按未登录访问限制、另有独立获取路径及后续综合分析的理由默认剔除，受限访问未被绕过，也未与独立结果重复计数；
- 页面、证据和查询三层记录可关联，转载、镜像和重复内容已去重；
- 正式页面数按机器生成的页面实体统计，规范 URL、最终跳转、内容指纹、碰撞合并理由和可信独立出现依据均可复算；
- 检索有效证据、评分候选证据与实际计分证据三层数量分开；`dimension_tags` 只用于主题覆盖，正式门禁和计分只按唯一 `primary_dimension`；多维文本已经拆成独立证据单元；
- 事实来源未混入用户情感样本，推广内容未计入普通用户评分；
- 非量化文字使用发布时在线校准、评审、回归并锁定的评价协议；评价协议版本、语义代码簿版本、发布校准来源与决策及代码簿 SHA-256 可追溯，单次任务未更新或改写规则；
- 锁定代码簿同时包含中文、英文及机器可读近义释义；零命中和部分命中均启动编码解析，未经确认的候选未进入评分，新表达只进入未来版本校准候选清单；
- 缺少原生数值的合格文字已按方法库量化；原生数值与语义推断分栏，低置信、冲突、反讽和待复核记录未进入正式评分；
- 无正文原生数值仅在原值、量表、评价对象、平台、页面实体、采集时间、来源和快照全部可复验时进入既有换算链，未生成虚假摘录；
- 每条语义评分均能从原文、维度命中、上下文修正、规则轨迹、语义置信度和证据可靠性重算；
- 七维均按材料规定的名称、定义、检索内容和边界完整保留，并按三标题正文样式呈现八个结构化叙事字段、固定二十九项标签和代表性来源；初始研究权重严格为 15%、15%、20%、15%、10%、15%、10%，总和 100%；“历史文化活态传承感知评价”只作为结果层，不另设权重且不重复加权，没有跳维、重分权重或用汇总表替代详细分析；
- 每个平台维度的候选数、实际计分数、最低样本状态、正中负计分数和倾向值来自同一记录集合；平台等权和样本加权结果可从底层证据复核；
- DOCX 详细解释证据、感知、差异、机制、反例、替代解释、不确定性边界和评分形成过程，未前后套用、近义改写、简单复述表格或为字数堆砌低质量内容；
- DOCX 只使用黑体、宋体和 Times New Roman，正文中的英文词、缩写和短语均已就近附中文释义；
- 主 XLSX“样本明细”包含摘要原文及完整语义量化、词表命中和编码解析字段，“检索日志”包含全部基础与迭代查询；
- 规则 XLSX 严格采用固定文件名、七表顺序和精确表头，并完整导出本次实际规则；
- 实际生成并重新打开一份 DOCX 报告、两份 XLSX 工作簿和一份从初始化开始实时追加的详细运行日志 TXT；
- DOCX、两份 XLSX、详细运行日志、三层证据、来源台账和计算结果的页面数、样本数、分数、规则、状态与结论一致；
- 全链预检为有效，正式 validation 不超过两轮且最终零错误，运行未进入任何非完成终态；
- 所有正式台账、审计、评分与 Office 成果的当前哈希均存在于受保护写入台账，且与发布清单、真值冻结和详细日志使用同一运行编号；
- 采集清单与受保护写入台账的编号分配、前序哈希、事件哈希和追加写入在同一跨进程临界区完成；并发写入、异常中断、截断尾行和恢复路径均已复验；
- 状态、快照和冻结工件使用运行根目录相对逻辑路径与内容哈希绑定，迁移到含中文或空格的新目录后仍能复验；旧绝对路径只作显式兼容迁移，不进入新哈希真值；
- 正式交付封印完整绑定发布清单、真值冻结、来源、原始/语义/正式证据、检索日志、维度审计、平台评分、综合评分、三份 Office 成果、详细运行日志、验收与错误清单哈希，并绑定登记前写入台账的不可变前缀；该前缀之后恰有一条同事务的封印登记事件且没有其他写入；
- `assert-final` 已验证状态为完成、阶段为交付、验收零错误、冻结真值未变、日志终态一致且不存在不可信写入、验收后上游变更、状态日志分歧或非收敛状态；
- 四份最终成果位于用户指定的唯一输出目录，且不含凭据、授权状态或无关个人信息。
