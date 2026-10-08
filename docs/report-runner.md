# 离线对比报告

`abbench.report.create_report` 汇总测试框架已经产生的指标行，不连接设备，不启动测试，也不确认镜像或传感器身份。

```python
from abbench.report import create_report

result = create_report(metric_rows, "out/campaign-report", campaign={
    "purpose": "validation",
    "failed_runs": [{"run_id": "gpu-1", "reason": "结果导出失败"}],
    "skipped": [{"metric": "standby.energy", "reason": "累计电荷尚未验证"}],
})
```

接口为 `create_report(metric_rows, directory, *, campaign=None) -> dict`。每行沿用 `abbench.compare.summarize` 的输入格式：`mode` 为 `native` 或 `xhyper`，并提供 `metric`、`value`、`boot_id`、`run_id` 和 `valid`。每行应代表一次完整运行的指标；不能把一秒一次的电池读数直接当成独立测试重复。

调用方应继续携带 `unit`、`app_version`、`api`、`configuration`、`measurement_boundary`、`power_boundary` 等身份字段，以及原始结果路径、镜像摘要和计量证据。比较器按照已有身份字段分组，报告不会合并不同版本、接口或边界，也不会自行补造缺少的身份。

输出目录包含两个文件：

- `comparison.json` 保留比较器的 `groups`、`rows` 和 `issues`，并增加 `report_metadata`。其中有全部有效原始值、运行标识、启动标识、均值、中位数、样本标准差、范围和差异百分比。
- `report.md` 用简体中文展示分组统计、全部有效值、运行来源，以及失败、跳过和缺项原因。Markdown 最多显示六位有效数字，完整原始数值仍保存在 JSON 中。显示位数不代表计量精度。

统计由 `abbench.compare.summarize` 提供，报告模块不另外计算统计量。差异百分比采用 `(XHyper 均值 / 原厂均值 - 1) × 100%`。独立开机数来自不同 `boot_id` 的数量，它与运行次数分别展示；二者都不能仅凭标签证明实验独立性。报告不生成置信区间，不把观察到的差异直接归因于虚拟化层。

返回值包含 `created`、`directory`、`comparison_path`、`report_path`、`has_comparable_groups`、`validation_only`、`group_count` 和 `input_row_count`。`created=True` 只说明文件已经完整发布。`has_comparable_groups=True` 只说明至少一组有可计算的双侧差异百分比，不能证明条件可比或计量准确。空输入、只有一侧数据以及原厂均值为零时仍可生成缺项报告，相应分组不补成零。

`campaign` 可以记录执行方掌握的设备、模式身份、镜像、截止时间和测试环境。`failed_runs` 与 `skipped` 保留执行方的原始条目和原因，未提供清单不会解释成没有失败。`purpose="validation"` 会使整份报告显著标为工具验证，即使两种模式都存在有效数值，也不能把它当成正式对比结果。没有提供用途或身份材料时，报告会说明证据缺失。

`power_boundary="battery_net"` 始终解释为电池净变化，不能当作整机功耗或续航结论。充电和放电的符号保留，百分比需要结合方向解释。`battery_side_device` 的有效性仍依赖执行方的输入隔离和计量证据；报告模块不会自动认证这些证据。

失败行由比较器排除并保留原因。非有限原始浮点数写为 JSON 空值，其原始表示和位置保存在 `report_metadata.serialization_notes`，不能被误认为零。不能直接写入 JSON 的原始对象也保留类型与表示。输入行不会被修改。

调用方必须为一次报告选择唯一的新目录，并保证只有一个写入者。工具先在同一父目录的私有暂存目录写完两个文件，再整体重命名为目标目录；已有目录或符号链接会被拒绝。写入失败时不会发布半份报告。这个协议提供同一文件系统内的整体发布，不包含断电持久性保证或多个写入者之间的锁。

离线检查命令为：

```sh
python3 -m unittest discover -s tests -p test_report.py -v
```

检查覆盖已知双侧数据的统计一致性、工具验证标记、空输入与单侧缺项、失败与跳过记录、非有限数值、供电边界、身份分组，以及写入失败时不发布目标目录。检查只能验证报告行为，不能替代板上的测量真实性验证。
