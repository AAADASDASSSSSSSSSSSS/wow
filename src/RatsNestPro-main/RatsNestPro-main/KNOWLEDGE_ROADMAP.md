# RatsNestPro — 全面知识库总纲 (Comprehensive Knowledge Roadmap)

> 目标:把知识库从"一个板型(ATmega328 MCU 开发板)"扩成**通用 PCB 设计**的支撑。
> 本文件是**总纲/活文档**:列全需要的知识域、硬/软分类、来源要求、优先级、覆盖状态,
> 以及标注哪些"光补知识不够、必须同时升级引擎"的项。内容按此纲**分域、按真实来源**填充。

---

## 0. 一条不可违反的硬规则(全面化的前提)

**每个数值/事实必须可溯源**:`value · unit · scope · source_ref(URL) · page/table · access_date · confidence · status`。
- 抽不到来源 → `status: not_asserted / blocked`,**绝不写成静默默认值**。
- 禁止由 LLM 凭训练记忆生成数值/料号(这正是 `AP2112K-5.0` 幻觉、`100nF` 无 ATmega 官方源被抓出的原因)。
- 合法 status:`found / downloaded / extracted / distilled / blocked / not_asserted`。

**两层知识**(沿用项目现有约定):
- **harddata(硬)**:权威数值/料号/工艺,供**选型接地 + 底线校验(防烧板)**消费。必须权威来源。
- **corpus(软)**:设计经验/模式/取舍,按 `role:` 检索**注入 LLM 提示**。可引用少量带源关键数值,大表放 harddata。

**知识 ≠ 能力**:标 ⚙️ 的域,光加文档不生效,需**引擎升级**(约束驱动布线 / 真实验证),否则"能过检 ≠ 正确"。

---

## 1. 当前覆盖快照 (2026-08-03)

- **corpus 软知识**:16 篇,role 覆盖 `topology/selection/schematic/layout/routing/stackup/dfm/emc`(+reviewer/repair/architect)。内容偏 ATmega328/USB-C/AP2112。
- **harddata 硬数据**:`data/process_capability.json`(JLCPCB 保守默认)+ `data/fact_sheets/*.json`(17 个器件 × 37 slot 问卷,全部页码级引用)。staging 仍有未整合内容。
- **覆盖度自评**:器件事实层已成体系,但 roster 只有 5 个 MCU(ATmega328P / STM32F103 x8·xB / ESP32 / ESP32-C3 / RP2040)。横向铺器件类仍是主要缺口。
- **前置条件**:凡是「从符号库读引脚」的判决都以库可解析为前提,而这个前提曾经是静默的。`eda.symbols.symbol_roots()` 原先只在 `KICAD_SYMBOL_DIR` **未设**时才做发现回退,env 指向已搬走的目录时返回空列表,于是每一次引脚查询都返回 `None` —— 与「符号确实不存在」同一个信号。已与 `config.symbol_dir()` 统一为同一条规则(env 路径全部不存在时继续发现),回归在 `tests/test_symbols_adapter.py`。任何新增的库依赖检查都应假设这条规则,而不是自己再判一次 env。

### 1.1 硬事实的消费点(2026-07-31 起不再只是声明)

`SlotSpec.consumers` 里的名字曾经大半是设计目标。现在分三类,由测试而非文档保证:

| 消费方式 | 实现 | 覆盖 |
|---|---|---|
| **门禁**(判决 + 拦截) | `factgate.gate_findings` → `SelectionStep.datasheet_limits`、`SchConnectionsStep.datasheet_connection`,加 3 个 `cross_device_verdicts` | 12 个 `comparison != NONE` 的 slot |
| **跨器件拓扑判决**(2026-08-03 新增) | `factgate.supply_pin_conflicts` → `supply_pin_not_on_regulator_input`;`factgate.crystal_channel_conflicts` → `crystal_on_rated_oscillator_channel` | `supply_range` / `clock_external`。判据是**同网关系**与**符号 alternate 名**,不是数值比较 —— 前者曾按 `min(regulator_outputs)` 从料号推断而漏过 VBAT 落在 5 V 输入网上,后者的网名本来就叫 `HSE_OSC_IN`,按网名判会放行 |
| **提示注入**(设计前的参考) | `factbrief.brief` → `PipelineContext.fact_brief` → `TopologyStep` / `SelectionStep`(propose + repair)/ `SchConnectionsStep` 的独立 `Datasheet facts` 分块 | 22 个 `Comparison.NONE` 的数据 slot |
| **仍未接线** | `pins` / `clock_layout` / `thermal_pad` | 唯一消费者 `SchPinMapStep` / `LayoutCriticalStep` 的 `propose` 返回 `(artifact, False)`,不调模型,注入无效。登记在 `tests/test_factbrief_contract.py::UNBRIEFED_SLOTS`,附不可达原因。落地路径是确定性 `check` 或放置算法直接读事实,不是 prompt。 |

两条实测计数(`37 = 12 门禁 + 25 数据`,数据 slot 中 22 已注入、3 已登记):

- 12 个门禁 slot **也全部被注入**。此前一条手册限值只能通过「被违反」来被发现,每次都要烧掉一轮 repair;现在它在提案前就到场,门禁退回兜底位置。
- 22 / 25 个纯数据 slot 首次对产出有影响。它们无法门禁(`Comparison.NONE`),被读取是其唯一可能的作用方式。

强制这些声明为真的测试:

- `test_factgate.py::test_every_comparable_slot_has_an_observer_or_a_stated_reason` —— 每个可比较 slot 都能到达判决,否则须在 `UNOBSERVED_SLOTS` 登记原因。
- `test_factbrief_contract.py::test_every_data_only_slot_is_briefed_or_states_why_not` —— 每个纯数据 slot 都能到达某个 step 的提示,否则须在 `UNBRIEFED_SLOTS` 登记原因。
- `test_factbrief_contract.py::test_unbriefed_slots_are_only_routed_to_unwired_steps` —— 登记的理由必须为真,不能是借口。
- `test_factbrief_pipeline.py::test_briefed_steps_are_exactly_the_steps_that_override_the_hook` —— `factbrief.BRIEFED_STEPS` 不得与 pipeline 实际接线漂移。

### 1.2 表边界与器件匹配

- **匹配按标识符精确比对**,不再是子串包含。此前 `ESP32-S3` 归一化为 `esp32s3`、包含 `esp32`,于是拿经典 ESP32 的表作答:该表 `pin_count` 是 `FixedFact 48`(QFN48),S3 是 QFN56,结果产出一条带页码引用("ESP32 Series Datasheet v5.2 Figure 6-1/6-2, p.60")的自信错误 ERROR,拦死合法设计。现在每个受支持的订货码都在 `aliases` 里显式列出;未列出的解析为 `None`。矩阵测试:`tests/test_factsheet_matching.py`。
- **`FactSheetBase.excludes`** 让一张表显式拒绝邻近器件族(ESP32 表拒 `ESP32-S/C/H/P`;STM32F103 表拒其他密度段)。这是黑名单,按构造不完备;真正保证安全的是「未列出 = 不匹配」这个默认方向。
- **缺表不再静默**。`factgate.coverage_gaps` + `SelectionStep` 的 WARNING 级 `datasheet_coverage:<ref>` 会报告"该器件无手册数据,因此 N 个 burn 级检查根本没跑"。**保持 fail-open**:本项目开放世界选型,fail-closed 会拦死每个不在 roster 里的器件。

### 1.3 用户指定值的仲裁

`eda/factclaim.py`:需求文本里的数值先屏蔽料号与 ACK 行,再按子句里的器件类别路由到 slot,然后

- **Tier 1 硬冲突**:有 asserted 且可比较的 slot → `factsheet.evaluate` 判决。无 ack → `RequirementsStep` 出 ERROR 拦住,消息带页码与精确 `ACK-RISK:` token;有 ack → 降级 WARNING **但检查保留**,并进 `final_report` 的「已接受风险」段。
- **Tier 2 经验判定**:无手册可判时交 LLM + soft corpus,在经验范围内静默采纳(仍记录范围与 corpus 依据),超范围则追问。`advisory` **绝不产生 ERROR**——没有页码引用就没有拦板的资格。LLM 不可用一律 fail open(离线是受支持的运行方式)。
- Ack 按 `slot + 规范化数值` 精确作用域,改数值即失效;agent 层把自然语言确认交 LLM 转 token,但必须由确定性代码校验该 token 属于本轮真实待确认冲突,否则不予采纳。

---

## 2. 知识分域地图(全面清单)

> 标记:🟩 已有基础 / 🟨 薄 / 🟥 缺失(≈0) ; ⚙️ = 需引擎升级 ; 🔩 硬 / 📄 软

### A. 制造与工艺 (Fabrication & Process) — 🔩
- 🟨 A1 fab 工艺能力(线宽/间距/孔/环宽/铜厚/板厚):现仅 JLCPCB 保守默认;需**多 fab** + vendor-min profile(staging 有候选)。
- 🟥 A2 **叠层结构**(2/4/6/8 层、介质厚度、铜厚组合):staging blocked(JS/图片)。
- 🟥 A3 **受控阻抗叠层**(单端 50Ω / 差分 90·100Ω 的叠层+线宽/间距对应表)。
- 🟥 A4 **装配(SMT)能力**(最小间距、可贴封装范围、拼板、钢网)。
- 🟥 A5 材料属性(FR4 Dk/Df、高速板材、Tg)。

### B. 标准与合规 (Standards & Compliance) — 🔩(权威 PDF)
- 🟥 B1 **IPC-2221 / IPC-2152**(载流 vs 线宽/温升):现 R01 `blocked`,硬电流 gate 缺数据。
- 🟥 B2 **IPC-7351**(焊盘/land pattern 标准)。
- 🟥 B3 **IPC-2141 / 受控阻抗**(阻抗计算模型)。
- 🟥 B4 **安全 creepage/clearance**(按工作电压的爬电/电气间隙,IPC-2221 表)。
- 🟥 B5 接口规范:USB-IF(有 R2.5 CC 片段)、以太网、CAN、HDMI、PCIe——差分/端接/管脚。
- 🟥 B6 EMC/EMI 设计指引(辐射/敏感度、滤波、屏蔽);FCC/CE 布局含义。

### C. 器件类知识 (Component Classes) — 🔩参数 + 📄应用规则(广度核心)
> 每类需:典型型号族的**参数事实**(供电/引脚/极限/时钟)+ **应用设计规则**(去耦/外围/布局)。
- 🟨 C1 MCU/SoC:仅 ATmega328。缺 STM32 / ESP32 / RP2040 / nRF / i.MX 等族(供电引脚、boot strapping、时钟、复位)。
- 🟨 C2 线性电源 LDO:仅 AP2112。缺通用 LDO 选型(压差/PSRR/热)。
- 🟥 C3 **开关电源**(buck/boost/buck-boost/PMIC):拓扑、电感/电容选型、反馈、布局(热环路)。
- 🟥 C4 电池与充电(Li-ion 充电、保护、电量计、负载开关、理想二极管)。
- 🟥 C5 存储(SPI/QSPI Flash、EEPROM、SDRAM/DDR ⚙️、eMMC)。
- 🟥 C6 接口 PHY(USB FS/HS/3.0、以太网 MAC/PHY+变压器、CAN/RS-485 收发+端接)。
- 🟥 C7 模拟(运放、ADC/DAC、基准、传感器:温湿度/IMU/压力)。
- 🟥 C8 射频 ⚙️(天线、匹配网络、收发器、屏蔽罩)。
- 🟨 C9 连接器(有 USB-C/排针;缺板对板、FFC/FPC、电源、卡座)。
- 🟨 C10 分立(有二极管/电阻/电容/晶振;缺 MOSFET/BJT 驱动、电感、磁珠、TVS 系统化)。
- 🟥 C11 无源件选型规则(X7R vs C0G、电压降额、电阻功率/温漂、电感饱和电流)。

### D. 电路块 / 参考设计 (Circuit Blocks) — 📄 + 🔩
- 🟩 D1 MCU 基础块(去耦/复位/晶振/USB-C 供电):已有(ATmega scope)。
- 🟥 D2 各电源架构参考(单/多轨、时序、效率、软启动)。
- 🟥 D3 各接口参考块(USB PHY 布局、以太网 magnetics、CAN 端接、SD 卡)。
- 🟥 D4 混合信号分区(模拟/数字地、隔离)。
- 🟥 D5 时钟分配 / 抖动。

### E. 信号完整性 SI — 📄 + ⚙️(通用化硬骨头)
- 🟥 E1 受控阻抗(单端/差分)概念 + 与叠层(A3)联动。
- 🟥 E2 差分对(USB/以太网/LVDS/MIPI/PCIe):布线、等长、skew、对内/对间间距。
- 🟥 E3 长度/时延匹配(总线、DDR fly-by)。
- 🟥 E4 端接策略(串/并/戴维南)。
- 🟥 E5 参考平面连续性 / 回流路径 / 跨分割(有 vias_return_path 起步)。
- 🟥 E6 串扰、via stub / 背钻。
- ⚙️ **引擎依赖**:E1–E6 要真正落地,需**约束驱动布线 + SI 检查**(Freerouting 只做连通性)。

### F. 电源完整性 PI & 热 — 📄 + ⚙️(部分)
- 🟨 F1 去耦策略(有基础;需按频段/bulk vs 本地/PDN 目标阻抗完善)。
- 🟥 F2 平面电容、IR drop、平面/走线电流密度。
- 🟥 F3 热设计(功率器件铺铜、散热过孔、结温估算)⚙️(需热校验)。

### G. 布局与布线策略 (Placement & Routing) — 📄
- 🟩 G1 分区/关键件就近(有 layout corpus 基础)。
- 🟥 G2 通用摆放启发(连接器边缘、热区、RF 隔离、模拟/数字分离)。
- 🟥 G3 层分配策略、扇出(BGA/细间距)⚙️。

### H. DFM / DFA / DFT — 📄 + 🔩
- 🟨 H1 通用 DFM(有基础);需焊盘规范(IPC-7351,见 B2)。
- 🟥 H2 装配规则(器件间距/朝向/热焊盘/阻焊/钢网)。
- 🟥 H3 测试点 / fiducial / 工装孔 / 拼板。

### I. 真实料号与采购 (Catalog & Sourcing) — 🔩(硬基础设施)
- 🟨 I1 元件目录(MPN↔LCSC/Digikey↔封装↔库存↔datasheet):staging 有 11 个 verified,**项目无消费点**。
- 🟥 I2 参数化选型(跨目录按参数筛)。
- 🟥 I3 替代料 / 二供。

### J. 保护与鲁棒性 (Protection) — 📄 + 🔩
- 🟥 J1 ESD/TVS、反接保护、过流(保险丝/PTC)、浪涌、EN 上电。

### K. 机械 / 结构 / 连接器 — 📄
- 🟥 K1 板框/安装孔/keepout、结构约束、连接器机械对位、堆叠高度。

---

## 3. 需要的基础设施 (Infrastructure)

- 🟥 **INF1 硬事实消费层**(最高优先/地基):可检索、带来源页码的 harddata 入口,供**选型接地 + 底线校验**消费。不建这个,广度越大越易编。
- 🟨 INF2 corpus 扩展:递归子目录支持(现 `glob("*.md")` 非递归)+ 新 role 评估(现 8 个够不够)。
- 🟥 INF3 **provenance schema** 统一(每条硬事实:value/unit/scope/source_ref/page/access_date/confidence/status)。
- 🟥 INF4 **覆盖追踪器**(域 × status 矩阵,见 §7)+ 缺口登记。
- 🟥 INF5 采集流水线(下载→抽取→蒸馏→review→整合),沿用 staging 的 `collection_log` 方法。

---

## 4. 必须的引擎升级 (⚙️ 光补知识不够)

| 能力 | 现状 | 通用化需要 |
|---|---|---|
| 约束驱动布线 | Freerouting 只做连通性 | 阻抗/差分对/等长/层约束(E1–E6) |
| SI 验证 | 无 | 差分/阻抗/回流检查 |
| PI/热 验证 | 无 | PDN 阻抗、IR drop、结温(F2/F3) |
| 电气正确性 | 只查连通/短路/几何 | 功能级校验(超出 DRC) |

> 结论:SI/PI/热/高速接口这些域,**知识 + 引擎要同时上**;否则补了文档,板子仍"能过检 ≠ 对"。

---

## 5. 分阶段建设计划 (Tiers)

- **Tier 0 — 地基**:INF1 硬事实消费层 + INF3 provenance + INF5 采集法;把 staging 已核的料号/datasheet/policy 接进来。
- **Tier 1 — 广度(最高 ROI)**:C1 多 MCU 族、C2/C3 电源(含开关电源)、C6 常用接口、I 料号目录、J 保护。以"目标领域"推进(如 IoT 传感器节点)。
- **Tier 2 — 横切学科(需引擎)**:E 信号完整性、F 电源完整性/热——以**约束+检查**落地,非散文。
- **Tier 3 — 标准与高级**:B(IPC 全族)、A2/A3 叠层与受控阻抗、C8 射频、C5 DDR。

**推进原则**:**领域优先**(把一个领域的器件+块+料号+校验补全成样板),而非泛泛铺全部 PCB。

---

## 6. 采集方法(如何在不编造的前提下做到全面)

1. **来源等级**:官方 datasheet / 标准组织(IPC/USB-IF/JEDEC)/ fab 官方能力页 优先;二级来源标注。
2. **每条事实**按 §0 provenance schema 记录;抽不到 → `blocked/not_asserted`。
3. **产出形态**:软→`corpus/*.md`(带 `role:` 前言 + 来源);硬→`harddata/*.candidate.json`(带 source_ref)。
4. **review→整合**:候选先入 staging,人工/门禁核对后再进项目(沿用 `merge_review` 做法),**不覆盖 active 保守默认**。
5. **每次一小步**:清一个缺口、加一篇 corpus 或一张硬表,过 `test_corpus_roles` 等门禁。

---

## 7. 覆盖矩阵(活追踪器 · 随建设更新)

| 域 | 硬 | 软 | 状态 | 引擎 | 备注 |
|---|---|---|---|---|---|
| A 制造工艺 | ◑ | — | 🟨 | | 仅 JLCPCB 默认;缺多 fab/叠层 |
| B 标准合规 | ○ | — | 🟥 | | IPC 全族缺;R01 blocked |
| C 器件类 | ○ | ◑ | 🟨 | 部分⚙️ | 仅 ATmega/AP2112 |
| D 电路块 | ◑ | ◑ | 🟨 | | 仅 MCU 基础块 |
| E 信号完整性 | ○ | ○ | 🟥 | ⚙️ | 近零;需引擎 |
| F 电源完整性/热 | ○ | ◑ | 🟥 | ⚙️ | 仅去耦基础 |
| G 布局布线 | — | ◑ | 🟨 | 部分⚙️ | |
| H DFM/DFA/DFT | ○ | ◑ | 🟨 | | 缺 IPC-7351/装配 |
| I 料号采购 | ◑ | — | 🟨 | | staging 有 11 verified,无消费点 |
| J 保护 | ○ | ○ | 🟥 | | |
| K 机械结构 | ○ | ○ | 🟥 | | |
| INF 基础设施 | ○ | | 🟥 | | 硬事实消费层未建 |

> 图例:● 较全 / ◑ 部分 / ○ 缺失。随每次采集更新本表。

---

## 8. 下一步(建议)
1. **Tier 0 地基**:建 INF1 硬事实消费层 + 把 staging 已核资产接入(通用化第一块砖)。
2. 选一个 **Tier 1 目标领域** 做样板(端到端补全:器件+块+料号+校验)。
3. 按 §7 矩阵**持续采集**,每次一小步、可溯源、过门禁。
