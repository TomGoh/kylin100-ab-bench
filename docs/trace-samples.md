# Perfetto 电池计数器转换

本模块把官方 Trace Processor 导出的原始电池计数器转换为 `power.integrate` 所需的样本。转换器只检查数据结构、单位声明、时间及开机身份绑定。转换成功不等于电池计量已校准，也不等于设备整机功耗可测。

## 接口和输入证据

```python
normalize_battery_csv(
    csv_path,
    *,
    boot_id,
    source_verified=False,
    source_evidence=None,
    external_online=None,
    supply_evidence=None,
) -> dict
```

输入 CSV 必须包含 `ts_ns,track_id,name,type,unit,source_arg_set_id,dimension_arg_set_id,value` 这八列。转换器选取 `battery_counter` 类型的 `batt.current_ua`、`batt.voltage_uv` 和可选的 `batt.charge_uah`。它不会使用 `batt.power_mw`，也不会用电源轨或其他计数器补齐缺失电池值。

调用者必须提供非空的 `boot_id`，并在单位与时间轴核对后设置 `source_verified=True`。`source_evidence` 必须具有以下结构。

```json
{
  "boot_id": "与本次设备记录一致的启动标识",
  "csv_sha256": "当前原始 CSV 的完整 SHA-256",
  "clock": "CLOCK_BOOTTIME",
  "ts_unit": "ns",
  "units": {
    "batt.current_ua": "uA",
    "batt.voltage_uv": "uV",
    "batt.charge_uah": "uAh"
  },
  "reference": "记录单位检查、trace 时钟和开机绑定的证据文件"
}
```

`source_verified` 只表示调用者已经核对单位和时钟绑定，不表示传感器精度、累计量稳定性、负载响应或绝对准确度已经验证。轨道名称不能独自证明单位；CSV 的 `unit=[NULL]` 只有在上述显式证据成立时才能接受。转换器不猜测单位，不猜测时钟偏移，也不把不同开机的相近时间当作同一时间轴。输入 CSV 没有开机标识时，外部记录和文件哈希绑定负责关联；转换器无法独立发现调用者同时写错的两份开机声明。

`external_online` 默认保持未知值 `None`。该字段表示整个窗口内经过验证的实际外部供电状态，不表示 USB 线是否插入。调用者若提供布尔值，还必须提供 `supply_evidence`，其中 `boot_id`、`csv_sha256` 和 `reference` 与数据绑定，`external_online` 与参数相同，`state_verified=True`，并且 `covers_entire_window=True`。仅有 USB 的物理连接标志、一次充电状态读取或暂停充电命令的成功回显不能提供这种证据。

模块始终保留 `measurement_validated=False`、`counter_validated=False` 和 `input_supply_verified_off=False`。后续分析必须依据真实计量和供电证据决定结果范围，不能根据转换器返回 `valid=True` 自动将整机隔离标志改为真。

## 严格配对和来源保留

电流与电压必须拥有完全相同的原始纳秒时间戳集合，转换器不进行最近时间匹配、前向填充或插值。若存在电荷轨，它也必须具有相同的时间戳集合；若整个电荷轨缺失，输出的 `charge_uah` 保持为空值。

一个轨道标识可以在不同时间重复出现，但必须始终对应相同的轨道名称、类型、单位及来源参数。同一电池字段出现多个轨道标识、同一轨道在同一时刻出现重复点、倒序时间、非有限数值、零或负电压、负电荷、缺失必需字段和交叉开机绑定都会被拒绝。不同原始纳秒时间在转换为浮点秒后发生合并时，模块也会拒绝结果。

输出样本包含 `t_s`、`boot_id`、`current_ua`、`voltage_uv`、`charge_uah` 和 `external_online`，因此可以直接传给 `power.integrate`。每个样本同时保留精确的 `raw_ts_ns`、原始 CSV 哈希、来源轨道标识、声明单位和原始行号。结果还保留 CSV 的路径、大小、哈希、全部轨道元数据、总轨道数和选取轨道数。失败结果保留稳定的原因代码，样本数组保持为空，工具不会补成零值。

共同的导出时间戳仅证明这些数值属于同一跟踪批次，不能证明计量芯片或底层读取操作完全同时发生。模块不伪造读取结束时间或零读取耗时。只有一个完整批次时，转换可以成功；积分器仍会因样本不足而拒绝能耗计算。时间间隔门槛和任务窗口裁切继续由积分器负责。

## 离线验证和真实记录

执行以下命令可以运行转换器的阳性验证与必须拒绝输入检查。

```sh
python3 -m unittest discover -s tests -p test_trace_samples.py -v
```

已知阳性输入使用 4,000,000 微伏和放电方向的 -500,000 微安，两个批次相隔 10 秒。转换后采用 `discharge_sign=-1` 和 `battery_net` 边界，积分应当返回 20 焦耳和 2 瓦。未知供电状态下将相同样本用于 `battery_side_device` 时，积分必须拒绝。

2026-10-08 的离线验证读取了主工作树中的 `runs/new-image-20261008/trace-export/counter-samples.csv`。该原始文件的哈希为 `21a4aa9b8bf19264597a740c9d8ea99a5a00f2e6c4411fcb844961263c2a650e`，文件大小为 19,662 字节。该文件包含 240 行和 20 条轨道，其中六条电池轨道在每个批次的时间戳完全一致。转换器选取电荷轨道 0、电流轨道 2 和电压轨道 4，输出 26 个完整样本，时间范围为开机后 454.220234982 至 479.001286964 秒。

这次单位和时间绑定依据如下。

1. 同次采集的 `root-output/boot/boot-id.txt` 和前后身份记录都指向 `953a8a32-df97-491c-81b1-327447eae8dc`。文件哈希把这一声明限定在上述原始 CSV 上。
2. 使用同一导出器记录的官方 Trace Processor 58.2 读取原始 trace 的 `clock_snapshot` 表，`clock_id=6`、`clock_name=BOOTTIME` 的两条快照分别具有 `ts=clock_value=454163560059` 和 `454163624097`。同时保存的 `/proc/uptime` 记录处于相同的开机时间范围，因此本次转换绑定为设备开机时钟，纳秒值除以十亿后形成秒值。
3. 原始轨道命名、`preflight/power-supply.txt` 的标准电压、电流、电荷属性，以及 `crosscheck.json` 中同期读取的量级与传输检查支持微伏、微安和微安时的声明。USB 条件下电流快速变化且两种采集存在时间偏移，因此交叉读数差不能当作校准误差。

离线转换成功后，供电状态仍保持未知，计量验证和输入隔离标志仍保持为假。26 个样本的电荷值全部为 7,984,000 微安时，不能把这个短窗口的零电荷变化报告成零功耗。该次记录处于 USB 供电和接近满电的状态；它只验证采集和转换链，不属于正式原生／XHyper 功耗结果。后续的单独采集器短测显示负载期间电池净流出增加，这可以支持负载响应检查，但仍不能证明绝对精度或整机供电边界。

正式使用时，调用者应保存源 trace、官方导出文件及版本、开机记录、单位及时钟绑定、供电证据和转换结果。转换器不会操作设备，不会修改镜像，也不会启用任何模拟电源统计数据。
