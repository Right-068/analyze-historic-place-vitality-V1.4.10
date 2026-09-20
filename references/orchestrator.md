# 统一编排入口与宿主输入合同

`run_research.py` 只管理既有流程的衔接。搜索、打开页面、判断来源性质、选择语义单元和撰写解释仍由当前宿主实际执行；程序不创造网页、人工身份或研究结论。评分、来源准入、冻结、状态和 Office 沿用发布模块，编排器没有正式台账写入权限。

## 命令与下一动作

init 立即锁定动作模式，直接返回首份 `operation/input_contract/action_id`。宿主随后仅用 `execute-next-action`，按合同提交整批。它自动处理机械模块与恢复；下面的兼容命令不是宿主可选路径，从初始化起即拒绝绕行。背景事实保留来源，不进入感知编码；未确认用户记录默认正式不计分并继续原审计，不把普通待复核当用户阻塞。详情见批量合同。

所有命令指定同一个 `--run-dir`，目录在 Skill 外。`init --place` 创建新运行；同目录同对象重入恢复旧运行，不新造编号。`next` 重查当前状态及完整已登记计划，仅返回当前持久化执行窗口。`window_id/window_size/window_query_ids/plan_total/completed_query_count/remaining_query_count/next_cursor` 明确区分完整计划与当前负载。窗口内只暴露未完成项；已完成窗口自动派发下一窗口。执行负载只读 `assets/runtime-dispatch.json`，连续健康窗口可扩大，连续局部失败可缩小，不改查询或研究预算。

## 既有适配器兼容路径（不作为默认调度）

默认执行顺序及批量合同见 `references/batch-pipeline.md`。下列单项接口仅为未启用批量模式的既有适配器保留；批量模式中公共 Python 方法与 CLI 均拒绝调用，只能由内部兼容层执行。动作模式也禁止宿主自行调用各模块命令。

先用 `begin-query --execution-id <next返回值>` 记录实际开始点，再调用宿主原生搜索。把真实搜索返回保存为文本文件，以 `record-search --execution-id <编号> --body-file <真实搜索响应> --result-count <实际返回数> --url <发现URL>` 登记；多个 URL 可重复 `--url`。程序从已登记计划生成全部机器绑定。没有结果如实填零，记录后继续其他查询。

搜索响应只作 discovery，必须继续打开实际页面。每份实际正文用：

```text
python scripts/run_research.py record-page --run-dir "<运行目录>" --execution-id "<编号>" --url "<请求URL>" --final-url "<最终URL>" --page-title "<实际标题>" --body-file "<真实页面正文>" --tool-name "<实际工具名>" --source-category "<来源分类>" --content-layer "<内容层>" --relevant --evidence-file "<候选判断数组.json>"
```

`--retrieved-at` 有实际时刻时保留；未提供时，程序在接收到真实工具响应时记录带时区观察时间，不能用于给旧材料补造读取时间。`--tool-call-id/--result-ref` 仅在宿主真实暴露时提供。当前时间、本地ID、哈希、schema、绑定和批次路径由程序产生；不生成宿主调用号、签章或连接 IP。

来源分类、内容层、是否相关、实体层级是研究判断，不由程序猜测。用户来源还需 `--promotion-status not_suspected/suspected/unknown`，没有判断不能默认为非推广。候选数组只含 `text/primary_dimension`，以及必要的 `unit_type/place_relevance`；text 必须为真实正文中可定位的原文，不含资格或评分字段。未准备候选时正文先保留，不把搜索摘要充当证据，也不自动猜主维度。

失败页面用 `page-failure --execution-id <编号> --url <URL> --error-type <真实原因>` 记录并继续其他公开结果；不绕过登录或访问限制。缺标题优先从真实 HTML title 提取；仍缺时保留原响应、再读实际元数据或换候选，不生成标题。`--kind snippet` 只能返回继续读页动作，不产生正式采集。

本查询实际页面处理完成后，调用 `complete-query --execution-id <编号>`。程序从保存的搜索和页面事实构建批次，执行原正式采集。`rebuild-batch` 可以从同一组事实重建损坏的派生 JSON，保留被替换的字节，不能修改原工具事实。然后 `advance → next`，自动执行下一动作/窗口，不询问用户是否继续。重复提交不重复增加记录。

命令返回的 `continuation` 为 `AUTO_CONTINUE/SOFT_RETRY/HARD_BLOCKER/USER_INPUT_REQUIRED/TURN_CHECKPOINT/COMPLETED`。格式与单项失败返回 `SOFT_RETRY`；所有合法结构化工作流结果均退出 0，宿主读取 continuation/action。非零表示输入、存储或程序失败。真实整体能力缺失、不可恢复状态才是硬阻塞；明确缺少用户信息或授权才需用户输入。TURN_CHECKPOINT 保存回合但研究未完成，恢复同一目录 `execute-next-action`，不承诺突破宿主限制。适配器和暂存助手见 `host-input-adapters.md`。

本次材料已确认的别名、代表节点和地方文化用语，可在 `init` 分别追加可重复的 `--alias`、`--building`、`--local-term`。这些只是既有查询生成器的原有参数，不增加另一套研究规则；本次上下文保存在运行目录，恢复时复用，同名上下文冲突拒绝。空间口径仍由本次材料确认并用于候选筛选与报告，不在 Skill 中写固定地点。

`advance` 自动调用准入、量化、刷新、审计、冻结、评分、报告真值编译和验收。它在需要外部事实或判断时返回：

- `search_and_read`：执行返回的真实查询，或继续这一批实际检索中的页面读取；不把计划当执行。
- `resolve_coding`：仅旧兼容适配器返回本地语义文件与待复核计数，不倾倒完整编号。只有真实可信人工决定才通过 `review --input <决定CSV> --trusted-review-key <既有外部密钥>` 应用；不具备条件的记录保持待复核，不能由模型确认。随后 `finalize-coding-without-review` 完成派生、刷新和审计；该命令不确认任何判断。默认动作入口自动执行此步骤，不返回普通复核等待。
- `write_evidence_grounded_narrative`：读取返回的冻结 `report_truth`，按 `assets/report-narrative-template.json` 填写定性字段，按限制表模板填写本次真实限制。保存到返回路径，再 `advance` 生成并验收。
- 旧 `blocked/invalid` 细节：仍保留实际错误，但用 `continuation` 区分局部自动修复与真正阻断；单条候选不能准入时隔离该条，不使其他合格候选丢失，不修改正式资格。合法审计缺口自动回到检索。
- `complete`：已经通过 `assert-final`；仅此时交付四份正式文件。

兼容适配器较小采集批次可先准入再继续同轮查询；默认顺序流水线完成整轮判断后统一提交。`resume-collection` 只允许尚未进入正式审计且未冻结的 `EVIDENCE_BUILD` 回到 `SEARCH`，不降低页面门槛，也不允许越过一次无效审计。一次入库不等于研究达标。
完整本轮计划（包括允许的重试）处理后才进入语义与审计。调度调用审计的 `--iteration-feedback` 接口：只将原审计中唯一的页面数量不足转为 `needs_iteration + collection_blockers`，其他错误原样保留；默认审计计算、门槛及正式准入不变，反馈不能授权冻结或评分。缺口计划仍由原发布规划器生成并执行原预算。
关闭查询前完成该次实际读页和候选判断；关闭后不能追加页面改写该执行的既定计数。重复事实可重放，新读页须属于后续真实未完成查询，不能伪造重试。部分工具正文使用 `--access-status partial`。最小文本接口采用 `page_body/official_fact/user_post/user_review/comment/reply`；分类仍须依据实际文本，不把新闻或官方材料改成用户内容。
单条无结果或不准入始终继续其他查询。仅当完整登记计划及允许重试全部真实执行完、仍没有任何可准入材料时，保留现有事实并返回 `USER_INPUT_REQUIRED` 请求确认研究上下文或公开入口；不能自动改固定计划、无限空转、宣布穷尽或评分。已采集但暂未判断的页面应在关闭查询前完成候选判断，不能把跳过判断当作检索完成。执行结束时间在首次关闭时保存，覆盖搜索及失败读页耗时，重放不改时间。

## 兼容适配器的低层批次结构（默认 Agent 不手工构造）

输入 UTF-8 JSON 恰含 `observations` 与 `pages` 两个数组。输入文件及完整工具正文属于本次受控私有工作材料，不交付给第三方。正式评分字段不可出现在候选输入中。

`observations` 每项沿用 `execution_schema.field_contract()` 的正式观察合同：

- 选择器 `plan_id`、`query_id` 来自 `next`；不得挂接到未执行查询。
- `execution_id` 使用 `next` 分配的本次尝试编号，重放不变。受控重试编号、原尝试链接和原因同样由已验证执行记录派生；瞬时失败以外的访问限制不重试，全部次数与预算仍由正式执行合同核验。
- 实际时间 `started_at`、`finished_at`、`retrieved_at` 都为带时区完整时间；不能用提交时间补造搜索时间。
- `search_tool` 采用发布枚举；状态、下一动作、返回数、已开页数、相关数、重复数、登录与限制布尔值以及重试信息均为本次事实。
- 所有查询文本、轮次、哈希、路由和派生计分数由已提交计划与正式写入器恢复。观察中不复制机器字段。

完整实时机器合同可运行 `python scripts/run_research.py contract --run-dir "<本次运行目录>"` 查看；观察合同直接读取既有 `execution_schema`，不自行猜测枚举。候选模板列出的机器标识按下文省略，由编排器恢复。

`pages` 每项恰含：

| 字段 | 内容 |
|---|---|
| `execution_id` | 对应已经实际完成的观察编号 |
| `record` | 页面实际元数据与工具 trace；见下文 |
| `response_text` | 工具实际返回的原始正文字符串；不使用研究摘要 |
| `candidate_source` | 来源分类判断，去掉模板中的机器标识 |
| `candidate_evidence` | 独立候选语义单元数组，去掉模板中的机器标识 |

`record` 沿用 `assets/source-capture-record-template.json` 的页面部分；省略 `task_run_id`、`query_id`、`query` 及所有执行/计划绑定字段，编排器从对应已提交观察恢复。初次输入不得包含 `raw_response_file`、`tool_artifact`、`capture_id`。实际 URL、最终 URL、标题、带时区时间、来源类型、内容层、访问状态、真实可见块及其位置必须与工具结果一致。`tool_trace` 的 schema、tool_name、URL、标题、时间、格式仍必需；`tool_call_id`、`result_ref` 不暴露时省略或留空，其余不可猜测。

`candidate_source` 沿用 `assets/candidate-source-template.jsonl`，省略 `schema_version/task_run_id/candidate_source_id/capture_id`；`candidate_evidence` 沿用 `assets/candidate-evidence-template.jsonl`，省略 `schema_version/task_run_id/candidate_evidence_id/candidate_source_id`。其余语义与来源字段保持原义；候选主维度是待准入的研究判断，不是正式评分资格。数值证据仍必须来自真实结构化工具响应，不得人工补星级。

编排器生成暂存编号与采集编号，并把实际响应送入 `source_capture.py capture --tool-result`，候选随后由 `pre_admission_audit.py` 全批检查。一次失败不自动提高资格或更改原文；禁止来源、证据、平台分与评分真值的手工写入。

## 最小能力实测

`probe --input <实际元数据JSON> --tool-result <实际正文JSON>` 验证已实测的页面内容能否真实登记与重读。正文 JSON 恰含 `response_text`；测试不创建正式证据，也不增加样本。搜索能力从本运行已经登记的真实执行观察取得。优先先提交首批真实观察，再完整探测；提前探测而尚无已登记观察时，搜索状态为未知，继续实际检索和登记，不把未知当作禁止首轮检索，也不凭模型宣称正式就绪。

没有内部调用 ID 不阻断普通工具读取：本地登记身份以任务、执行、URL、时间和正文哈希绑定，但不能声称这是宿主签章或不可伪造网络证明。A 级宿主签章仍是可选增强；失败签章不能降级。

## 幂等、恢复与责任

批次内容寻址存入 `staging/batches/<hash>/.private-response/` 并限权。同一批次重复提交时复查已登记采集哈希，复用稳定编号；已准入来源从受保护 canonical 清单确认，不重复提交。人工复核文件还必须绑定当前语义输入哈希；补检后不得用旧复核文件覆盖新证据，过期时应针对当前语义输入走已有可信复核路径。工作目录和内容冲突失败封闭，不删除其他批次。复制运行时必须复制整个同一运行，再由现有安全路径和哈希复验，不单独拷贝派生物后补台账。

正式数据仍只有既有追加台账和派生层；这些暂存文件不形成第二套 Evidence 系统。编排器不授予人工复核权限、不改变预算、不重分配缺分权重、不重算新的评分算法。程序返回等待输入时，宿主继续执行对应真实动作；没有后台进程时不得声称自动在后台研究。
# 审计恢复与退出码

正常工作流结果统一 exit 0；非零只代表程序、参数、存储或不可解析输入错误。审计 invalid 的下一步必须读取 repair_required 子状态，不能直接循环 advance。调用 `repair-audit` 仅允许依据已登记真实输入刷新派生计数或重新执行原有缺口审计接口；不修补正文、URL、证据 ID 或资格。修复前保存错误指纹、输入指纹和尝试次数，相同错误无数据进展时返回 HARD_BLOCKER。原始审计、冻结及评分门禁仍然有效。
