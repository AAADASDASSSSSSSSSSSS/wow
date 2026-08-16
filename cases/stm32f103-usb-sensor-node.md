请从零开始设计一块 USB-C 供电的 STM32 环境数据记录与调试板。

project_name:
stm32f103-usb-sensor-node

run_name:
stm32f103-usb-sensor-node-e2e

llm_mode:
required

这是一个新的 PCB 设计和构建任务，不存在需要审查的已有 KiCad 工程。

完成设计后需要由 Reviewer 审查，但任务的主意图是“新建设计”，不是“审查已有工程”。

禁止调用、复制、重命名或回退到任何已有离线 PCB 模板。只能复用通用工具、器件知识和经过验证的 Harness 能力。

一、预期多智能体流程

Supervisor
→ Architect
→ Parts Specialist
→ Hardware Engineer
→ Reviewer
→ 必要时 Hardware Engineer 修复
→ Reviewer 复审
→ Supervisor 汇总

要求：

1. Architect 负责资料检索、系统分解和关键设计依据。
2. Parts Specialist 负责器件型号、真实 KiCad 符号、封装和采购状态检查。
3. Hardware Engineer 负责生成实际原理图、PCB、布线和制造文件。
4. Reviewer 必须独立审查 Hardware Engineer 生成的实际工程。
5. 遇到可修复问题时，应先进行有限次数的结构化修复，不得第一次发现缺件或连接错误就直接结束任务。
6. 本地采购数据库不可用时，标记为“采购状态未验证”，但不要因此停止电路设计。

二、主控制器

固定使用：

STM32F103C8T6，LQFP-48 封装。

不得替换为其他 MCU。

最小系统包括：

- 8 MHz HSE 晶振；
- 32.768 kHz LSE 晶振；
- NRST 复位按键；
- BOOT0 配置电阻或跳线；
- 标准 10-pin Cortex SWD 接口；
- 所有 VDD、VSS、VDDA、VSSA 和 VBAT 引脚正确连接；
- 每个 MCU 电源组附近配置独立 100 nF 去耦；
- 合理的 3.3 V bulk capacitor；
- 晶振负载电容依据数据手册计算或选择；
- 不允许关键电源、复位、启动和时钟引脚悬空。

三、USB-C 与电源

USB-C 同时用于：

- 5 V 供电；
- USB 2.0 Full-Speed Device 数据通信。

要求：

- CC1、CC2 分别配置正确的 Rd；
- VBUS 具有保险丝或自恢复保险丝；
- VBUS 具有适合 5 V 输入的 TVS/ESD 防护；
- 5 V 通过 LDO 转换为 3.3 V；
- LDO 的输入电压、输出电流、压差和稳定性满足要求；
- LDO 输入、输出电容符合数据手册；
- 给出 MCU、传感器、Flash、LED 和接口的简单功耗预算；
- USB D+、D− 按 STM32 官方要求连接；
- D+、D− 各自具有合适的串联电阻；
- 如果该 MCU 需要外部 USB 上拉，必须按照官方要求实现；
- 使用真实的双通道 USB ESD 器件，或者两颗独立单通道器件；
- D+、D− 不得与电源或 GND 短接；
- Shield 接地策略需要说明；
- PCB 上把 D+、D− 作为差分对处理；
- 当前工具无法精确验证 90 Ω 阻抗时，标记为“需要板厂叠层复核”，不要因此单独阻断整个任务。

四、SPI Flash

增加一颗容量不低于 64 Mbit 的 SPI NOR Flash。

要求：

- 可以使用 W25Q64 或功能等效器件；
- 必须选择具有真实 KiCad 符号和兼容封装的型号；
- 正确连接 CS、SCK、MOSI 和 MISO；
- 必要的保持、写保护或上拉引脚不得悬空；
- Flash 具有独立去耦；
- Flash 靠近 MCU；
- 不得与 SWD、USB、晶振或其他外设产生 MCU 引脚冲突。

五、温湿度传感器与 I²C 扩展

增加一颗数字温湿度传感器。

要求：

- 具体型号由 Architect 和 Parts Specialist 自主选择；
- 优先使用 I²C；
- 必须具有真实 KiCad 符号和兼容封装；
- 根据数据手册配置电源、去耦和必要引脚；
- SDA、SCL 具有合理上拉；
- 增加一个外部 I²C 扩展接口，提供 3.3 V、GND、SCL、SDA；
- 检查传感器地址和总线上拉负载；
- 如果首选传感器在本地 KiCad 库中不存在，应寻找满足相同功能且证据充分的真实替代器件，而不是修改其他符号的显示值冒充。

六、调试和人机接口

包括：

- 10-pin Cortex SWD；
- 一个 3.3 V UART 调试接口，提供 TX、RX 和 GND；
- 3.3 V 电源状态 LED；
- 用户状态 LED；
- 用户按键；
- 复位按键。

要求：

- 所有 LED 必须有限流电阻；
- 用户按键输入必须有确定的默认电平；
- UART 电平必须与 MCU IO 兼容；
- SWD、UART、USB、SPI 和 I²C 不得产生 MCU 引脚冲突。

七、PCB要求

使用两层 PCB：

- 顶层：元件和主要信号；
- 底层：尽量保持连续 GND 覆铜和少量辅助信号。

板框最大：

60 mm × 45 mm

布局要求：

- USB-C、SWD、UART 和 I²C 接口靠近板边；
- USB ESD 和 VBUS 防护靠近 USB-C 连接器；
- MCU 去耦靠近对应电源引脚；
- HSE、LSE 及负载电容靠近 MCU；
- SPI Flash 靠近 MCU；
- 温湿度传感器远离 LDO 和明显发热器件；
- USB 差分对具有连续参考地，避免不必要的过孔和分支；
- 提供四个安装孔；
- 不允许元件重叠、越界或封装焊盘不匹配；
- 制造线宽、间距和过孔尺寸从系统制造能力配置读取。

八、资料检索要求

Architect 至少需要查找并引用：

- STM32F103C8T6 官方数据手册；
- STM32F1 官方硬件设计或应用指南；
- STM32 USB Full-Speed Device 相关官方设计要求；
- USB Type-C Sink/Device 的 CC 电阻要求；
- 所选 LDO 的数据手册；
- 所选 SPI Flash 的数据手册；
- 所选温湿度传感器的数据手册。

搜索失败时应尝试官方替代入口、制造商产品页或本地可信资料，不得用无依据的记忆填充关键引脚和电气参数。

九、成功验收条件

只有满足以下条件才能报告成功：

- MCU 确实为 STM32F103C8T6；
- MCU 使用真实 KiCad 符号和兼容 LQFP-48 封装；
- 工程中不存在其他 MCU 替代品；
- 不允许通过修改显示值冒充其他器件；
- 所有物理器件具有真实符号和兼容封装；
- 符号引脚号与封装焊盘号兼容；
- 不存在一个引脚属于两个不同网络；
- 不存在未解析逻辑引脚；
- MCU 电源、地、复位、启动、HSE 和 LSE 连接完整；
- USB、SWD、UART、SPI 和 I²C 不存在引脚冲突；
- 产生实际 KiCad schematic；
- 产生实际 KiCad PCB；
- KiCad ERC error 为 0；
- KiCad DRC error 为 0；
- Freerouting 真实执行；
- 产生实际 DSN 和 SES；
- SES 成功导回 PCB；
- unconnected 为 0；
- 输出 BOM、CPL 和 Gerber；
- Reviewer 对本次生成的工程完成独立审查。

以下情况可以作为 warning，而不是立即 blocked：

- 本地采购数据库不可用；
- 器件库存和价格尚未验证；
- USB 差分阻抗需要根据板厂叠层复核；
- 非关键丝印或布局优化建议。

如果出现缺少支持器件、引脚冲突、符号封装不兼容、未知引用或网络错误：

1. 输出结构化诊断；
2. 只修改相关器件、网络或布局；
3. 从受影响的最近 Checkpoint 继续；
4. 重新执行对应确定性检查；
5. 不得删除用户要求的功能；
6. 不得替换固定 MCU；
7. 不得降低 ERC、DRC 或连接性检查等级；
8. 只有达到有限修复轮次上限且仍无法通过时，才返回 blocked，并保留所有中间产物。
