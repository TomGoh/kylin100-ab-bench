# T11206 原生与 XHyper 对比：镜像交付和 Mac 上的执行说明

镜像负责方（Claude Code 会话 26）2026-10-08 14:24 交付。本目录是 [执行交接](../docs/HANDOFF.md) 要求的三样材料：正式镜像清单、身份读回依据、模式切换适配器。测试框架和流程由 Codex 负责，本文只写镜像一侧的事实和用法。Mac 上的 Claude Code 和 Codex 都可以照本文执行；无线连接、拔线功耗等测试方法以 [Mac 无线执行](../docs/MAC-WIRELESS.md) 为准。

## 一、平板现在的状态（2026-10-08 14:22 核对）

| 项目 | 内容 |
|---|---|
| 系统 | 安卓 16，`AOC/ums9620_2h10_native/ums9620_2h10:16/BP2A.250605.031.A3/eng.root:userdebug/release-keys`（10-08 12:30 编译），`su 0` 可用 |
| boot_a | **XHyper cmp1**，sha256 `0235c68c…`（14:32 起运行，启动编号 `67abc779`） |
| boot_b | 新系统的原厂 boot，sha256 `30e41ac4…`，**不要动**：cmp1 起不来时引导程序会退回这里 |
| vendor_boot_a/b | 新系统原厂的 `e557a0dd…`（`loglevel=1`），两种模式共用，不要动 |
| 平板上的镜像副本 | `/data/local/tmp/boot_a.stock-20261008.img`（原厂，`30e41ac4…`）、`/data/local/tmp/xhyper_boot-cmp1.img`（`0235c68c…`）、`/data/local/tmp/xhyper_boot-cmp2.img`（`a8b67677…`） |
| Geekbench 6 | 6.7.1，用户装好（firstInstallTime 2026-10-08 14:22:51）；APK 集合摘要 `51581978…` 已写进两份清单，cmp1 清单校验通过 |
| 串口 | 13:38 以后没有收到任何数据，飞线可能断了；测试不依赖串口 |

两种模式的宿主内核是同一个文件（`16ecc6dd…`），系统指纹、`/sys/kernel/notes`（`a898c6b2…`）都相同，唯一的差别是 boot_a 有没有 XHyper。

## 二、本目录的文件

| 文件 | 用途 |
|---|---|
| `t11206_mode_switch.py` | 模式切换适配器：把平板上对应的镜像副本写进 boot_a，核对读回，重启，等新的启动完成，再核对 boot_a、槽位和模式标记。只写 boot_a。每次调用都会**重启一次平板，约 1 分钟** |
| `mode-switch.argv.json` | 交给 `campaign --adapter` 的参数文件（在仓库根目录运行时有效） |
| `t11206-images.json` | 适配器用的镜像表：native = 原厂，xhyper = **cmp1** |
| `t11206-images-cmp2.json` | 同上，xhyper = **cmp2** |
| `formal-manifest-cmp1.json` | 正式镜像清单（cmp1），`validate-manifest` 通过 |
| `formal-manifest-cmp2.json` | 正式镜像清单（cmp2），`validate-manifest` 通过（14:31 在新系统上核实身份） |
| `app_identity.py` | 装好 Geekbench 后，按框架的算法算出 `app_version`、`app_apk_sha256` 并写进清单 |

## 三、两个 XHyper 镜像怎么选

- **cmp1**（`c815907c`）：代码等于 Gerrit 上各改动最新补丁集的叠加（拆锁、卡死修复、向量页、T11206 中断和就地 SGI），只多一处合并冲突的取舍；配置只把日志级别降到告警，不开黑匣子。代码里还留着一些不受开关控制的诊断计数和偶发打印（约每 20 分钟一百多行）。
- **cmp2**（`e9a6abab`）：在 cmp1 上加一个没有推送的本地提交，把这些诊断计数和打印放到开关后面，镜像里关掉。最接近“只有虚拟化本身的开销”。
- 两者的评审、门禁、QEMU 记录在主工作区 `docs/artifacts/t11206-native-cmp-20261008/`（`AUDIT.md`、`cmp2/`）。默认文件用 cmp1；要用 cmp2，把下面命令里的清单和镜像表换成 `-cmp2` 的那份。

## 四、Mac 上的步骤

前提：Mac 上有 `adb`（Android platform-tools）、Python 3.10 以上、Perfetto 的 `trace_processor`（见 MAC-WIRELESS 第一节），仓库已经拉到本机，命令都在仓库根目录执行。

1. **用 USB 连接平板**，解锁并接受调试授权：`adb devices` 应列出 `T11206HXH6600007 device`；`adb -s T11206HXH6600007 shell su 0 id` 应有 `uid=0`。切换模式和开机计时都在 USB 下做（MAC-WIRELESS 第“启动测试单独组织”节）。
2. **Geekbench 6 已装好、身份已写进清单**（6.7.1）。以后重装或升级 Geekbench 后，要重新算出应用身份并写进清单：
   ```sh
   python3 handoff/app_identity.py --serial T11206HXH6600007 --manifest handoff/formal-manifest-cmp1.json
   python3 -m abbench validate-manifest handoff/formal-manifest-cmp1.json   # 应为 "valid": true
   ```
3. **（建议）先自检适配器**，每条命令重启一次平板，先告诉身边的人：
   ```sh
   python3 handoff/t11206_mode_switch.py --mode native --serial T11206HXH6600007 --output runs/mac-adapter-check-1/native
   python3 handoff/t11206_mode_switch.py --mode xhyper --serial T11206HXH6600007 --output runs/mac-adapter-check-1/xhyper
   ```
   每条最后一行应为 `OK: <模式> is running`；详细记录在 `<output>.switch-<模式>.log` 和 `.json`。
4. **正式双侧运行**（先原生、后 XHyper，框架负责调度；输出目录必须是新的）：
   ```sh
   python3 -m abbench campaign \
     --serial T11206HXH6600007 --profile profiles/t11206.json \
     --processor /path/to/trace_processor \
     --manifest handoff/formal-manifest-cmp1.json \
     --adapter handoff/mode-switch.argv.json \
     --options examples/quick-coverage.json \
     --out runs/formal-cmp1-<日期时间> --repetitions 1
   ```
   用 cmp2 时：清单换成 `handoff/formal-manifest-cmp2.json`；在 `mode-switch.argv.json` 末尾加上 `"--images", "handoff/t11206-images-cmp2.json"`。cmp2 已在 14:31 于新系统上开机核实（启动编号 `692b2be4`），清单已标为核实。
5. **拔线功耗**按 MAC-WIRELESS 的流程：先在 USB 下用适配器切到要测的模式，等系统稳定，再切无线、拔线测量，测量期间不重启。适配器重启后会尝试 `adb connect`，但无线端口重启后是否还在没有验证过，所以不要在拔线状态下调用适配器。

## 五、出问题时

- 适配器返回非零：看 `.switch-<模式>.log` 的最后一行。boot_a 读回不一致时它**不会重启**；开机超时或槽位变成 `_b`，说明目标镜像没起来，引导程序已退回 boot_b 的原厂系统。
- 手动回到原厂：`adb -s T11206HXH6600007 shell "su 0 sh -c 'dd if=/data/local/tmp/boot_a.stock-20261008.img of=/dev/block/by-name/boot_a bs=4M conv=fsync'"`，读回应为 `30e41ac4…`，再 `adb reboot`。
- 平板上的副本丢了（例如再次刷系统）：把镜像放到 `handoff/images/`（不进 git），适配器会自动推送。来源在主工作区：原厂 `docs/artifacts/t11206-native-cmp-20261008/stock-20261008/boot_a.img`，cmp1 `…/board-cmp1/xhyper_boot-cmp1.img`，cmp2 `…/board-cmp2/xhyper_boot-cmp2.img`，文件名按 `t11206-images*.json` 的 `local_file` 改。再次刷系统后要先确认新系统的内核还是 `16ecc6dd…`、签名密钥 sha1 还是 `3450b118…`，否则 XHyper 镜像要重新打包，找镜像负责方。

## 六、报告里要如实写的 XHyper 固有差别

- 宿主可用内存：原生 5,511,788 kB，XHyper 5,414,472 kB，少约 94 MB（虚拟化层的预留，Android 看不到）。
- 开机：适配器两次实测，发出重启到 `sys.boot_completed=1` 都是约 52 秒（只是适配器的粗略计时，正式数字以框架的启动测量为准）。XHyper 在 Linux 之前多一段 stub 和虚拟化层初始化，旧系统上量到约 1.8 秒。
- 宿主在 XHyper 下运行在 EL1（`All CPU(s) started at EL1`），没有 KVM；原生运行在 EL2，KVM 被命令行关闭。
- XHyper 下宿主设备树的 TRNG 节点被关闭，宿主没有硬件随机数源。
- cmp1 还带不受开关控制的诊断计数和打印（见第三节）；cmp2 没有。

## 七、第二块平板 T11206HXH6700075（2026-10-08 14:40 核对）

- 系统、内核标识（`/sys/kernel/notes` `a898c6b2…`）、vendor_boot（`e557a0dd…`）与 6600007 相同；boot_a 已是 cmp1（`0235c68c…`，正在 XHyper 下运行，有 `xhyper.mode=host`），boot_b 是原厂 `30e41ac4…`；Geekbench 6.7.1，APK 集合摘要与 6600007 相同（`51581978…`）。所以 `formal-manifest-cmp1.json` 和适配器两块平板共用。
- 平板上已放好镜像副本：`/data/local/tmp/boot_a.stock-20261008.img`（从 boot_b 复制，`30e41ac4…`）、`/data/local/tmp/xhyper_boot-cmp1.img`（`0235c68c…`）。
- 设备配置 `profiles/t11206-HXH6700075.json`：复制 `t11206.json`，只改 `serial`。它的电源接口清单摘要实测与 6600007 相同（`7815b686…`，`abbench supply` 读回）。配置里 `observed_preflight` 等观察值来自 6600007，以该平板自己的预检为准。
- 这块平板还没有用适配器切到原厂开过机；正式运行时框架会在每侧核对身份。
- 两块同时测：各开一个 `campaign`，`--serial`、`--profile`、`--out` 各用自己的，清单和适配器参数相同。
