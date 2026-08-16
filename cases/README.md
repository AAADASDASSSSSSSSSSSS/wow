# 案例基准套件

这个目录是**度量基线**。每个 `.md` 文件是一份完整的自然语言硬件需求,原样作为用户提示词送入 `ratsnestpro-multi-agent`。

## 为什么需要它

同一个案例在不同轮次里,连接步产出过 26 个网络,也产出过 0 个。只用一两个提示词判断"改好了没有",无法把真实改进与运行方差区分开。所有设计侧改动都必须用这个套件对照基线,而不是看单次运行。

## 运行

```powershell
# 跑一遍全部案例
.\scripts\run_with_ericai.ps1 python scripts/run_case_suite.py

# 跑两遍,用于观察方差区间
.\scripts\run_with_ericai.ps1 python scripts/run_case_suite.py --repeat 2

# 与基线对照
python scripts/run_case_suite.py --compare data\ratsnestpro\suite\<基线>.json data\ratsnestpro\suite\<新>.json
```

报表写入 `data/ratsnestpro/suite/`,包含每次运行的完成步数、首个阻断步、ERC/DRC 错误数、修复轮次、`verdict` 分布,以及聚合后的均值与最小/最大值。**评分只读工具结果与文件系统检查,不读模型叙述**,所以报表无法被措辞美化。

## 添加案例

新增一个 `.md` 文件即可,文件名(不含扩展名)就是案例 ID。建议每个案例:

- 写明固定器件与不可替换约束,便于检验身份门禁;
- 写明验收条件(ERC/DRC、布线、产物),便于人工核对报表;
- 覆盖一个此前失败过的场景 —— 修好的 bug 应当留下一个案例。

`README.md` 不会被当作案例。

## 当前基线 (2026-08-03)

`data/ratsnestpro/suite/suite-20260803-093911.json` —— 2 案例 × 2 遍,EricAI 后端。

| 案例 | 步数 | 阻断点 | ERC | acks | 耗时 |
|---|---|---|---|---|---|
| `stm32f103-power-only-minimal` | 4/17 | `schematic_connections` | — | 1 | 876s / 1653s |
| `stm32f103-usb-sensor-node` | 11/17 | `layout_general` | 0 | 1 | 1015s / 883s |

`release_ready_rate` = 0,`completed_steps_mean` = 7.5,`repair_rounds_mean` = 0(AHE 层未介入;这两个都是步内 repair 用尽预算)。

**两遍结果完全一致** —— 同一案例的步数、阻断点、ERC 数一个都没变。这本身是结论:此前记录过「同一案例产出 26 nets 和 0 nets」的方差,现在至少在阻断点这个粒度上不再出现。后续任何改动都应对照这份基线,`--compare` 直接比 summary。

阻断原因(两条都可复现):

1. **`schematic_connections`:安装孔被选成了振荡器。** 选型步给 `MH1`–`MH4` 挑了 `Oscillator:Si512A_2.5x3.2mm`,一个 6 脚有源振荡器,然后 role 写 `mechanical`。7 条检查同时报,全部指向这一个错:`mechanical_part_not_electrical`(×4)、`selected_components_used`、`component_pins_accounted`、`power_pin_rail_class`。**管线行为正确** —— 这是模型的选型错误,检查如实拦下并给了可执行的指令("select a real mounting-hole symbol instead of relabelling an electrical device")。缺的是步内 repair 没能在预算内换掉它。
2. **`layout_general`:`crystal_near_mcu`。** `X1` 离 `U1` 太远,步内 repair 未收敛。归 `routing_congestion`。

顺带确认:`_normalize_duplicate_supply_pins` 在真实运行里生效并留痕 ——
`U1: VDD is on pins 24, 36, 48 and only some were wired; attached 24 to VCC_3V3 alongside its already-wired siblings`。这是 `auto_fixes` 第一条来自真实 run 的记录。

### 立基线过程中修的三处

1. **`scripts/run_with_ericai.ps1` 找错解释器。** 写死 `$root\.venv`,而 `ericai` 装在 `~\.venvs\rn-generality`。失败表现是 token 刷新里抛 `ModuleNotFoundError`,看着像认证问题。改成按顺序探测第一个能 `import ericai` 的解释器,`RATSNESTPRO_VENV` 可覆盖,CA bundle 随解释器走。
2. **claim 抽取跑在了注入证据上。** `RequirementsStep._arbitrate` 读 `state.requirement_text`,那段文本在运行中会被追加 architect 证据。结果:第一遍跑停在第 1 步,要求确认「`clock_external` = 36 MHz」,而这个 36 MHz 出自 `"...APB domain is 36 MHz. See Figure 2..."` —— 描述芯片内部总线的句子。用户被要求承担一个自己没提过的风险。改为只读 `_original_requirement()`(这个函数的 docstring 本来就写着 "Exclude downstream evidence from user-intent parsers",这里漏了用);ack 解析与 fact sheet 匹配仍读全文。回归在 `tests/test_factclaim_pipeline.py`。
3. **suite 跑不完就什么都不留。** 一个案例最长 27 分钟,报告原先只在干净退出时写一次。改成每跑完一个案例就落盘。同时加了自动风险确认:仲裁是 Tier 1 确定性的、不调模型,所以在发消息前就把 `ACK-RISK` 算出来拼进需求,一轮跑完。不用第二轮回答 —— 这个入口没有 checkpointer,ACK 会被当成新请求,原需求丢失,照样 0/17。每次运行记录 `acked_risks`。

### 未修:`supply_range` 误报(降压链)

`stm32f103-power-only-minimal` 每次都要 1 个 ACK,而那个风险不存在。需求写的是「USB-C 接口只用来取 5V 供电(不走数据),经 AMS1117-3.3 降到 3.3V」,`factclaim` 的 `logic_supply` 家族在 5V 旁边看到「供电」,就抽成 `supply_range=5`,判它超了 STM32 的 2–3.6 V。实际 5 V 是稳压器输入,MCU 拿的是 3.3 V。

`factclaim` 已经处理了料号数字掩蔽和中英文否定,缺的是**降压链**:同一句里出现「电压 A + 降压动作 + 电压 B」时,A 属 `vin_range`,B 才属 `supply_range`。已有的正面断言(`"给 STM32F103C8T6 供电 5V"` → `supply_range=5`)必须继续成立,所以判据要以「同句有降压动词且有第二个电压」为条件,不能靠给 `logic_supply` 加排除词。

没有一起改的理由:它是语义抽取判据,`test_factclaim.py` 有 31 条断言守着正反两侧,值得单独一轮做完并单独对照基线 —— 而基线现在有了,正好可以用来判断那次改动是变好还是变坏。

## 当前案例

| 案例 | 覆盖点 |
|---|---|
| `stm32f103-usb-sensor-node` | 固定 STM32F103C8T6 / LQFP-48、USB-C 供电与 FS 设备、SPI Flash、I²C 传感器、两层板;曾暴露"意图误判为审查""连接步覆盖率不足""修复越修越差" |
| `stm32f103-power-only-minimal` | 最小系统 + USB-C 仅供电、两层板;一次跑批同时暴露六个缺陷,见下 |

待补:SAME54 工业网关(RMII PHY、CAN-FD、microSD、0–10 V 模拟输入、四层板),用于覆盖符号缺失时的获取阶梯。

### `stm32f103-power-only-minimal` 的六个缺陷

来自 `data/ratsnestpro/runs/ratsnest-370639d2`。**案例文件本身只含需求正文** —— 套件把整份 `.md` 原样当提示词(`path.read_text()`),所以诊断结论必须写在这里而不是案例里,否则等于把答案泄进提示词。

已修复:

1. **`route_planes` 输入缺失导致跨器件族幻觉。** 该步的 user prompt 曾只有 `Ground net: GND` 加检索知识,不含实际网表与层数。检索命中 `esp32_pcb_layout.md` 后,它对这块两层 STM32 板输出了 `planes=['L1:Signal','L2:GND','L3:POWER','L4:Signal']` 和 11 个 ESP32 网络名(`RF_TX`/`ANTENNA`/`HSPI_CLK`/`UART0_TXD`/`VDD_SDIO` 等),本设计中一个都不存在。唯一的检查 `ground_plane_present` 因 `'GND' in 'L2:GND'` 成立而放行。
2. **`led_current_limit_in_series` 误报。** 候选电阻过滤器要求 role 含 `current` 或 `limit`,而 LLM 命名为 `led_series_resistor`,候选集为空导致必然失败。实际拓扑 `VDD33 → R4 → LED_SERIES → D1:A`、`D1:K → GND` 是正确串联。

已实现检查:

3. **VBAT 超压。** `U1:1`(VBAT,`power_in`)曾落在 `REG_IN` 网络上,而该网络同时含 `U2:VI`(AMS1117 输入,5V)。`data/fact_sheets/stm32f103.json` 的 `supply_range` 记录上限 3.6 V。已由 `factgate.supply_pin_conflicts` 实现,接在连接步 `supply_pin_not_on_regulator_input`。判据是拓扑的而非电压比较 —— 详见下面「电源引脚与稳压器输入同网」一节。
4. **晶振接错振荡器通道。** 8MHz 晶振曾接到 `U1:PC14`/`U1:PC15`,解析为 pin 3/4,其符号 alternate 是 `RCC_OSC32_IN` —— 32.768 kHz 的 LSE 通道。HSE 通道 pin 5/6(`RCC_OSC_IN`/`RCC_OSC_OUT`)全程未接。已由 `factgate.crystal_channel_conflicts` 实现,接在连接步 `crystal_on_rated_oscillator_channel`(`_crystal_channel_checks`)。通道从符号库的 alternate 名读,不看网名 —— 这个 run 里网名本来就叫 `HSE_OSC_IN`,按网名判会放行。归 `pin_conflict`:网对了,引脚错了。
5. **需求点名的 GPIO 未使用。** 需求明写状态 LED 接 PC13,但 PC13(pin 2)在网表中一次都没出现,LED 实为 3V3 经 R4 直连到地,常亮且不受 MCU 控制。已由 `_requested_pin_checks` 实现,检查名 `requested_pin_used:<pin>`。标识从 `RequirementSpec.constraints` 与 `raw_text` 同时正则抽取,再与符号库中该 MCU 真实存在的引脚名求交集 —— 抽错的会被符号库过滤掉,所以不依赖 LLM 是否把引脚复述进 constraints。语料上不适用(demo 板没有 `RequirementSpec`),这不是「零报错」。
6. **两脚元件短路。** `C10`(第 4 颗 VDD 去耦,`selection` 步已判为多余)两个端子都落在 `VDD33` 上。KiCad ERC 默认不覆盖此情形。已由 `_two_terminal_short_checks`(`orchestration/pipeline.py`)实现:引脚号取自符号定义而非 `role`/`value`,只在两端都接线时判定,两端同网即 ERROR。2026-08-03 在 KiCad demo 语料(35 个工程根图纸、18772 条 pin→net)上的误报基线是 **1 条**,且那一条是上游 demo 的原理图不完整,不是检查缺陷 —— 见下面「连通性真值源」一节。

C10 那条还有一个确定性补正兜着:`_normalize_bridged_capacitor_return` 把桥接电容的一个端子移到 GND,并写 `auto_fixes`。判据不读 `role`(「这是去耦电容」正是 C10 出事时说错的那句),只看「恰好两个已接引脚落在同一个非 GND 网上」。

### `families/atmega328.py` 的三个缺陷

不来自案例套件,来自 `tests/test_pipeline_e2e.py`,发现于 2026-07-31 统一 KiCad 路径口径之后。

在此之前 `config.symbol_dir()` 只读 `KICAD_SYMBOL_DIR`,而 `eda/symbols.py` 和 vendored 层会回退到自动发现。测试宿主没有导出该变量,于是 `component_pins_accounted` 和 `power_pin_rail_class` 这两条检查**整条被跳过**,e2e 因此长期是绿的 —— 不是因为设计正确,而是因为验证没有执行。`config.*` 现在与其他解析器口径一致,检查随即报出下面三条。

这三条是 **pipeline A 黄金参考板的真实电气缺陷**,不是误报。它们同时是 ATmega 侧的正样本来源:在此之前正样本只有 `ratsnest-370639d2` 一个,且只覆盖 STM32。

已修复(2026-08-03,`src/ratsnestpro/families/atmega328.py`):

1. **4 焊盘晶振的接地脚悬空。** 族声明 `Device:Crystal`(2 脚)配 `Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm`(4 焊盘)。`_normalize_symbol_for_footprint` 正确地换成 `Device:Crystal_GND24`,但 pad 3 与 pad 4(接地屏蔽脚)没有任何连接。**族现在直接声明 `Device:Crystal_GND24`**,`Y1:2`/`Y1:4` 进 GND。同时改掉一个更隐蔽的错:`Crystal_GND24` 的两个端子是 pin 1 和 pin **3**,而族原来把 XTAL2 接在 `Y1:2` —— 那是接地屏蔽焊盘,不是端子。靠归一化换符号只改了符号名,没改网表里的引脚号。
2. **USB-C B 侧与屏蔽悬空。** `J1`(`Connector:USB_C_Receptacle_PowerOnly_6P`)的 `B9(VBUS)`、`B12(GND)`、`SH(SHIELD)` 全未连接。**现已并联**:`A9`+`B9` 进 VBUS,`A12`+`B12`+`SH` 进 GND,`CC1`/`CC2` 改用 `A5`/`B5`。理由是 receptacle 对称,反插时原先悬空的那一排才是供电排;屏蔽不接地就是天线而不是屏蔽。
3. **未用 GPIO 既未连接也未标 no-connect。** `U2`(ATmega328P-A)的 pin 16、17、19、20、22–28。**现由 `MCU_UNUSED_PINS` + 参数没覆盖到的 GPIO 一起填进 `CircuitIR.no_connect_pins`**(该字段本轮新加在 `domain/contracts.py`)。区别在于「按决定不用」和「忘了」:参数会缩小映射集而不对剩下的引脚表态,而无标记的悬空引脚是 ERC 错误。

改黄金参考板会影响所有 pipeline A 产出,所以这三条是一起改、一起验的。

验证:临时移除 `tests/test_pipeline_e2e.py::_deterministic` 里对 `config._first_discovered` 的 stub,在装有 KiCad 的机器上跑 `pytest tests/test_pipeline_e2e.py` —— 现在应当是绿的,`component_pins_accounted` 与 `power_pin_rail_class` 会真跑并通过。该 stub 存在的理由是那个 fixture 自己声明的宿主无关性("reproducible regardless of whether KiCad ... is installed on the test host"),不是为了掩盖这三条。

### ~~生成的 `.kicad_pcb` 缺少顶层 net 声明~~ → 误诊,真因是解析器落后三个格式版本

**2026-08-03 更正。产出一直是合法的 KiCad 10,坏的是读它的解析器。** 原结论(下面保留)
把观察归因错了,并据此把 Freerouting `exit=1` 也归到了这里,那个归因同样不成立。

KiCad 在板格式 `20250907` 与 `20251101` 之间改了网表的记录方式:

| 格式 | 顶层 | pad |
|---|---|---|
| `20250907` 及更早 | `(net INDEX "NAME")` 声明表 | `(net INDEX "NAME")`,按索引引用 |
| `20251101` 起 | **没有声明表** | `(net "NAME")`,直接写网名 |

两种都是当前格式。KiCad 10 随装 demo 里两种都有:`CM5_MINIMA_3`(`20250513`)是旧的,
`pic_programmer`(`20260206`)是新的 —— 后者顶层 net 声明同样是 **0**,pad net 同样是
`(net "VCC")` 这种两 token 形式,和管线产出**完全一样**。

`PcbBoard.list_nets()` 只认旧格式,于是对 pcbnew 10 保存的**每一块**板返回空列表,而空列表
和"这块板真的没有网表"无法区分。修好之后:

| | 修前 | 修后 |
|---|---|---|
| `pic_programmer.kicad_pcb`(官方,新格式) | 0 | **111** |
| `CM5_MINIMA_3.kicad_pcb`(官方,旧格式) | 221 | 221 |
| `stm32f103c8t6-board.kicad_pcb`(管线产出) | 0 | **15** |

管线产出那 15 个网与该 run 原理图经 `kicad-cli` 导出的 15 个有名网**双向零差异**。
`.unrouted.kicad_pcb` 是管线自己的写入器出的(`generator=kicad-mcp-py`,格式 `20231120`),
顶层只有 `(net 0 "")` 而 pad 无 net —— 这也是对的:布线前还没有网,net 由布线 worker 通过
pcbnew 赋。布线后的文件 `generator=pcbnew`、`generator_version=10.0`,是 KiCad 自己写的。

回归防护在 `tests/test_pcb_net_layouts.py`:两种布局各有一组不需要 KiCad 的最小样本单测
(含"segment 的 `(net 1)` 是索引引用,不能被当成网名"这条,因为它和新格式的 pad net 同为两
token),外加一条扫描 —— 任何 pad 里有 net 却读出空网表的板都算失败。

**Freerouting `exit=1` 的根因因此仍未知。** DSN 导出由 pcbnew 自己做,它认得自己的格式;
`RoutingJob.setInput` → `FileInputStream.open` 失败要另找解释,不要再引用本节。

---

以下为 2026-07-31 的原始记录,保留以便追溯推理过程:

> 发现于 2026-07-31 建静态 fixture 集时。`data/ratsnestpro/runs/ratsnest-370639d2/stm32f103c8t6-board.kicad_pcb` 的 `PcbBoard.list_nets()` 返回 **0**,而同样解析器读 KiCad 官方 demo `CM5_MINIMA_3.kicad_pcb` 得到 221 个。
>
> 板文件正文里有 89 处 `(net ...)`,但全部在 `footprint` 的 pad 内部;顶层标签只有 `version / generator / generator_version / general / paper / layers / setup / footprint×28 / gr_line×4 / embedded_fonts`,**没有一条顶层 `(net N "name")` 声明**。KiCad 的板格式要求先在顶层声明网表,pad 再按索引引用。
>
> 这很可能就是那次 Freerouting `exit=1` 的根因:Specctra DSN 导出需要网表,栈顶是 `RoutingJob.setInput` → `FileInputStream.open` 失败,即 Freerouting 拿到的输入文件不可用 —— 不是缺二进制(本机 `freerouting.exe` 存在且 preflight 探测为 `discovered`)。
>
> 影响:板级检查(courtyard、线宽、DRC)在这类产物上拿不到网表,只有原理图级检查可用。

那条"KiCad 的板格式要求先在顶层声明网表"是错的 —— 只对 `20250907` 及更早成立。教训:
判断一个产出违反格式之前,先拿同版本的官方文件比一次,而不是拿手上恰好有的那个旧文件。

### 连通性真值源:从 SchematicGraph 换成 kicad-cli 网表

2026-08-03。所有读文件的连接检查此前都建立在 `_ConnectivityView.from_schematic`,而它走 vendored `connectivity.SchematicGraph`。那个图只 union 导线**端点**,引脚落在导线中段时不与之相连(模块文档自称引脚附着是 best-effort)。用 `kicad-cli sch export netlist` 在 KiCad 官方 demo `pic_programmer` 上对照:

| | KiCad 网表 | SchematicGraph 路径 |
|---|---|---|
| pin→net 条目 | 236 | 48(20%) |
| 网名一致 | — | 25/48(52%) |
| `two_terminal_not_shorted` 语料误报 | — | 34 |

错误是系统性错并网:KiCad 说 `R15:1` 在无名网 `Net-(R15-Pad1)`,那条路径说 `GND`。

现在 `from_schematic` 读 `kicad-cli sch export netlist --format kicadsexpr`(`eda/netlist.py`)。`pin_nets` / `parts` / `pins` / `no_connect` 全部来自同一次导出,所以引用集合不可能互相矛盾。三个连带结论:

- **层次工程必须从根图纸导出。** `SchematicDoc.load()` 只解析一张图纸,`pic_programmer` 的 `U5` `U6` `P2` `P3` `C6` `C7` 都在 `pic_sockets.kicad_sch` 上。原先"parts 仍从 `SchematicDoc.components()` 拿"的方案会让 `pin_nets` 指向 `parts` 里不存在的器件。
- **`no_connect` 现在可以填。** KiCad 把 no-connect 存为坐标标记,自己完成引脚归属,再以 node 的 `pintype` 后缀 `+no_connect` 报出来。实测 `pic_programmer` 两张图纸 77 个 `no_connect` 元素 = 77 个标记节点,两个集合相同。此前留空是因为坐标匹配错了会静默压制真实的悬空引脚发现;现在这个判断是 KiCad 做的,不是猜的。
- **代价:语料检查依赖 kicad-cli。** 导出不可用时抛 `NetlistError`,不退化成空视图 —— 空视图会让每条连接检查通过,读起来就是"这个设计没问题"。单个工程冷启约 2 s,全语料约 110 s,有会话级缓存。

验收:`pic_programmer` 的 236 条 pin→net 与 KiCad 逐条一致(`tests/test_connectivity_view_netlist.py`,真值由测试内独立的行扫描器提取,不复用被测解析器)。语料 35 个工程根图纸全部建出视图,`two_terminal_not_shorted` 误报 34 → 1。

剩下那 1 条:`royalblue54L_feather/nfc_antenna` 是单器件板,2 脚 FPC 连接器 `J1` 两脚都在 `/ANT`。天线线圈是 PCB 上的铜箔,原理图从未画它,所以就原理图自身的证据看,这个连接器确实被一个网跨接、回路缺失。判定为上游 demo 的原理图不完整而非检查缺陷,因此报出而不抑制 —— KiCad ERC 完全不看这一类。demo 若补上线圈符号,`test_corpus_has_one_known_two_terminal_short` 里的这条期望值就该删掉。

**尚未做**:`_ConnectivityView.build()`(管线自己的 `NetlistIntent` 路径)不受影响,它从声明的意图建视图,没有几何推断问题。

### 电源引脚与稳压器输入同网(2026-08-03)

抓的是上面第 3 条。`factgate.supply_pin_conflicts`,接在连接步,检查名
`supply_pin_not_on_regulator_input`,失败类 `erc_violation`(修法是改线,不是换料)。

**为什么是拓扑判据而不是电压比较。** 原来 `observe()` 用 `min(regulator_outputs)`
回答"MCU 看到多少伏" —— 那是从稳压器**料号**推出的标称输出,和 MCU 实际接在哪里无关。
对正确的板它给对答案,对错的板它沉默。

改成读那个网的电压也不行:`/REG_IN` 网名里没有电压,上游 `/VBUS` 只有在"USB VBUS 就是 5 V"
这个假设下才有 —— 而 USB-C PD 让这个假设不成立,且是往更危险的方向不成立。所以判据落在证据
真正在的地方:稳压器的输入侧按构造就不在稳压后的电压上,而受同一个 `supply_range` 约束的引脚
必须在同一个电压上。一个器件不可能跨在自己稳压器的两侧,无论输入到底是多少伏。

**三个条件,每个挡掉一类误报:**

1. 稳压器必须有且只有一个 `power_out` 网。buck 的符号根本没有 `power_out` 引脚 ——
   输出在电感之后,`SW` 的类别是 `output` —— 所以开关电源在这里不出裁定。这是正确的 fail-open,
   而不是去猜哪个引脚是输出。
2. 那个输出网上必须还有同一器件的另一个电源引脚,这才确立"这个稳压器给这个器件供电"。
   没有这条,器件碰到的任何无关轨都会被指控。
3. 排除**任何**稳压器输出所驱动的网。双电压域设计(3.3 V 逻辑 + 第二个稳压器出的 core 轨)
   里,3.3 V 网既是器件的电源又是 core 稳压器的输入,靠这条保持沉默。

两侧身份都来自 `resolve_sheets`,不看 `role`。地脚按符号的**引脚名**识别而不是网名:
LDO 的地脚电气类别就是 `power_in`,和它的供电输入一样,MCU 的 `VSS`/`VSSA` 也是,
不排除的话每块接了地的板都会被报。

**证据边界(重要)。** demo 语料对这条检查几乎没有覆盖力:35 个工程里只有 1 个
(`tiny_tapeout`)命中 17 份 fact sheet 中的任何一份,而它没有稳压器。所以语料上的零误报
说明不了什么,负样本证据全部来自 `tests/test_supply_pin_conflicts.py` 里构造的六种设计
(正确接线、地脚、双级供电、不给本器件供电的稳压器、buck、无 fact sheet)。
正样本是那个 run 的真实原理图,连通性走 kicad-cli,身份走 fact sheet,与管线里的执行路径相同。

`observe()` 的 `logic_supply_v` 保持不变 —— 选型步没有网表,只能从料号推,那里它仍是唯一
可读的东西。两者互补,不是替代;它的注释现在写明了这个局限。


### 地名白名单缺口:官方模板板上的真误报(2026-08-03)

`kicad-templates` 的官方项目模板 `BeagleBone-Black-Cape` 上,`power_pin_rail_class`
报 `U1:4(GND)->GNDD`。根因是 `_GROUND_NAME_TOKENS` 只有

```
GND  GROUND  VSS  AGND  DGND  PGND  EARTH
```

而这块板把数字地命名为 `GNDD`。整词匹配认不出来,`ground_nets` 是空集,于是板上每一个地脚
都被判成"没接到地网"。同样漏掉的有 `GNDA`、`GND1`、`GNDPWR`、`EGND`、`VSSA`、`VEE`。

**没有改成子串匹配。** 整词匹配的初衷是对的:`GNDSENSE` 是对地的测量,不是回流路径,
把它当地网会让检查放过一块地脚没接地的板。改法是扩精确白名单,再加一条"剥掉尾部数字后
若在白名单里也算地"(`GND1`/`VSS2` 与 `GND`/`VSS` 是同一类网)。新集合是旧集合的超集,
所以对已识别的网只增不减 —— demo 语料 33/35 个工程识别出 ground_nets。

`factgate._is_ground_pin_name` 用的是子串匹配,和这里不同,这是有意的:它判的是**符号引脚名**,
取值空间小且规范(库里地脚就叫 GND/VSS/AGND),而网名是设计者自由命名的。

### 外部语料接入:按证据强度分三层(2026-08-03)

语料在 `RATSNESTPRO_FIXTURE_HOME`(本机 `C:\Users\ewneiiy\kicad-fixtures`),911 条清单,
609 条可解析,来自 KiCad 自己的 `qa/data`、KiBot `board_samples`、`kicad-templates`。
访问层 `tests/fixtures/kicad_fixtures.py`,缺环境变量即整组 skip,不写死路径。

**`role` 的语义和字面相反,先看这条再写断言:**

| role | 含义 | 数量(原理图) |
|---|---|---|
| `positive` | 上游断言**这里有缺陷**(`asserted in test_erc_ground_pins.cpp`) | 39 |
| `negative` | 上游预期**干净**(文件名标记 `_ok`,或 CI 消费的可用输入) | 52 |
| `excluded` | 无上游断言也无文件名标记,角色未知 | 242 |

做误报基线要用 `negative`。7-31 记的"48 个带 KiCad 官方断言的正样本",按这个语义是**有缺陷**的样本。

**三层:**

1. `ground_pin_test_*` 五件套 —— 四个上游断言 `ERCE_GROUND_PIN_NOT_GROUND`,一个 `_ok` 配对。
   `power_pin_rail_class` 在 4/4 上独立命中(不读 KiCad 的 ERC 输出,只看地脚落在一个名字不表示
   地的网上),在 `_ok` 上沉默。**这是本仓目前唯一的外部有效性证据** —— 证明拓扑检查抓的是真缺陷,
   而不只是没误报。默认门禁就跑,约 6 s。
2. `role=negative` 的 33 个根图纸 —— 误报基线,502 器件、2006 条 pin→net,12 条 view 级检查全跑,
   零误报。挂 `real_kicad`。
3. 一条**记录测量**的测试:这批语料无法为 fact sheet 类检查作证。1988 个器件里 7 个命中
   17 份 fact sheet(0.35%),99 个目录里 **0 个**含带 sheet 的 LDO/DCDC,所以
   `supply_pin_conflicts` 这类需要两份 sheet 的判据在这里结构性地不可达。这条断言"regulator 数为 0",
   它开始失败就是好消息:语料有了真实设计,那时该把它们并入基线并删掉这条。

`role=excluded` 那 242 条不接:上游对它们没有任何声明,拿它们建基线等于什么都没证。

**排除清单。** 对方发现的 `qa/data/pcbnew/teardrop_offcenter_two_segment.kicad_pcb`
不需要这边再维护 —— manifest 自己已把它标成 `parse_ok=false`,理由 `trailing data after
top-level expression`,读清单时尊重 `parse_ok` 即可。这边额外排除两类,各带理由与"何时该删":

- `NOT_A_CIRCUIT`:KiBot 的 `off-grid.kicad_sch`(kicad_9 / kicad_10 两份)。只有一个电阻、
  两脚同网,是偏移网格的放置 fixture 而不是电路。对方的 `role_evidence` 自己写了
  `measured triggerable: two_pin`。`two_terminal_not_shorted` 报它是对的,只是这个文件
  不该当负样本。
- `CLI_REJECTED`:`issue24543` / `issue24544` 的原理图。`kicad-cli sch export netlist`
  自己 exit 3(加载失败);上游那两个案例断言的是板级 `DRCE_CREEPAGE`,原理图是附带的。

### 结构化失败信息、确定性补正与步内回灌(2026-08-03)

**`CheckResult` 加 `targets`,`failure_class` 做成属性。** 属性而不是字段:映射维护在
`check_classes.CHECK_FAILURE_CLASS`,每个实例存一份副本就会和它分歧。`None` 表示"未声明",
消费方必须读作"回退到推断",不能当成一个类。

`targets` 是检查自己声明它涉及哪些对象(ref / 网名 / `ref:pin`)。原来 diagnosis 侧从消息散文里
用 `\b([UJDRCLQYFK]\d{1,3})\b` 抽 —— 抽不到 `FB1`/`MH1`/`TP1`(两字母前缀),抽不到任何网名,
也分不清真引用和恰好长得像的 token。已填 targets 的检查:`two_terminal_not_shorted`、
`supply_pin_not_on_regulator_input`、`datasheet_limits`、`datasheet_connection`。

**payload 边界改了 B 侧一行。** `src/agents/ratsnestpro/tools.py:_pipeline_steps` 原来只取
`{name, message}`,现在多带 `failure_class` / `targets` / `auto_fixes`。7-31 的方案是"A 侧另写
一个 `pipeline_checks.json` 由 diagnosis 读",没有采纳:那会造出第二个真相源,而 payload 本来
就是既有通道,加字段是向后兼容的(`classify_failure` 对缺失字段回退到原来的正则)。

**`StepResult.auto_fixes` + `PipelineState.record_auto_fix`。** 被丢弃的修复轮不留痕:
`run()` 在每轮前记下长度,候选没赢就把这一轮的记录截断掉。不这样做的话,一个没进入产物的
补正会被 AHE 当成生效的改动。

**唯一一条确定性补正:`_normalize_bridged_capacitor_return`。** 两端落在同一个非地网上的
电容,把第二个端子移到 `ground_net`。无极性电容两端等价,所以"给一端回流路径"是唯一修法 ——
这是补正而非选择的判据。

三处刻意不读:
- 不读 `role`。"这是去耦电容"正是 `C10` 出事时那句错的声明。
- 不读符号库。`symbols.symbol_pins` 只按 `lib_id` 记忆,库不可用时的一次查询会把空结果缓存到
  进程结束 —— 那会让补正静默停止工作,而且看起来和"没有电容被短接"一模一样。改用自足判据:
  一个 ref 恰好两个已接引脚、都在一个网上,就是被跨接了,与它的符号有几个引脚无关。有一条
  测试专门 stub 掉 `symbol_pins` 返回空来守住这点。
- 不假设引脚号是 "1"/"2"。搬的是第二个 entry 自己的标识,`(net "A")`/`(net "K")` 也对。

跨接在**地**网上的电容不动:移一端到地什么也没改变,而它该跨哪条轨是设计决定。

**放弃了"同器件多电源引脚必须同网"。** `NetlistIntent` 用的是逻辑引脚名,同名引脚的多个实例
在那一层无法区分,强制同网会和 `no_double_assigned_pins` 的职责冲突 —— 它不是无自由度项。

**步内回灌加了按失败类的定向指令**(`check_classes.CLASS_REPAIR_DIRECTIVE`)。它不是
diagnosis 侧 `_STRATEGY` 的副本:那张表决定外层 AHE 允许做什么,这张是交给某一步里的模型的
散文。两者不能矛盾,所以策略为"不要修"的类(`constraint_violation` / `tool_unavailable` /
`harness_defect`)在这里也明说不要改,而不是索要一个帮不上忙的编辑。有一条测试断言
`CHECK_FAILURE_CLASS` 里出现的每个类都有指令。

`failure_score` 的单调下降保护与自适应轮次(预算 5,自适应到 `min(10, 预算+2)`)未改动。

### N4(晶振 HSE 通道)勘察结论:可做,数据齐全

`MCU_ST_STM32F1.kicad_sym` 里 `STM32F103C8Tx` 自己没有引脚定义,它 `extends
STM32F103C_8-B_Tx`,基符号有 119 处 alternate 声明:

| pin | 默认名 | alternate |
|---|---|---|
| 3 | PC14 | `RCC_OSC32_IN` |
| 4 | PC15 | `ADC1_EXTI15` / `ADC2_EXTI15` / `RCC_OSC32_OUT` |
| 5 | PD0 | `RCC_OSC_IN` |
| 6 | PD1 | `RCC_OSC_OUT` |

这证实了 7-31 的记录。判据可以是:晶振频率落在 MCU `clock_external` 区间(STM32 HSE 4-16 MHz)
时,它必须接在 alternate 含 `RCC_OSC_IN`/`RCC_OSC_OUT` 的引脚上。目标缺陷里 8 MHz 晶振接的是
pin 3/4(LSE 的 32.768 kHz 通道),网名却叫 `HSE_OSC_IN` —— 名字对、引脚错。

三个已确认的实现要点:
1. **必须跟 `extends`。** 不跟就看到 0 处 alternate,会误判成"这个符号没有 alternate 信息"。
2. **不需要实例级 alternate。** 目标缺陷里实例没有选 alternate(引脚用默认名 `PC14`),所需
   信息全在符号定义级。实例级 alternate 确实存在且可读(`(symbol ... (pin "G39" (alternate
   "CAN0_DIN")))`),但整个 demo 语料只有 4 处,都在 jetson 的 CAN 引脚上。
3. **`symbols.symbol_pins()` 返回的 dict 没有 alternate 字段**,要加。这是主要工作量。

我此前判断"N4 成本高于文档估计"是错的 —— 那次勘察没跟 `extends`,并误以为需要读实例级选择。
