# kylin100-ab-bench

本仓库提供原生 Android 与搭载 XHyper 时的可复用上层对比测试框架，覆盖启动、Geekbench 6 中央处理器和图形处理器、运行能耗、亮屏静置、熄屏待机及内存占用。镜像负责方交付镜像和身份信息，测试框架负责协议、采集和离线分析。

已实现启动观察、Geekbench 界面操作和新结果绑定、设备本地 Perfetto 控制、静置及待机窗口、环境和内存采集、离线分析及中文报告。`suite` 串行执行一个模式，`campaign` 调用外部切换适配器执行双侧活动；入口及参数见 [自动执行说明](docs/AUTOMATION.md)。2026-10-08 13:10 已完成一轮设备上的完整串行工具验证，185 项离线检查通过，详见 [实机验收记录](docs/VALIDATION-20261008.md)。正式双侧 A/B 尚未执行；GPU 覆盖计算性能，图形渲染及模拟日常使用当前没有自动覆盖。

2026-10-08 追加物理设备身份与连接地址分离、功耗窗口前后外部供电证据、待机端点供电资格及 Mac 操作说明。IP 地址形式的实机采集、导出和分析通过；此短测走 USB 转发，尚未验收真实拔线无线采集或 Mac 实跑。

2026-10-08 13:30 开始执行前，Claude Code 需要先交付 [正式镜像清单及模式切换适配器](docs/HANDOFF.md)。有限时间的一键执行入口使用 [quick-coverage 配置](examples/quick-coverage.json)，参数和用途见自动执行说明；它必须保存所选窗口、截止预算和缺项，不能把缩短窗口当作完整测试重复。镜像切换由交付方的适配器承担，本仓库不提供刷写实现。

## 先读这些文件

- [总体设计](docs/DESIGN.md)定义主要指标、供电边界、测试矩阵和有效性。
- [成熟工具选择](docs/MATURE-TOOLS.md)说明 Perfetto 优先方案、系统启动记录和本机模拟 PowerStats 通道的排除。
- [Mac 无线执行](docs/MAC-WIRELESS.md)说明官方工具安装、无线 ADB、实际拔线验证和启动测试的分工。
- [启动及性能方法](docs/benchmark-method.md)定义计时终点、完整结果导出和 GPU 覆盖范围。
- [功耗方法](docs/power-method.md)定义传感器验证、积分和低干扰待机测量。
- [内存方法](docs/memory-method.md)区分 Android 可见占用、压缩交换内存和虚拟化层保留内存。
- [数据合同](docs/DATA-CONTRACT.md)定义镜像、运行、采样和结果的格式。
- [2026-10-08 执行计划](docs/PLAN-20261008.md)安排 16:00 前的交付。
- [执行交接](docs/HANDOFF.md)列出镜像负责方和测试执行方的分工。

## 使用参考工具

工具仅依赖 Python 3.10 及以上版本的标准库。命令在仓库根目录执行。ADB 预检需要现有的 `adb` 和设备 Root，结果写入新目录。

正式双侧的一键入口使用执行方已经核实的材料，以下命令选择有限时间的覆盖配置：

```sh
python3 -m abbench campaign \
  --serial T11206HXH6600007 --profile profiles/t11206.json \
  --processor /path/to/installed/trace_processor \
  --manifest /path/to/formal-manifest.json \
  --adapter /path/to/mode-adapter.json \
  --options examples/quick-coverage.json \
  --out runs/formal-20261008-1330 --repetitions 1
```

示例路径必须替换为实际材料，输出目录必须是新的目录。`quick-coverage` 每侧安排 CPU/GPU 各一次、三次启动、三分钟亮屏静置及五分钟熄屏窗口，不能替代总体设计中的完整重复与待机窗口。其正式每侧保守预算约 77 分钟，双侧约 154 分钟，而 13:30 至 15:40 只有 130 分钟，因此仍可能因截止预算跳过第二侧；框架不会保证双侧已经完成，也不能调低预算绕过检查。执行者应按实跑耗时保留双侧覆盖和报告、清理余量。仅验证自动化时应显式使用 `--validation-only`；验证数据会被标记，不能进入正式 A/B。

```sh
python3 -m abbench --help
python3 -m abbench doctor --serial T11206HXH6600007 --out runs/preflight-001
python3 -m abbench validate-manifest examples/image-manifest.json
python3 -m abbench export-geekbench --db /path/to/consistent/history.db --out out/history.json
python3 -m abbench power --samples /path/to/samples.csv --config /path/to/power-config.json --out out/power.json
python3 -m abbench endpoint --input /path/to/endpoints.json --out out/standby.json
python3 -m abbench memory --meminfo /path/to/meminfo.txt --zram /path/to/zram-mm-stat.txt --out out/memory.json
python3 -m abbench perfetto-export --trace /path/to/trace.perfetto-trace --processor /path/to/trace_processor --out out/trace-export
python3 -m abbench pull-root --serial T11206HXH6600007 --remote-dir /data/local/tmp/ab-run --out runs/run-001/root-output
python3 -m abbench compare --input /path/to/metric-rows.json --out out/comparison.json
python3 -m unittest discover -s tests -v
```

示例镜像清单故意使用空哈希与未确认状态；验证失败说明它还不能用于正式计分。设备配置区分已经核对的单位及符号、尚未校准的传感器，以及尚未验证的累计计量和供电隔离。不能因为路径存在就确认计量能力。

亮屏负载优先采用 [Perfetto 配置](configs/perfetto-power-memory.pbtxt)和官方 Trace Processor。设备内 [采样脚本](device/sample-battery.sh)保留为缺少接口或交叉读取时的降级方案。熄屏待机应使用经验证的累计计量端点，避免周期采样唤醒设备。

设备内 [内存快照脚本](device/snapshot-memory.sh)保存三次轻量内存读数及对应设备时间。完整进程内存统计放在功耗窗口之外；它不能量出 Android 看不到的 XHyper 保留区。

## 结果解释

所有差值统一为 `(XHyper - native) / native × 100%`。分数增加表示性能提高；时间和设备边界能耗增加表示开销增加。带符号的电池净变化需要另外结合充放电方向解释，不能仅按百分比判断节能。报告保留每次运行、每次启动和所有失败原因，不把缺失填成零，不把连续电流采样当成独立实验。

USB 仍供电时，`android.power` 采集的电池电流和电压可以形成带符号的电池净功率、净能量实测结果；它们描述电池充放电，不能作为整机功耗或续航结论。负值表示净充电，不能取绝对值改称耗电。设备完全由电池供电的边界仍未证实，框架不会写未知输入控制节点。

满电短窗累计电荷不变化时，待机端点能耗会被拒绝，不能报告零瓦。本机示例 PowerStats 服务的模拟轨、消费者和驻留数据也被排除。报告保留这些缺项及原因；单位、符号和负载响应核对不等于传感器校准，也不能保证当前内部计量能够分辨很小的模式差异。
