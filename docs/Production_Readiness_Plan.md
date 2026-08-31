# 生产化路线图与分工（RatsNestPro 多智能体硬件系统）

> 历史说明：本文中的实测数字和“17 步”表述记录的是加入 `component_prepare`
> 之前的基线。当前实现已在选型后增加独立的器件准备阶段，正式流水线为 18 步；
> 下文的基线数据仍保留，用于对比改进前后的效果。

## 1. 目标与当前差距

**目标**：把 `ratsnestpro-multi-agent` 做成可投产的企业级系统 —— 能稳定地把一份自然语言硬件需求做成通过全部确定性门禁的 KiCad 工程，并且可度量、可部署、可运维、可审计。

**当前最大的缺口不是某个功能，而是可重复的度量。** 现在用 1–2 个提示词判断好坏，而同一个案例不同轮次，连接步既可能产出 26 个网络也可能产出 0 个。在这种方差下，任何"改进"都无法与噪声区分。参考实现（`china-qijizhifeng/agentic-harness-engineering`）之所以能做十轮演化，前提是它有数据集与 pass rate。**这是本路线图的第一原则。**

## 2. 实测现状

以下均为真实运行结论，非估计。

| 维度 | 状态 | 证据 |
|---|---|---|
| 结构化意图路由 | 可用 | 45 项回归测试；SAME54 / STM32 长提示词均正确判为 `build` |
| 内循环自修复 | 部分可用 | 修复轮 1 将 ERC 74 → 46（判决 `MIXED`，命中 46/74）；修复轮 2 退化被判 `HARMFUL` 并回滚 |
| 完成一块板 | 从未达成 | 最好到 8/17；门禁配置修正后停在 4/17 |
| 确定性门禁 | 完整但需配置 | 同一网表回放：未设 `KICAD_SYMBOL_DIR` 时 8 项检查 0 阻断；设置后 26 项检查 8 阻断 |
| 反馈质量 | 部分 | ERC 报告已做对象级蒸馏；**步骤级 check 消息未蒸馏**，打开门禁后模型输出 0 nets |
| 诊断覆盖 | 不全 | `has_nets` / `has_supply_net` / `has_ground_net` 落入 `unknown` → 外层 `should_attempt_repair=False` |
| 符号接地率 | 不足 | 本案 45 个器件中 15 个符号解析失败（`Device:CP`、`Regulator:MIC5504-3.3`、`Device:PTC`） |
| 布线与制造（15–17 步） | 不可达 | 本机无 Java 与 Freerouting jar |
| 状态一致性 | 有缺陷 | 回滚只回滚内存状态，`pipeline_state.json` 仍是被否决的版本 |
| 凭据管理 | 不合规 | EricAI bearer token 明文写入 `.env` |
| CI / 类型 / 格式 | 缺失 | 无 CI 跑本项目用例；项目级 pyrefly 243 错（既有基线）；部分文件未过 `ruff format` |
| 可观测性 | 缺失 | 无循环级指标；LangFuse 钩子存在但未接 |
| 成本与时延 | 无约束 | 单次约 20 分钟；选型 200s+、连接 200–400s；无预算上限 |
| EHE 外环 | 未实现 | 代码中不存在 `EvolutionCandidate` / `RepairRecipe` / `CapabilityGap` 等 |

## 3. 分工总则

两条并行轨道，按**文件归属**划清边界，交汇处用冻结接口约束。

- **工程师 A —— 设计收敛**：让它真的做出一块板。
- **工程师 B —— 工程化**：让它能交付给企业用。

代码归属：

| 归属 | 路径 |
|---|---|
| A | `src/RatsNestPro-main/**`（嵌入引擎、pipeline 步骤与门禁）、`src/agents/ratsnestpro/evidence.py`、`src/agents/ratsnestpro/diagnosis.py` |
| B | `src/agents/ratsnestpro/tools.py`、`src/agents/ratsnestpro/ratsnestpro_agent.py`、`src/core/`、`src/service/`、`scripts/`、`docker/`、`.github/workflows/` |

## 4. 第 0 天：先冻结接口（已完成）

两条轨道在证据模块与状态字段处交汇，**第一天就定契约，之后任何一方不得单方面修改**。

冻结的接口不只写在文档里，而是由 `tests/agents/test_ratsnestpro_contracts.py` 强制执行：单方面修改会让 CI 失败，而不是悄悄破坏另一条轨道。修改契约需双方同意，并在同一个提交里更新该测试。

```python
# A 负责产出，B 负责持久化与度量
ViolationDigest.to_prompt(max_rules, max_objects, max_pins) -> str
ViolationDigest.error_signatures -> set[str]          # 形如 "rule:object"
compare_signatures(before, after) -> {"fixed", "introduced", "persisted"}
ChangeEvaluation.verdict ∈ {EFFECTIVE, PARTIALLY_EFFECTIVE, MIXED, INEFFECTIVE, HARMFUL}
```

```text
状态字段（B 拥有读写，A 只消费）
verification_digest, verification_signatures, change_evaluations, repair_patches
```

### 4.1 共同归属的契约模块

`src/agents/ratsnestpro/repair.py` 含 `RepairPatch`、`ChangeEvaluation`、`plan_repair`、`evaluate_change`，**两条轨道都要用**，因此不归任何一方单独所有：与冻结接口同等对待，修改需双方确认。

### 4.2 判决与保留策略（冻结）

| 判决 | 保留本次结果 | 允许再修一轮 |
|---|---|---|
| `EFFECTIVE` | 是 | 是 |
| `PARTIALLY_EFFECTIVE` | 是 | 是 |
| `MIXED` | 是 | 是 |
| `INEFFECTIVE` | 否 | 否 |
| `HARMFUL` | 否 | 否 |

### 4.3 运行资源约定

两人都要跑 20 分钟级别的真实运行，为避免互相阻塞：

- `run_name` 加前缀区分（如 `a-` / `b-`），避免 `_serialize_pipeline_run` 的运行目录锁竞争；基准套件已自动使用 `suite-<case>-<id>` 前缀；
- EricAI 若有并发或配额限制，批量基准运行需错峰；
- `.env` 属本地配置，不入库，各自维护。

### 4.4 度量口径先行

A 改动反馈内容会直接影响 B 报表里的 `verdict` 分布。因此 **A1 落地前后，B1 的基准必须各跑一次做对照**，否则无法区分分布变化来自设计改进还是口径调整。

## 5. 工程师 A 的任务（设计收敛）

### A1 步骤级失败消息蒸馏

- **问题**：`component_pins_accounted` 等门禁把 100+ 引脚逐个枚举回灌，步内自纠提示词膨胀，模型退化为输出空网表。
- **做法**：复用 `evidence.py` 的分层思路（聚合计数 → Top-N 对象与引脚 → 指向完整清单），对步骤级 `CheckResult.message` 同样处理。
- **验收**：连接步自纠不再产出 0 nets；单条反馈长度有硬上限；`tests` 覆盖"超长失败清单被压缩且仍含首要对象"。
- **依赖**：无（最高优先）。

### A2 诊断分类补全

- **问题**：连接性门禁的失败被判为 `unknown` → `record_capability_gap`，外层因此拒绝修复。
- **做法**：为 `has_nets`、`has_supply_net`、`has_ground_net`、`selected_components_used`、`component_pins_accounted`、`power_pin_rail_class`、`crystal_two_distinct_signal_nets`、`led_current_limit_in_series` 增加分类，归入连接性类，策略 `fix_connectivity`，作用域 `schematic_connections`。
- **验收**：上述消息全部命中非 `unknown` 分类；`should_attempt_repair=True`；修复在第 4 步发生而非漂到 ERC。
- **依赖**：无（与 A1 并列最高优先）。

### A3 提升符号接地率

- **问题**：选型步产出的符号有 1/3 在已装库中解析不到，这些器件的引脚不进入门禁统计，并在 ERC 阶段变成 `footprint_link_issues`。
- **做法**：在选型步的门禁里要求每个器件符号必须可解析（复用既有 `grounding` 能力），不可解析即当场阻断并重选。
- **验收**：选型产出 100% 可解析符号；`footprint_link_issues` 归零。
- **依赖**：A1（否则失败清单同样会膨胀）。

### A4a 重设计策略本体（A）

- **问题**：出现 `HARMFUL` 判决时当前只回滚并终止；参考实现的策略是"回滚**或重设计**"。
- **做法**：在 A 归属的文件内实现可选策略（例如换分解粒度、先补支持网络再连线），以策略枚举对外暴露。
- **验收**：策略可被单测独立触发并产生不同的提议结构。
- **依赖**：A1、A2。
- **注**：判决到策略的接线在 B 的 `ratsnestpro_agent.py`，见 B6。此项拆分是为了两人不改同一文件。

### A5 打通布线与制造

- **问题**：无 Java 与 Freerouting，15–17 步不可达，`RATSNESTPRO_REQUIRE_FREEROUTING=true` 下永远无法报成功。
- **做法**：接入固定版本 Freerouting（校验和固定），确保 DSN 导出、布线、SES 回导与零未连接检查全链路可跑。
- **验收**：产出真实 DSN / SES / Gerber，`unconnected=0`。
- **依赖**：B4（CI 与镜像内需备好 Java 与 jar）。

## 6. 工程师 B 的任务（工程化）

### B1 案例基准套件与自动评分

- **问题**：只有 1–2 个提示词，方差大到无法判断改进。
- **做法**：建立 5–10 块板的案例集（含 SAME54 工业网关、STM32 数据记录板），一条命令批量运行并输出报表：pass rate、完成步数、ERC/DRC 计数、修复轮次、`verdict` 分布、耗时与调用次数。
- **验收**：单命令产出可对比报表；同一提交连跑两次，报表能显示方差区间。
- **依赖**：第 0 天的接口冻结。**这是 A 所有改动的度量基础，必须最先可用。**
- **状态**：骨架已入库 —— `scripts/run_case_suite.py` 与 `cases/`（含 `--repeat` 观察方差、`--compare` 对照基线）。待补：更多案例与 CI 集成。

### B2 状态与产物一致性

- **问题**：回滚只回滚内存状态，磁盘检查点仍是被否决的版本，续跑会基于错误基线。
- **做法**：最优产物快照（参考 `best_ever.json` 思路）；回滚时同时恢复 `pipeline_state.json` 与产物目录。
- **验收**：构造一次 `HARMFUL` 回滚后，磁盘检查点与内存状态一致；续跑基于被采纳版本。
- **依赖**：无。

### B3 凭据治理

- **问题**：EricAI bearer token 明文写入 `.env`，且长跑会过期。
- **做法**：改用本地 OpenAI 兼容代理或 token provider，运行时取得凭据；`.env` 不落静态密钥。
- **验收**：超过单个 token 生命周期的长跑不再 `Unauthorized`；仓库与磁盘无静态密钥；密钥不出现在日志。
- **依赖**：无。

### B4 CI 与质量基线

- **问题**：无 CI 跑本项目用例；`ericai` 安装后 `test_required_fails_closed_when_no_client_available` 因环境依赖失败；pyrefly 243 错为既有基线。
- **做法**：把 `tests/agents` 与嵌入式套件接入 GitHub Actions；为依赖 `ericai` 缺失的测试加 marker；锁定 ruff / pyrefly 基线，只允许下降不允许上升；镜像内预置 Java 与 Freerouting。**分两阶段上线**：第 1 周只记录数值不阻断（避免 A 被自己的中间状态反复卡住），第 2 周起转为 PR 硬门禁。
- **验收**：PR 门禁可用；基线数值写入仓库并在 CI 校验。
- **依赖**：无。

### B6 重设计策略接线（B）

- **做法**：在 agent 循环里根据 `verdict` 选择 A4a 提供的策略，并把轮次上限放开到 3。
- **验收**：至少一个案例中 `HARMFUL` 后的下一轮取得 `PARTIALLY_EFFECTIVE` 或更好。
- **依赖**：A4a。

### B5 服务安全与可观测性

- **问题**：未审查 `/{agent_id}/invoke` 的认证暴露面；无循环级指标；无成本与时延预算。
- **做法**：给出认证与授权结论并落实；接 LangFuse；采集 `verdict` 分布、修复轮次、无效重试比例、单次 token 与耗时；加单次运行预算上限与超限熔断。
- **验收**：有明确认证结论；指标可查；超预算能被终止并如实报告。
- **依赖**：B1（指标口径与报表复用）。

## 7. 排期与同步点

| 阶段 | 工程师 A | 工程师 B | 同步动作 |
|---|---|---|---|
| 第 0 天（已完成） | 参与接口冻结 | 接口冻结 + B1 骨架 | 契约测试与案例套件已入库 |
| 第 1 周 | A1、A2 | B1、B2 | 周末合并跑基准，比对 `verdict` 分布 |
| 第 2 周 | A3、A4a | B3、B4、B6 | 基准回归，确认无退化；CI 转硬门禁 |
| 第 3 周 | A5 | B5 | 全链路跑通一块板 |

**同步节奏**：每次基准运行后共同评审 `verdict` 分布 —— 这是唯一同时反映"A 改得对不对"与"B 度量准不准"的信号。

## 8. 完成定义

以用户案例的验收条件为准，一块板视为完成需同时满足：

- 主 MCU 身份与封装正确，无替代品，无仅改显示值冒充；
- 所有器件具备真实符号与兼容封装，引脚号与焊盘号兼容；
- 无一引脚属于两个网络，无未解析逻辑引脚；
- 产出真实 `.kicad_sch` 与 `.kicad_pcb`；
- `kicad-cli` ERC error 为 0，DRC error 为 0；
- Freerouting 真实执行，产出 DSN 与 SES，SES 成功回导，`unconnected=0`；
- 输出 BOM、CPL、Gerber；
- Reviewer 对本次生成的工程完成独立审查。

采购数据库不可用、库存价格未验证、阻抗需板厂复核、非关键丝印建议 —— 记为 warning，不算失败。

## 9. 暂不启动 EHE 的理由

在 A1 未解决前，每轮修复仍可能退化。此时启动 EHE 归纳"经验"，会把错误配方固化成通用规则 —— 正是 `Intent_Routing_and_AHE_EHE.md` 第 5.2 节警告的错误记忆粒度。**待 A1–A4 让内循环稳定通过第 8 步之后再开 EHE 外环。**

## 10. 相关文档

- 架构方案：[`Intent_Routing_and_AHE_EHE.md`](Intent_Routing_and_AHE_EHE.md)
- 集成说明：[`RatsNestPro_Integration.md`](RatsNestPro_Integration.md)
- 参考实现：`china-qijizhifeng/agentic-harness-engineering`（其 "AHE" 指 harness 外环演化，对应本项目的 EHE）
