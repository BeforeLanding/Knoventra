# 回答支持度审核

这套工具回答两个不同的问题：当时模型实际拿到了哪些资料；审核者认为这些资料是否支持答案中的论断。它不自动判断语义支持关系，也不把模型审核算成人工审核。

## 1. 数据从哪里来

从本次代码开始，Agent 模式的 `run_eval.py` 为每例保存 `review_snapshot`：用户问题、最终答案、当轮完整证据注册表、实际展示的引用卡、错误码及 SHA-256。来源保留 chunk / 文档 ID、修订、页码、章节、字形和完整正文；拒答时实际来源卡为空，但仍保存当轮曾检索到的证据，供分析过度拒答。还保存线程 ID；通过标准 CLI 运行时，报告配置包含系统提示词的 SHA-256。

这些是评测文件中的离线副本，不是新建的对外查询接口，也不替代在线预览的重新鉴权。审核不连接数据库、不重新检索、不调用 LLM。校验值用于检测意外修改；它不是签名，也不能证明审核意见正确。

旧报告若没有 `review_snapshot`，会报 `no captured source snapshot`，不能用当前库重建旧来源。历史报告保持原样。新的真实 LLM 评测仍按开发方案先估算费用、确认后运行。2026-11-08 已在确认后完成一次 dev 20 例真实运行，并保留[原始报告](results/dev-agent-20261108.json)和[未标注审核包](results/dev-agent-20261108-review.json)。另有 10 个可答例的 Codex 审核，`reviewer_kind` 为 `agent`；真人审核尚未完成，详见[评测报告](../../docs/evaluation-report.md)第 15 节。

## 2. 生成审核包

先用 `run_eval.py --mode agent --split dev --case-id ops-01 --dry-run` 检查用例与调用估计。
填入实际输入 / 输出单价后可预览费用场景；真实运行还需显式 `--allow-llm`。
命令、token 限制和估算边界见[后端说明](../README.md)。预检 JSON 不是 Agent 报告，不能生成回答审核包。
真实评测使用专用种子库；转交用例会按预设决定恢复确认，可能提交转交记录，预检字段
`confirmation_decision_cases` 列出这类用例。调用模型前仍须按方案确认费用。

真实运行逐例写入 `<报告路径>.progress.jsonl`（可用 `--progress-out` 指定），保存开始时的线程 ID
以及完成后的原始回答 / 快照 / 诊断。中途失败保留已完成记录，成功写出正式报告后才记
`run_completed`。日志不自动恢复执行，也不能直接作为审核包输入；失败运行不发布汇总指标。
进程被强制终止可能没有结尾事件，最后一行也可能不完整。日志含完整资料正文，按报告同等保护。

### 离线检查运行日志

```bash
uv run python eval/inspect_progress.py eval/results/new-agent-run.json.progress.jsonl --out eval/results/new-agent-inspection.json
# 已有正式报告时，显式指定要核对的文件；工具不自动打开日志中的路径
uv run python eval/inspect_progress.py eval/results/new-agent-run.json.progress.jsonl --report eval/results/new-agent-run.json --out eval/results/new-agent-verified.json
```

输出列出已完成 ID、当前未完成用例及线程 ID、尚未开始 ID，以及 `completed` / `failed` /
`unclosed` 状态。`unclosed` 只说明没有完整终态，不证明进程仍在运行；报告已保存后再发生
输出错误时为 `completed_with_error`。未完成用例仍可能已计费或提交转交记录。

默认拒绝未以换行结束的末行。确认日志来自已停止的进程后，可加 `--allow-truncated-tail`
忽略该末行（包括截断的 UTF-8 字符），输出会明确标记；中间坏行不会跳过，终态后的截断也
不会默默接受。读取正在写入的文件只能得到某一时刻的前缀，不应用来判断运行是否已经结束。

检查会验证事件顺序、所选用例顺序、唯一线程、完成结果与证据快照；显式核对报告时还比较
预检、用例摘要、split、语料、逐例结果及重算指标。尚未记录到日志的配置字段无法由日志核实，
摘要也不是防篡改签名。检查结果不含回答质量指标，不是 Agent 报告或回答审核输入；不自动
恢复、重试或调用模型，也不连接数据库。输出独占创建，不覆盖原日志、报告或已有检查结果。

从 `backend/` 运行，输入应为新产生的 Agent 报告：

```bash
uv run python eval/review_answers.py prepare eval/results/new-agent-run.json --out eval/results/new-agent-review.json
```

默认选择报告全部用例；可重复传入 `--case-id` 选择部分用例。审核包记录选择的用例 ID，汇总同时显示原报告用例总数和实际选择数。所有输出使用独占创建，不覆盖原报告、已编辑的审核包或既有汇总。

审核包包含来源快照和建议审核单元。按行和中文句末标点划分，句末引用保留在对应句子中；标题、问候、建议等也可能被分成单元，由审核者判定是否是事实论断。当前不提供重新分句功能；一个单元包含多个论断时，需整体判断，部分有依据就标记 `partially_supported` 并在理由中区分。

### 离线浏览器审核页

不便直接编辑 JSON 时，可从**未标注**审核包生成一个单文件页面；页面只读取报告和审核包，不连接数据库或模型：

```bash
uv run python eval/render_review_html.py eval/results/new-agent-review.json \
  --report eval/results/new-agent-run.json --out eval/results/new-agent-review-human.html
```

打开 HTML 后逐句核对答案与可展开的当轮原文，填写审核者、判定、理由及逐字引句。可随时导出部分进度 JSON，之后用页面的“导入进度”继续；“完全 / 部分支持”的引句先在浏览器校验，最终仍须按第 4 节用原报告运行 `summarize`。页面不会自动标注，也不会把 Codex 审核转成人工审核。只有真人实际阅读和填写后才能使用 `reviewer_kind: human`。

旧 12 份语料真实 dev 运行的 10 个可答例已有[未标注浏览器审核页](results/dev-agent-20261108-review-qa-human.html)，共 50 个建议单元。它包含当轮冻结来源，页面本身不代表已经有人审核。新提示词复测若获授权并实际运行，须从新报告重新准备审核包，不能沿用旧快照。

## 3. 编辑审核意见

只修改每个 `claims` 项目的 `verdict`、`reviewer`、`reviewer_kind`、`notes` 和 `supports`，保留原文、引用、ID、来源和用例顺序。支持 / 部分支持必须提供原文引句，其他已审核项也必须注明审核者和理由。

示例（引句须从该审核包自己的来源中复制）：

```json
{
  "verdict": "supported",
  "reviewer": "reviewer-name",
  "reviewer_kind": "human",
  "notes": "句子中的断电要求与来源一致。",
  "supports": [{"alias": "S1", "quote": "断电后手动清理进料口"}]
}
```

- `unreviewed`：尚未审核，默认值；不进入支持率分母。
- `supported`：审核者认为该单元的事实论断全部有依据。
- `partially_supported`：部分论断有依据，部分未被支持。
- `unsupported`：没有充分依据或与资料冲突；在理由中说明，必要时附原文帮助定位。
- `not_a_claim`：标题、问候等非事实内容；不是用来跳过难以判断的事实论断。

真人实际阅读后用 `reviewer_kind: human`。Codex、其他模型或自动化标注用 `agent`。工具不会将 agent 结果合并到 human 指标。

引句必须逐字存在于捕获的“章节路径 + 正文”中。若答案有 `[S1]` 这样的引用，支持 / 部分支持所用引句必须来自该句实际展示的引用；不能用另一个未引用来源替错误引用背书。没有引用的句子可以依据当轮证据审核，但会另计为缺少引用。空引句、伪造来源和无法定位的引句均拒绝汇总。

## 4. 验证与汇总

```bash
uv run python eval/review_answers.py summarize eval/results/new-agent-review.json --report eval/results/new-agent-run.json --out eval/results/new-agent-review-summary.json
```

工具先与原报告重建的审核包核对，发现答案、引用、来源、审核单元、选择清单或报告发生漂移就报错。随后分别统计 human / agent 的已审核事实单元、完全 / 部分 / 不支持数量、非事实单元及缺少引用的事实单元。

`fully_supported_rate` 的分母是对应审核者类型已经标注的事实单元；没有已审核事实时为 `null`，不会显示 100%。`annotation_coverage` 是所选用例中已标注单元的比例，包含已经确认的非事实单元；不是“事实论断覆盖率”。引用支持度仍是审核者的语义判断，引句校验只证明引文存在。

汇总保留未审核数量、所选用例 ID、原报告和审核包的校验值。报告时同时给出审核范围、审核者类型和覆盖率，不能把少量已审核单元的支持率说成整个项目的回答质量。

## 验证范围

`tests/test_answer_review.py` 覆盖快照漂移、引用绑定、字形、分句、未审核分母、人工 / agent 分离、错误引句、审核归属、报告绑定及 CLI 不覆盖已有文件；`tests/test_eval.py` 使用真实 Agent 图、独立测试库、离线编码器与脚本模型验证快照可直接生成审核包。它们验证工作流，不是实际 LLM 回答的人工抽查结果。

## 5. 先定位拒答发生在哪一层

查询数值策略在新报告中记为 `config.query_vector_policy: float32-finite-v1`：查询按 float32
回环精度序列化，归零的稠密通道跳过；无有效通道返回空召回。候选的非有限距离不进入融合，
检索报告阶段字段可记录 `dense_unscorable` / `sparse_unscorable` 中的本人有权 chunk ID。
这些数值诊断不作为模型证据，不证明语义支持关系；其他有限通道仍可贡献同一块。历史报告
未标记时保持原样，不能据当前代码重建旧查询排序，也不能将旧效果数字直接当作新策略实测。

新 Agent 评测还保存每例 `expect` 和 `diagnostics`。诊断包含按调用顺序排列的检索查询、工具调用 ID、返回状态、闸门拒绝原因、所需与缺失编码、实际返回的 chunk ID，以及最终回答是否使用了简体 / 繁体拒答控制标记。这些字段只用于检查运行状态，不加入模型消息，不新增公开诊断接口。新用户轮次清空追踪；同一轮重试累积追踪，interrupt 恢复保留已有追踪。

新报告还保存可选的 `diagnostics.model_finish_reason`（最后一次模型响应的完成原因）。
`length` 对应 `MODEL_OUTPUT_TRUNCATED`，模型响应含无法解析的工具调用时对应
`MODEL_TOOL_CALL_INVALID`；二者终止本轮并清空展示引用，保留已有检索证据与诊断，不执行
该响应中的工具调用、不自动重试。在预期可答用例中它们归为 `operational_failure`，不算
“无依据拒答”或过度拒答。历史报告没有完成原因时不回填；供应商未提供标记的截断无法自动识别。

`MODEL_TOOL_CALL_INVALID` 也包括 JSON 已解析但参数不符合工具契约的响应：缺字段、错误类型、
空白 / 超长文本、额外字段、未知工具、空或重复调用 ID、单响应多次转交。整批执行前校验，
不发生“先执行合法调用、再发现另一个不合法”的部分执行；它仍归为运行故障，不算无依据拒答。

纯检索批次之后的正常回答若没有任何当轮有效引用，会以 `MODEL_CITATION_MISSING` 结束，
最终答案替换为固定错误文本、展示引用为空，原检索证据仍保存在快照中。可答例的该错误归为
`operational_failure`，不计无依据拒答。纯检索无证据且带拒答错误码时，最终输出固定拒答文本，
避免保存模型忽略闸门后生成的操作建议；明确拒答控制标记仍记录在诊断中。
转交 / 状态查询等工作流批次（含与检索混合的批次）及未检索对话不受这个引用要求约束。
一条有效引用不证明整段答案或每个论断均被支持，人工支持度复核仍须进行。旧报告不重写。
新 Agent 报告用 `config.citation_policy: retrieval-batch-v1` 标明该行为，便于区分历史结果口径。

在 `backend/` 执行：

```bash
uv run python eval/analyze_refusals.py eval/results/new-agent-run.json --out eval/results/new-agent-triage.json
```

这是离线命令，不连接数据库、不调用模型，输出文件使用独占创建。输入须为本轮代码生成的 Agent 报告；旧报告没有快照或诊断记录会直接拒绝，不能根据当前资料推断历史检索路径。答案、错误码、证据校验值和成功检索返回的 chunk 集合也会交叉校验；校验值不是签名，不能证明输入没有被有意修改。

分类口径：

- `retrieval_blocked`：做过检索但没有成功返回证据；区分 `IDENTIFIER_MISMATCH`、`LOW_SCORE` 和 `NO_ACCESS`，并保留多次重试。
- `model_refusal_with_target_span`：至少一次成功检索，模型主动拒答，且捕获证据中存在指定文档、修订、页码和 `expect.support_text` 原文片段。它是优先人工复核的候选，不直接计为“确定过度拒答”。
- `model_refusal_with_target_location_only`：只有文档 / 修订 / 页码命中，尚无目标片段命中；旧用例没有 `support_text` 时也进入此类。
- `model_refusal_without_target`：检索曾成功，但捕获证据没有命中目标位置。
- `refusal_without_search`、`refusal_after_successful_retrieval`：保留其他显式拒答路径，供人工检查。
- `operational_failure`：`RUN_LIMIT` 等运行错误单独计数。正常完成的回答按是否展示来源分组。

拒答率分母仅为 `ops` / `cross` 的预期可答用例，分子为明确携带 `NO_ACCESS` / `INSUFFICIENT_EVIDENCE` 的用例；运行错误保留在分母并另计，便于看出故障占比。`access`、`no_basis`、`escalation` 不进入该分母。分母为空时为 `null`。拒绝原因统计的是检索尝试次数，不是用例数；一个用例可以先失败、后成功。

没有控制标记且没有错误码的自然语言拒答不会被自动认定为拒答，仍需人工阅读。目标片段出现也不证明资料足以回答；人工复核必须结合完整问题、答案和当轮快照。可将候选 ID 传给 `review_answers.py prepare --case-id ...` 准备审核包，人工与 agent 标注继续分开统计。本轮只以脚本模型验证诊断链路，没有新增真实 LLM 评测结果或调整提示词 / 阈值。
