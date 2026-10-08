# 镜像交付与自动测试交接

核对日期：2026-10-08。本仓库的自动编排、采集、分析和报告代码已经提供，完整单侧串行工具验证于 13:10 在平板完成，详见 [实机验收记录](VALIDATION-20261008.md)。正式原生 Android／XHyper 双侧测试尚未执行，不能用历史数据库或工具验证记录替代。

Claude Code 负责准备两个正常配置镜像和可执行的切换适配器，测试执行方负责本仓库的上层测试。计分镜像必须来自未经临时插桩修改的提交构建，新增诊断功能单列比较。框架不构建镜像，不提供刷写实现，也不自行调整频率、温控或未知电源控制。

## Claude Code 的最小交付

交付方应提供以下三份材料。缺少任一正式身份材料时，先保留缺项或使用工具验证入口，不能把未核实的标志改成真。

1. **双侧正式镜像清单。** 以 [image-manifest.json](../examples/image-manifest.json) 为结构，填写 `images.native` 和 `images.xhyper` 的 `image_sha256`、`host_kernel_sha256`、`kernel_runtime_id`、`android_fingerprint`、`app_apk_sha256`、`app_version`、`mode_evidence`，并交付 `normal_configuration_confirmed=true` 和 `identity_verified_on_device=true` 的依据。XHyper 侧另外填写 `xhyper_commit`、`manager_commit`、`build_configuration` 和 `diagnostic_features`；正式配置不带单列诊断功能。示例中的空哈希和未确认状态故意不能通过正式验证。
2. **实际运行身份读回配置。** 核对设备配置的 `identity_readback`：实际启动镜像路径、活动槽、运行内核标识路径和方法，以及命令行模式证据。使用内核 notes 摘要时，`kernel_runtime_id` 必须与该路径读回的 SHA-256 一致。XHyper 侧需要真实、非空的正面运行证据；原生侧不能仅凭缺少某个标记宣称没有虚拟化层。配置中的路径和模式标记是执行合同，必须由当前设备证据支持，不能照搬示例当作已验证事实。
3. **外部切换适配器及其参数文件。** 适配器由交付方实现并承担镜像切换、失败处理和已知可用回退。参数文件是 JSON 参数数组，框架以 `shell=False` 调用，并替换 `{mode}`、`{serial}`、`{out}`。只写一个将来要实现的程序名不构成可执行交付。

两侧的宿主内核、Android 指纹、运行内核标识及测试应用身份必须一致。`app_apk_sha256` 使用 Geekbench 运行器保存的安装 APK 集合摘要，不能与单个下载文件的哈希混淆；以现场 `app.apk_sha256` 和 `app.apk_files` 为交付依据。镜像文件哈希在两侧可以不同，不能把模式名称或镜像哈希放进共有工作负载分组键。

参数文件形式如下，程序路径及具体参数由交付方确定：

```json
[
  "/absolute/path/to/image-owner-adapter",
  "--mode", "{mode}",
  "--serial", "{serial}",
  "--output", "{out}"
]
```

适配器返回零必须表示切换步骤已经完成，目标设备重新可读且建立了新的启动标识。原始输出和失败说明应保存在 `{out}` 指定的运行材料附近；适配器不能先创建同名 `{out}` 目录，套件随后需要以新目录方式建立它。框架还会核对前后启动标识，并在套件中读回镜像、活动槽、运行内核标识及指纹；返回零或 ADB 重连不能代替正式模式身份。失败、超时或用户中止必须停止继续切换，并由交付方提供明确的恢复状态。

## 13:30 的执行入口

执行者先确保设备没有其他测试会话、已解锁，Geekbench 首次运行许可已经确认。锁屏处理只使用普通唤醒及非安全锁屏关闭，不绕过凭据。每次重启或切换前告知设备使用者，并确认对应的交付及回退材料可用。

[自动执行说明](AUTOMATION.md)给出 `suite`、`campaign` 和外部适配器合同；有限时间的一键入口采用 [quick-coverage 配置](../examples/quick-coverage.json)。执行者应核对它与当天剩余时间相符的窗口和超时，保留所选配置；缩短窗口不会自动达到总体设计中的最低重复数。

使用有限时间配置的双侧命令形式为：

```sh
python3 -m abbench validate-manifest /path/to/formal-manifest.json
python3 -m abbench campaign \
  --serial T11206HXH6600007 --profile profiles/t11206.json \
  --processor /path/to/installed/trace_processor \
  --manifest /path/to/formal-manifest.json \
  --adapter /path/to/mode-adapter.json \
  --options examples/quick-coverage.json \
  --out runs/formal-20261008-1330 --repetitions 1
```

这些路径需要替换为交付材料，输出目录必须是新目录。配置的 `deadline_utc` 带时区，并为 15:40 固定数据集、16:00 前交付报告以及异常清理留出余量。`quick-coverage` 每侧 CPU/GPU 各一次、三次启动，保留开机稳定与恢复条件，亮屏／熄屏窗口分别缩为三／五分钟；它是有限覆盖，不是总体设计要求的完整重复。当前正式每侧保守预算约 77 分钟，双侧约 154 分钟，超过 13:30 至 15:40 的 130 分钟。约 14:23 是第二侧的预算准入边界，不是完成承诺。时间不足时工具保留跳过及原因，不能调低预算绕过检查。只做自动化验证时使用 `--validation-only`；活动或指标行带验证标签时，报告会显著标为工具验证。

## 功耗与结果的交付边界

当前活动窗口优先使用 Perfetto `android.power` 和 `linux.sys_stats`，通过官方 Trace Processor 导出。USB 供电下经过单位、符号、时钟及同开机来源核对的电池读数，可以形成电池净功率和净能量实测结果；它们不是整机功耗。输入隔离尚未被只读调查证实，不能把 `suspend_input` 命令存在、USB 在线状态或输入限流值当成断电证据，也不写未知电源节点。

当前满电短窗累计电荷没有可分辨变化，待机端点能耗会保留为未验证或不可分辨，不能填零。PowerStats 示例服务的模拟轨、能量消费者及驻留数据排除。没有可靠休眠证据时，熄屏窗口称为熄屏静置。详见 [成熟工具选择](MATURE-TOOLS.md)和 [功耗方法](power-method.md)。

正式运行仍需核对开机稳定、应用 APK/API、亮度及屏幕策略、网络和声音设置，以及起始热状态。内存保留 Android 可见基线和活动期间的实际采样子窗口；虚拟化层保留内存需要交付方另给来源和范围。CPU/GPU 起止边界是观察区间，上传或结果持久化不能伪装成精确计算结束。未完成的项目、失败、恢复异常和缺项必须出现在最终报告中。
