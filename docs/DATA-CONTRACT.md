# 数据合同

格式版本为 1。所有 JSON 使用 UTF-8，单位写入字段名或显式字段，时间戳保留时钟来源。可缺失的数据使用 `null` 并附原因。所有运行数据存入版本库外的 `runs/`。

## 镜像清单

`examples/image-manifest.json` 定义两侧镜像身份。`validate-manifest` 只检查合同格式、共同内核及应用、正常配置确认和设备身份确认，不读取镜像或自动证明声明真实。执行方应保存独立哈希日志和读回证据，不能只把未验证的布尔值改成真。

`image_sha256` 是整个启动镜像的身份，两种模式应不同；`host_kernel_sha256` 是提取后的共同内核身份，两种模式必须相同。`kernel_runtime_id` 是启动后读取的内核构建标识或 notes 哈希，不能直接拿它与整个 Image 哈希比较。管理器及虚拟化层版本在 XHyper 清单中记录，Android 模块包、应用包及配置的额外哈希可以扩展。

## 单次运行

```json
{
  "schema_version": 1,
  "run_id": "campaign-phase1-boot1-cpu1",
  "mode": "native",
  "phase": 1,
  "boot_id": "device-current-boot-id",
  "image_sha256": "64位实际哈希",
  "workload": "geekbench6-cpu",
  "app_version": "6.7.1",
  "state": "prepared",
  "post_flash_boot": false,
  "compute_window": {
    "clock": "device_boottime",
    "start_s": null,
    "end_s": null,
    "start_uncertainty_s": null,
    "end_uncertainty_s": null,
    "source": null
  },
  "geekbench_document_id": null,
  "geekbench_uuid": null,
  "valid": false,
  "invalid_reason": "尚未运行"
}
```

事件文件每行包含 `run_id`、`boot_id`、`event`、`device_boottime_s`、`host_monotonic_s`、`host_utc` 和 `evidence_path`。设备与主机时钟不能直接相减。起止未知时保留空值，不根据应用上传时间推导计算完成。

## 启动记录

记录包含模式、镜像、旧与新启动标识、`post_flash_boot`、控制端请求起止、第一次连接成功、第一次系统完成和桌面就绪的观测时间。每个终点同时保留“上次未满足”和“首次满足”，从而形成轮询观测区间。桌面或启动事件能力缺失时，对应时间留为空值及原因。

## 采样 CSV

设备脚本输出以下表头。每一行保留读取开始和结束时间；各传感器未必同时更新。

```text
t_s,read_end_s,boot_id,voltage_uv,current_ua,charge_uah,external_online,battery_temp_decic
```

`t_s` 和 `read_end_s` 是同次开机、包含系统休眠的设备时间，单位为秒；脚本来源为 `/proc/uptime`。`current_ua` 保留原始符号。`charge_uah` 是累计或剩余电荷接口值，是否能作短时端点法须另证。缺失节点写 `NA`。

脚本的 `external_online` 直接来自指定节点。默认 USB `online` 可能只描述物理连接；它不能替代实际输入供电遥测。离线积分器对 `battery_side_device` 要求逐样本外部供电为假和整段供电已验证。原始物理连接标志为真时会保守拒绝这个边界。执行方若使用经过验证的输入暂停及遥测，需要另存原始属性并显式提供对应的实际供电状态，不得覆盖原始数据制造断电证据。

## 功耗配置与端点

配置见 `examples/power-config.json`。`discharge_sign` 是原始放电电流的符号，取 `-1` 或 `1`；计算器不使用绝对值。`max_gap_s` 限制可线性积分的间断，默认正式每秒采样时为 3 秒。空窗口表示使用完整采样覆盖；指定窗口需要已验证的计算边界，并且完全落在采样范围内。

`power_boundary` 支持 `battery_net` 和 `battery_side_device`。前者的能量为带符号净变化，正值表示电池放电、负值表示充电；它不能作为整机能耗。后者还需整段外部输入确实断开。计量器的实际单位、方向、刷新率和覆盖范围由执行前校准记录约束。

端点命令输入为 `{"start": {...}, "end": {...}, "config": {...}}`，两端均包含 `t_s`、`boot_id`、`voltage_uv`、`charge_uah`、`external_online`。配置需 `counter_validated=true`。可选 `counter_resolution_uah` 用于量化误差提示；零变化不输出零功耗。端点电压只支持平均电压近似，同时输出平均放电电流，不能把近似值写成精确功率。

积分输出保留 `valid`、`invalid_reason`、计量边界、方法、窗口、持续时间、焦耳、毫瓦时和平均瓦数。它并不验证应用工作量或环境匹配；执行方还须依据运行记录判定能否纳入正式对比。

## Geekbench 导入及统计行

导入器输出全部文档、原始 JSON、总分和原始 API 整数，同时保留版本、完整性及重复原因。所有 CPU/GPU 子项保留在 `document.sections` 中，不能仅提交摘出的总分。它检查 SQLite 完整性，但不能修复“写入过程中只复制主数据库”的不一致快照。

统计输入是 JSON 行数组。每行必需 `mode`、`metric`、`value`、`boot_id`、`run_id`、`valid`；失败行仍需保留可得身份及原因。`comparison_key` 保存共同 Android/内核、应用、负载和设置协议身份。工具还自动按版本、API、单位、APK 哈希、工作负载版本、配置和计量边界分组。

```json
[
  {
    "mode": "native", "metric": "cpu_single", "value": null,
    "boot_id": "实际启动标识", "run_id": "唯一运行编号",
    "valid": false, "exclusion_reasons": ["尚未测量"],
    "app_version": "6.7.1", "unit": "score",
    "comparison_key": {"protocol": "20261008-v1", "host_kernel_sha256": "共同内核哈希"}
  }
]
```

重复的同指标运行编号或 UUID 全部保留并排除，不能静默挑选一个。CPU 单核、多核可以来自同一运行，分别形成两行。GPU 接口映射由当前成功 JSON 和界面共同验证，解析器不猜 API 枚举。不同镜像哈希是预期差异，不放入共同分组键。

分析器不自动给出置信区间、热状态有效性或归因。每组报告全部值、有效/无效次数、独立开机数、均值、中位数、标准差、范围和统一方向的差值。失败原因和完整性问题随结果交付。

## 内存快照

`memory` 命令读取单独保存的 `/proc/meminfo` 文本和可选的单个 zram `mm_stat` 文本，不能将混有 `swaps` 或其他命令输出的全量 doctor 文件直接输入。轻量快照脚本将这些文件及读取起止时间分开保存。

解析器把 Linux `kB` 换算为 1024 字节，输出总量、可用量、估算不可立即获得内存、独立缓存及内核分项，并保留原始字段。`estimated_unavailable_bytes = MemTotal - MemAvailable` 是估计量，不是所有进程实际分配量。多个缓存、Slab、共享内存、交换和 zram 分项互有包含关系，不能再把它们加到估算占用上。

zram 前三列保留标准解释及 `schema_verified=false`，执行方应确认本机接口语义。`mm_stat` 的实际物理分配与交换逻辑使用量分列。三次同开机轻快照用于构成一个基线，中位数绑定对应 `boot_id` 后进入统计；不能把三次快照当作三次独立开机。

虚拟化层保留内存使用独立附加合同：物理范围、静态/动态类别、大小、来源及是否已经包含在其他类别中。没有范围及分配证据时，该项留为空值。Android 可见总量差只按“可见内存变化”报告。
