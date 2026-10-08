# 优先复用成熟工具

核对日期：2026-10-08。框架优先使用现有 Perfetto、官方 Trace Processor、bootstat、getprop、logcat 和 Android 内存统计。自有代码只承担实验编排、身份绑定、有效性检查及对比，不另写跟踪协议解析器或功耗模型。

## 电池计量

电池电量计（Fuel Gauge）是硬件来源，Android Health HAL 提供读数，Perfetto `android.power` 负责采集。它们不是三种独立仪器，多个接口数值一致也不能当作独立校准。[Perfetto 官方说明](https://perfetto.dev/docs/data-sources/battery-counters)

亮屏负载优先采用 `configs/perfetto-power-memory.pbtxt`：电池每秒一次，内存每 5 秒一次，设备本地落盘，运行后一次拉取。这样可以复用原生采集器和统一时间线，避免周期 ADB 和 shell 子进程。时间戳分辨率不能提升电量计硬件的精度或更新率。

```sh
python3 -m abbench perfetto-export \
  --trace /path/to/trace.perfetto-trace \
  --processor /path/to/installed/trace_processor \
  --out runs/campaign/run-001/perfetto-export
```

导出器使用官方 `-q` 接口，保留工具版本、原始轨道、元数据、时间和值，不自行解码 protobuf。通用配置默认 900 秒，执行方需根据实际负载调整，保证完整覆盖；配置时长不是基准计算时长。时间轴、单位、供电状态、启动标识及计算窗口经过绑定后才进入积分。

本机快测发现 Root 输出文件不能用普通 `adb pull` 全部读取。框架提供 `pull-root`，复用 `adb exec-out`、`su 0` 和系统 `tar` 导出，不改变设备权限或安全策略：`python3 -m abbench pull-root --serial T11206HXH6600007 --remote-dir /data/local/tmp/ab-run --out runs/run-001/root-output`。输出目录必须是新目录，镜像切换后的记录不得覆盖旧启动数据。

本机 Perfetto 51.2 的短跟踪已经输出电池读数；官方 Trace Processor 58.2 能够导出。单位、符号、BOOTTIME 时间轴、同开机来源及采样覆盖通过核对后，USB 条件下仍可积分得到带符号的电池净功率与净能量实测值。这个边界称为 `battery_net`，不能解释成整机功耗；单位及来源核对也不代表传感器校准。

首次预检的满电 USB 状态中累计电荷保持 8000000 微安时；11:14 新镜像启动后短窗值为 7984000 微安时，也未随短负载变化。当前累计计量和分辨率未通过待机验证，端点不变化会被拒绝，不能把它写成零能耗。只读调查没有证实软件能够切断全部输入；命令存在、USB 在线标志、输入限流值或电池转为放电均不能单独证明完全电池供电。本框架不写未知输入控制节点。

熄屏待机仍使用经过验证的累计端点，窗口中停用周期跟踪。sysfs 脚本保留为缺少电压、计数器不支持或单位交叉读取时的降级通道，正式窗口只使用一个约定采集器。

重启前的 20 秒交叉读取发现，shell 一轮读取平均耗时 0.83 秒，实际读数平均间隔 1.97 秒，不能将 `sleep 1` 称作一秒采样。离线分析因此增加实际间隔、读取耗时和可选拒绝门槛。同期 Perfetto 每秒记录电池、每 5 秒记录内存；静置电压与 sysfs 读数一致，电流最大相差 3000 微安，最近时间匹配仍有约 0.49 秒偏移，不能把异步读数差异当作校准误差。二者来源相同，这次检查只验证单位、时间及传输链。

新镜像上的 25 秒验证再次成功导出 20 条轨道、240 条记录；内存总量与直接读取完全一致。此轮并行读取及短负载期间，shell 电流与 Perfetto 的异步最近匹配差值最高为 247000 微安，不能把先前静置下的小差值推广到负载瞬变。严格读取耗时门槛正确拒绝这些 shell 样本。随后仅运行 Perfetto 的 40 秒验证中，单线程负载使电池净电流从充电转向放电，说明读数对负载有响应；这仍不证明绝对准确度或整机供电边界。

## 电源轨和 PowerStats

设备电源轨监测（On-Device Power Rail Monitor，ODPM）需要真实硬件。PowerStats HAL 提供轨能量、能量消费者和状态驻留等接口，平台也可能只实现部分接口或示例服务。真实下游轨计量可以在插电时测覆盖部件的耗能，但局部轨、重叠轨或估算消费者不能自动相加成为整机能耗。[Android PowerStats 说明](https://source.android.com/docs/core/power/power-stats-hal)

**T11206 当前这些 PowerStats 通道必须排除。** 现场进程名为 `android.hardware.power.stats-service.example`，轨道为 Rail1/Display、Rail2/CPU、Rail3/Modem，消费者为 GPU/MODEM。10 秒跟踪里三条轨的值相同，每次查询均增加 1500 微瓦秒。这些元数据及行为与官方示例注册的 `FakeEnergyMeter`、`FakeEnergyConsumer` 和 `FakeStateResidencyDataProvider` 相吻合。[官方示例入口](https://android.googlesource.com/platform/hardware/interfaces/+/refs/heads/main/power/stats/aidl/default/main.cpp)、[模拟计量器](https://android.googlesource.com/platform/hardware/interfaces/+/refs/heads/main/power/stats/aidl/default/FakeEnergyMeter.h)

配置因此关闭轨、消费者分解及 PowerStats 状态驻留采集。它们不能说明真实 CPU/GPU 耗电或系统休眠。其他设备需先验证服务实现、硬件来源、覆盖范围和负载响应，再启用；注册成功、曲线变化或导出成功均不能单独通过真实性门槛。本次没有证明这台设备还存在另一套可用 ODPM。

## 启动事件

`device/snapshot-boot.sh` 只读 `bootstat -p`、`getprop` 和 `logcat -b events -d -v monotonic`，不写 bootstat、不清日志、不重启。系统现成记录负责阶段分析，主机单调计时负责端到端正常重启。

重启前记录为屏幕启用事件 27607 毫秒、启动动画完成事件 29069 毫秒、`boot_complete=29866`；但 `absolute_boot_time=29` 使用秒。11:14 新启动对应屏幕启用 25026 毫秒、动画完成 26449 毫秒、`boot_complete=27074`、`absolute_boot_time=27`。初始化原始属性还可能使用其他单位。不能统一假设 bootstat 所有字段的单位，必须保存原始值并结合本机事件校核。官方不同版本也存在秒/毫秒变化。[Android bootstat 实现](https://android.googlesource.com/platform/system/core/+/android16-qpr2-release/bootstat/bootstat.cpp)

本机引导程序总时长上报为零，因此 `absolute_boot_time` 不能证明包含完整引导及 XHyper 初始化。主项仍保留主机请求到新启动系统完成、桌面就绪的观测区间。持久记录可能属于旧安装或旧启动，要与当前启动标识及事件对应；系统完成、屏幕启用或动画完成均不单独替代桌面已绘制且可操作。

## 内存及休眠

活动内存曲线优先采用 Perfetto `linux.sys_stats` 的 meminfo 计数器；轻快照、zram 及功耗窗口外的 `dumpsys meminfo` 继续用于基线和归属。进程统计或堆采样仅用于必要的单列诊断，不默认加入正式功耗窗口。[官方系统统计配置](https://raw.githubusercontent.com/google/perfetto/main/protos/perfetto/config/sys_stats/sys_stats_config.proto)

休眠证据采用 `dumpsys suspend_control_internal` 中成功次数、总休眠时长和唤醒锁的窗口差值。本机能够读到这些字段，当前预检成功休眠次数为零，这不是正式待机样本。不能用已判为模拟数据的 PowerStats 驻留统计代替，也不能把处理器空闲当整机休眠。
