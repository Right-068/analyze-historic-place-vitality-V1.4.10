# 宿主输入、受控写入与跨回合恢复

本层只整理真实工具响应并保护运行事实，不改变检索方法、阈值、语义规则或评分。

## 当前批次输入

先运行 `run_research.py execute-next-action`，以返回的 `action_id`、操作及完整合同为准。暂存助手从已登记动作取得执行编号、页面编号和分析指纹，模型不填写这些字段。

```text
python scripts/prepare_host_input.py normalize-search --run-dir <运行目录> --action-id <动作编号> --input <实际响应数组.json> --output <运行目录>/staging/helpers/search.json
python scripts/prepare_host_input.py normalize-page --run-dir <运行目录> --action-id <动作编号> --adapter host_web_fetch --input <实际页面响应数组.json> --output <运行目录>/staging/helpers/pages.json
python scripts/prepare_host_input.py build-triage-batch --run-dir <运行目录> --action-id <动作编号> --input <逐项判断数组.json> --output <运行目录>/staging/helpers/triage.json
```

`build-extraction-batch`、`build-coding-batch` 使用同样参数。每个输入数组与当前整批项目同序同数；失败项由入口另发动作，不自行删除或拆分。助手只写 `staging/helpers`；其结果仍需提交 `execute-next-action --input` 正式检查。临时自编程序只可整理此类暂存输入或读取诊断，不能写状态、计划、正式台账、审计、评分和 Office。

搜索响应支持 URL 字符串数组、结构化结果数组、`results/items/organic_results` 以及 `web.results/webPages.value`。程序计算原始条目数，保留重复条目；缺 URL 条目标 `provider_result_without_url`，不能充当页面。无结构响应可从保留文本提取 URL。可选 result_count 仅是比对断言，不是数量真值；禁止为通过合同捏造链接。

临时自编辅助脚本、调试文件和临时报表默认放在本次运行目录的 `staging/helpers/untrusted-work/`，不得放入正式数据或成果目录。该目录属于非正式暂存区，不被自动扫描为来源、证据或成果；只有显式提交的候选输入才经过正式入口检查。临时报表不能代替正式 Office，修改受保护路径仍在下一动作硬阻断。

## 真实适配器身份

发布版 `host_adapters.FETCH_ADAPTERS` 是身份注册表。宿主页面导入默认为 `host_web_fetch → host_page_read`，浏览器及供应商读取分别使用对应入口，均明确属于外部响应导入，不声称独立认证了外部工具执行。可选 call_id/result_ref 缺失不阻断 B 级。

本地 curl/requests 必须使用 `prepare_host_input.py fetch-page --adapter curl` 或 `--adapter requests`；输入数组仅匹配已分派页面数量，URL 取自动作。程序实际发起读取并保存已登记适配器收据、正文、最终 URL、时间及身份。导入文件不能自称 curl。未安装的适配器报真实能力缺失，不能改名为 web_fetch。所有访问继续遵守公开访问边界，不处理登录、验证码或访问绕过。

本地读取每跳重新检查实际域名解析结果，只连接已验证的公网地址，保留原域名的 TLS 证书与 SNI 校验，不使用环境代理或 curl 自定义配置。混合私网解析、地址不一致、TLS 或依赖错误如实报告；不要求普通宿主导入凭空补造连接 IP。

正常批次页面保存 `actual_fetch_method`；最终来源的 tool trace 保留由适配器生成的 tool_name。哈希可核对保留响应，不能证明外部宿主没有伪造数据。不得把本地篡改检测描述为操作系统权限隔离或密码学网络真实性保证。

## UGC 原文依据

用户内容层要求 `classification_basis`：`author_type` 为 individual/community_participant，`basis_excerpt` 为可见作者或参与依据，`user_content_excerpt` 为唯一、连续的用户内容原文范围。两段必须在保留正文唯一定位。查询含“评论/游记”不是作者依据；机构编辑攻略、新闻、公告和来源不明内容进入 context_only。

官方页面的真实用户留言可以单独取其用户范围；周围机构正文不随之变为评价。摘录必须在已确认用户范围内。未建立用户来源的页面不进入提取和编码，也不影响既有背景页面统计规则。

## 查询和写入门禁

每条可执行 Query 的 `query_dimension_targets` 必须为一至三个唯一合法维度。覆盖缺口不是维度；只能合入当前真正不足维度的既有查询，不增加无绑定查询。仅覆盖不足时输出诊断，不假造七维缺口。规划器在任何落盘前验证全计划；失败保留上一计划原样。

每次动作核对发布快照中的 approved_writer_registry、脚本哈希和角色/阶段；状态与日志绑定，操作检查点与正式产物纳入写入链。篡改返回 `protected_artifact_modified_outside_approved_writer`，包含期望/实际哈希和末次写入者。不得删除记录、更新摘要或改状态继续。操作写入先登记私有事务意图，恢复只重放已经进入写入链且内容完全匹配的意图；外部修改不能借恢复被采纳。

评分完成后发布脚本生成 `score-gate.json`，绑定任务、审计、冻结、证据、评分结果、协议和评分代码；needs_iteration 不许可评分。零实际计分证据仅可保留原规则授权的空分/不足状态，不得产生数字。报告/Office 入口重新验证此许可。

最终交付封印就是 `deliverable-manifest.json`，不是另一套成果真值。它绑定 DOCX、两份 XLSX、TXT、生成器、验收器、评分许可和冻结；运行只有通过原验收及此封印才能 DELIVER。完成后可独立只读复验：

```text
python scripts/validate_deliverables.py verify-delivery --state <运行状态.json> --writer-ledger <写入台账.jsonl> --manifest <deliverable-manifest.json>
```

## 回合和可达性

页面/判断待返回使用 ACTION_REQUIRED，不再使用 awaiting_input+AUTO_CONTINUE。真正需要用户的信息才用 USER_INPUT_REQUIRED。turn-state 保存回合、动作数、模块、当前动作和恢复指令；80 个成功动作或40分钟触发软 TURN_CHECKPOINT，研究仍 running，不创建成果。下一次同目录调用保留动作和游标，不重复已完成查询。此为调度预算，不截断研究查询或降低证据标准。跨回合自动唤起依赖宿主；缺少该能力时明确 unavailable。

`cost-metrics` 输出可达性诊断：已取得页面、用户页面、直接用户单元、候选合格数、转换率、追加页面估计及逐轮边际计分收益。零收益不作无穷精度估计；剩余预算未知则如实未知。machine_bound_exhaustion_forecast 只提供预测，不是正式穷尽认定，也不授权停止或评分。仍调用原逐维独立审计。
