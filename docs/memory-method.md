# 内存占用对比方法

> 工具更新：活动曲线优先采用 Perfetto `linux.sys_stats`，轻快照、zram 和窗口外进程统计继续保留；细节见 [成熟工具选择](MATURE-TOOLS.md)。

日期：2026-10-08。状态：协议和纯离线分析器完成，设备内存对比尚未执行。

## 1. 指标及解释边界

本项比较同一设备在原生 Android 与搭载 XHyper 时，Android 内核可见内存、可立即获得内存、内核开销、缓存和压缩交换的差别。主指标是 `MemTotal`、`MemAvailable` 及二者差值；差值命名为“估计不可立即获得的内存”，不能命名为应用总占用或虚拟化层独占。

Linux `/proc/meminfo` 的 `kB` 按 1024 字节换算。报告保留原始文本、采集开始/结束时间、启动标识、运行编号和模式，汇总时统一使用字节或明确写出 MiB。1 GiB 等于 1024 MiB；因此 1 GiB 总量减 400 MiB 可用量是 624 MiB，而 1000 MiB 减 400 MiB 才是 600 MiB。报告不得混用十进制 GB/MB 和二进制 GiB/MiB。

| 指标 | 解释 |
|---|---|
| `MemTotal` | Android 内核可使用的物理内存总量；它不是设备标称内存容量。 |
| `MemAvailable` | 内核估计在不进行交换的情况下，可供新负载使用的内存。 |
| `MemTotal - MemAvailable` | 估计不可立即获得的内存；其中包含进程、内核及其他状态的共同影响。 |
| `MemAvailable / MemTotal` | 可用比例；两侧可见总量不同时，须同时报告绝对值。 |
| `Cached`、`Buffers`、`Shmem` | 独立报告缓存和共享内存字段。Cached 与共享内存可能重叠，不能直接相加。 |
| `Slab`、`SReclaimable`、`SUnreclaim` | 报告内核对象缓存及其组成。Slab 已包含后两个子项，不能再把它们相加。 |
| `KernelStack`、`PageTables` | 报告内核栈和页表字段；它们不能代表整个内核或所有页面管理成本。 |
| `CmaTotal`、`CmaFree` | 报告连续内存分配区域及其剩余量，不把 CmaFree 额外叠加到 MemAvailable。 |
| `SwapTotal`、`SwapFree`、两者差值 | 报告逻辑交换容量及用量，不把交换用量当作新增物理内存。 |
| zram `orig_data_size`、`compr_data_size`、`mem_used_total` | 分别报告逻辑数据、压缩数据和压缩设备实际用内存；第三项包含压缩数据及相关开销，不能再加第二项。 |

上述字段可能相互包含，框架不会把整张表相加形成“总占用”。框架也不会把 `MemTotal - MemFree` 当成应用占用。进程共享页不能按各进程驻留内存直接重复累计；`dumpsys meminfo` 的进程比例分摊集合也不能测出设备全部内存、硬件抽象层全部缓冲或 XHyper 在异常级别 2 的内存。

## 2. 固定状态采集

每次正式开机等待至少 300 秒，并按总体协议固定主屏幕、亮度、应用和后台服务。在主要静置状态下，以 10 秒间隔取得 3 次 `/proc/meminfo` 轻量快照；每次同时记录可读取的 zram `mm_stat`。快照中的窗口总长约 20 秒，三次用于观察短时波动，不能称为三个独立开机实验。

每个固定状态额外取得一次 `dumpsys meminfo`，保留完整输出。该命令可能唤醒进程并扫描内存，因此放在功耗测量窗口之外，不能每 5 秒运行。它用于理解进程及 Android 层的组成，主结果仍以轻量快照为准。执行方核对所读 zram 节点的设备名和模式；分析器解释的是标准 Linux 前三个字节字段，`schema_verified=False` 不得仅因解析成功而改成已验证。

CPU/GPU 工作负载开始前取得同样快照。工作负载结束后，至少等待 60 秒，再确认后台状态一致并取得恢复快照。60 秒是最低恢复等待，不保证后台清理、上传和温度已经稳定；结果页或上传进程仍活跃时继续等待并记录实际时间。网页、视频等日常负载的快照与相同场景、相同窗口位置绑定。

执行方不得运行 `drop_caches`、关闭 zram 或交换、杀掉不同的后台进程，或改变内存场景策略来制造“干净”数值。两侧都保留常态缓存和回收行为。需要追加清缓存实验时，它必须与主结果分列，并预先说明状态差别。

## 3. 活动期间的采样

如果要观察基准运行中的内存变化，可以在设备内每 5 秒读取一次 `/proc/meminfo` 和已验证的 zram 节点。采样器不执行 `dumpsys`、不触发回收、不进行页面映射扫描，双方采用相同频率和脚本。执行方先核对一次采集耗时及日志写入量；在主功耗采样中增加该能力前需要说明采样开销。

报告活动期间的 `MemAvailable` 最小采样值、估计不可立即获得内存的最大采样值、开始/结束及恢复后数值。名称必须包含“采样”：5 秒间隔会漏掉短时峰值，不能把最大采样值称为真实峰值，也不能用这些连续读数增加独立样本数。

## 4. Android 看不到的 XHyper 内存

镜像负责方提供双方宿主内核一致性证据，以及启动物理内存布局、保留区域和内存池的来源、大小与用途。XHyper、资源管理器或其他固件的内存可能在 Android 启动前已被保留，也可能由 Android 可见区域分配；仅凭 `/proc/meminfo` 不能判断所有权。

`native MemTotal - XHyper MemTotal` 只表示两侧 Android 可见内存总量的差。它可能包含 XHyper、资源管理器、固件、启动布局、设备缓冲或其他保留的共同影响，不能直接写成“XHyper 独占内存”。只有镜像负责方交付的布局及保留池证据能将其分解；没有证据时，报告把不可见区域留作“未分解”，不从应用内存或可用内存反推。

## 5. 分析器和结果绑定

调用方法是 `abbench.memory.summarize_snapshot(meminfo_text, zram_mm_stat_text=None)`。函数纯离线运行，输出字节字段、可用比例、原始字段、缺失可选字段列表和 zram 原始列。必需列缺失、重复键、错误单位、负值或可用量大于总量会抛出 `ValueError`；可选列缺失使用空值，不补零。

执行方将输出主指标绑定为通用统计行，例如：

```json
{
  "mode": "native",
  "metric": "memory.estimated_unavailable_bytes",
  "value": 629145600,
  "unit": "bytes",
  "boot_id": "本次开机标识",
  "run_id": "本次固定状态样本编号",
  "valid": true,
  "measurement_boundary": "android_kernel_visible_memory",
  "comparison_key": {"state": "home_settled", "snapshot_protocol": "three_reads_10s_v1"}
}
```

同一固定状态的 3 次快照先按开机汇总，同时保留原始值；跨模式统计以独立开机汇总行比较。mode、boot_id 和 run_id 由执行方绑定，分析器不会凭文件名或旧历史猜测本轮身份。运行中每 5 秒读数另存时间序列，不把它们逐行输入开机级显著性分析。

报告同时给出两侧可见总量、可用量及比例、估计不可立即获得内存、缓存和内核子项、交换与 zram、恢复后残留变化，并附独立开机数。任何应用失败、后台不匹配或无法读取节点都保留原因。没有 XHyper 物理布局证据时，只报告 Android 可见边界的结果。

字段定义参考 [Linux /proc 文件系统说明](https://www.kernel.org/doc/html/latest/filesystems/proc.html) 与 [Linux zram 文档](https://www.kernel.org/doc/html/latest/admin-guide/blockdev/zram.html)。这些标准说明不代替当前设备接口的现场核对。
