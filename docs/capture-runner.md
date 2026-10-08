# 设备本地采集的自动启停

采集控制复用平板已经安装的 Perfetto。启动后，控制端立即返回，设备在本地采样并保留有限时长上限。正式功耗窗口只有一个采集器，设备不会承受周期 ADB 拉取。

```sh
python3 -m abbench capture-start --serial T11206HXH6600007 --out runs/run-001/telemetry --duration 1200 --mode xhyper
python3 -m abbench mark --run-dir runs/run-001/telemetry --event workload_start
python3 -m abbench mark --run-dir runs/run-001/telemetry --event workload_complete_observed
python3 -m abbench capture-stop --run-dir runs/run-001/telemetry
```

运行目录必须新建。`--mode` 只是模式标签，不独自确认镜像身份；套件还需要镜像清单及分区、应用、内核运行标识核对。标记保存设备开机时间及主机查询前后区间，人工或界面观察得到的终点必须标注观察误差。标记不是 Geekbench 内部计算阶段的独立计时源。

采集器使用官方 `--background-wait` 启动确认，保存进程号、启动标识、进程启动时间、可执行文件及私有配置和输出路径。停止及异常清理只针对仍属于该会话的进程；停止后读取 Root 输出，并再次核对启动身份。进程号已被复用时不得停止新进程。读取及发信号之间仍存在非原子边界，因此供给同一设备的套件必须串行执行。

启动失败保留原因和清理结果。无法确认进程号时，记录时长上限并中止套件，不能直接开始下一项而让不明采集污染样本。跨重启会使采集或事件无效。只有文件导出成功仍不足以证明计量准确：时钟、单位、任务边界及供电真实性继续单独核对。

2026-10-08 的平板验证已覆盖启动确认、设备时间标记、主动提前停止、自动超时后导出、Root 文件读取及最终同开机核对。原始记录按启动分开保存在本工作区运行目录，均属工具验证，没有纳入正式原生／XHyper 对比。
