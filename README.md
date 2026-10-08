# kylin100-ab-bench

本仓库提供原生 Android 与搭载 XHyper 时的可复用上层对比测试框架，覆盖启动、Geekbench 6 中央处理器和图形处理器、运行能耗、亮屏静置、熄屏待机及内存占用。镜像负责方交付镜像和身份信息，测试框架负责协议、采集和离线分析。

已实现启动观察、Geekbench 界面操作和新结果绑定、设备本地 Perfetto 控制、静置及待机窗口、环境和内存采集、离线分析及中文报告。全套入口见 [自动执行说明](docs/AUTOMATION.md)。本机 CPU 自动运行及同步导出已验证，Vulkan GPU 和全套串行链路正在实机验证；正式 A/B 尚未执行。模式切换调用镜像负责方提供的适配器，本仓库不提供刷写命令。

## 先读这些文件

- [总体设计](docs/DESIGN.md)定义主要指标、供电边界、测试矩阵和有效性。
- [成熟工具选择](docs/MATURE-TOOLS.md)说明 Perfetto 优先方案、系统启动记录和本机模拟 PowerStats 通道的排除。
- [启动及性能方法](docs/benchmark-method.md)定义计时终点、完整结果导出和 GPU 覆盖范围。
- [功耗方法](docs/power-method.md)定义传感器验证、积分和低干扰待机测量。
- [内存方法](docs/memory-method.md)区分 Android 可见占用、压缩交换内存和虚拟化层保留内存。
- [数据合同](docs/DATA-CONTRACT.md)定义镜像、运行、采样和结果的格式。
- [2026-10-08 执行计划](docs/PLAN-20261008.md)安排 16:00 前的交付。
- [执行交接](docs/HANDOFF.md)列出镜像负责方和测试执行方的分工。

## 使用参考工具

工具仅依赖 Python 3.10 及以上版本的标准库。命令在仓库根目录执行。ADB 预检需要现有的 `adb` 和设备 Root，结果写入新目录。

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

所有差值统一为 `(XHyper - native) / native × 100%`。分数增加表示性能提高；时间、功率和能耗增加表示开销增加。报告保留每次运行、每次启动和所有失败原因，不把缺失填成零，不把连续电流采样当成独立实验。

电池侧整机功耗必须先验证设备完全由电池供电。USB 仍供电时，电池净能量只说明电池的充放电，不能作为整机功耗。短期内部计量能说明同设备、同条件下的相对差异；本框架不承诺未经校准的绝对准确度。
