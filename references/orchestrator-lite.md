# 运行核心：顺序模块、自动继续

初始化前可运行 `python scripts/check_runtime_environment.py --workspace <输出父目录>`。按正式 `requirements.txt` 一次安装依赖；预检只用可清理临时目录，不创建任务。缺少私有 ACL 不阻止普通公开研究；真正机密输入才要求该能力。运行日志、动作与事务不使用私密目录。完整存储与初始化合同见 `storage-contract.md`。

本页与根入口的研究边界是首次启动所需上下文。阶段方法、完整工具合同和报告规则按根入口路由再读取。

1. `run_research.py init --run-dir <目录> --place <本次对象>` 建立任务并立即锁定动作模式，直接返回首个 action_id 和自包含合同；随后仅用 `execute-next-action` 提交或恢复。完整查询计划只生成一次，输出只含当前窗口。
2. 按 `references/batch-pipeline.md` 连续完成 DISCOVERY 的搜索登记批次，再 PAGE_CAPTURE 的真实页面批次；然后 NORMALIZE、TRIAGE、EVIDENCE_EXTRACTION、SEMANTIC_CODING。不逐页来回切模块，不在机械登记上进行语义推理。
3. 宿主仅执行返回的操作，将完整分派批次置于 `{"action_id":"<返回值>","records":[...]}`，以 `execute-next-action --input <文件>` 提交。程序接续规范化、写入、量化、刷新和审计。背景事实保留来源但不送入感知编码；未确认用户记录正式不计分，无人工输入也继续原审计。只有真实审计授权新补检，所有门槛不变。
4. `AUTO_CONTINUE`：立即继续。`SOFT_RETRY`：用已保存事实重建格式，或记录当前项失败并继续其他允许路径；不编造元数据、不绕过访问限制、不无限重复同一错误。
5. `HARD_BLOCKER`：真实的整体能力/存储故障或不可恢复状态，先保存可保存的检查点。`USER_INPUT_REQUIRED`：确需新的用户信息/授权，不能用来表示普通网页错误。
6. `COMPLETED`：仅在正式验收与最终封印均通过后交付。强制回合终止不是完成；再次获得执行机会用原目录 `execute-next-action` 恢复。
7. `TURN_CHECKPOINT`：调度软预算到达，研究仍 running；本回合收口，下次恢复原 action/cursor。自动唤起取决于宿主，不伪称后台无限执行。

输入适配、受保护写入及回合合同见 `host-input-adapters.md`。搜索数量/URL 由程序整理；本地抓取必须保留实际适配器身份；UGC 提供原文作者/用户范围依据。

宿主原生搜索、读页、浏览器、文件、Python、Office 工具属于本流程，不是“其他 Skill”。程序不代替宿主真实联网，不自动确认人工判断。

批量负载只读 `assets/batch-execution.json`；旧适配器窗口保留 `assets/runtime-dispatch.json`。原正式协议/检索配置仍是研究与评分唯一参数源。批次上限只切批，不截断研究；完整正文、历史响应和调试 JSON 不持续送回模型。
# 进程契约

合法工作流 JSON 的退出码统一为 0，含 SOFT_RETRY、HARD_BLOCKER 和 USER_INPUT_REQUIRED；是否继续取决于 continuation/action，而不是退出码。程序错误仍为非零。审计修复由动作入口受控调度，重复 invalid 不会无动作地 advance。不要直接调用底层模块或阅读源码猜参数；修正输入应依据返回的字段合同，不能改变结论。
