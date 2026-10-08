# Mac 无线执行

Mac 与平板连接同一无线网络，先用 USB 建立 ADB，再切换无线连接并实际拔掉平板的 USB。测试控制端换成 Mac 不会改变设备内的采集协议。Linux 控制端已在实机验证 IP 地址形式的连接、采集和导出；这次使用 USB 转发，不是无线或拔线验收。Mac、拔线后的无线采集以及无线重启重连尚未实测。

## 准备官方工具

安装 Python 3.10 或以上版本，以及 Android SDK Platform-Tools。已有 Android Studio 的 Platform-Tools 可以直接使用；独立下载使用 [Android 官方页面](https://developer.android.com/tools/releases/platform-tools)。Python 可使用 [Python 官方 macOS 安装包](https://www.python.org/downloads/macos/)。不需要安装第三方 Python 包，也不要求安装 Homebrew。

打开终端，将已经安装的 `adb` 加入 `PATH`。下面只是 Android Studio 常见路径，独立下载时应换成实际解压目录：

```sh
export PATH="$HOME/Library/Android/sdk/platform-tools:$PATH"
adb version
git clone https://github.com/TomGoh/kylin100-ab-bench.git
cd kylin100-ab-bench
AB_PYTHON=python3
"$AB_PYTHON" -c 'import sys; assert sys.version_info >= (3, 10), "Need Python >=3.10"; print(sys.version)'
```

如果版本检查失败，将 `AB_PYTHON` 改成新安装解释器的实际命令或绝对路径，再执行检查。Mac 上名为 `python3` 的解释器可能只有 3.9，不能仅凭命令存在判断可用。

Perfetto 使用 [官方 Trace Processor launcher](https://perfetto.dev/docs/analysis/trace-processor)，第一次运行时自动下载并缓存对应平台的原生程序。2026-10-08 核对的 [官方 launcher 清单](https://github.com/google/perfetto/blob/main/tools/trace_processor)同时包含 Intel Mac 的 `mac-amd64` 和 Apple Silicon 的 `mac-arm64`；不需要自行编译或寻找名为 `brew perfetto` 的包。

```sh
mkdir -p private/tools
curl --connect-timeout 10 --max-time 60 -fL \
  https://get.perfetto.dev/trace_processor \
  -o private/tools/trace_processor
chmod +x private/tools/trace_processor
AB_PROCESSOR="$PWD/private/tools/trace_processor"
"$AB_PROCESSOR" --version
"$AB_PYTHON" -m abbench --help
"$AB_PYTHON" -m unittest discover -s tests
```

在正式采集前完成 launcher 的首次下载，避免分析阶段临时联网下载。双侧使用同一份 launcher 和原生程序版本，保存 Python、ADB 和 Trace Processor 的版本输出。实际运行时不要重新下载工具更新版本。

本框架以 Python 的 `subprocess` 超时控制命令，不依赖 Linux 的外部 `timeout` 程序。Mac 不需要安装 GNU coreutils。设备脚本中的 `/proc`、`/sys`、`sha256sum` 和 `readlink` 在 Android 内执行，不能替换成 Mac 的路径或 BSD 命令。本机设备锁使用 Python `fcntl.flock`；此次静态检查未发现需要改写的 Linux 主机命令依赖，最终以 Mac 上的检查和预检结果为准。

## 从 USB 切换无线 ADB

确认镜像已由负责方切换完成，Mac 与平板处于同一网络。用 USB 连接平板，解锁后接受 Mac 的调试授权。以下流程使用 [Android 官方说明中的初始 USB 连接方式](https://developer.android.com/tools/adb)，不会修改镜像或系统供电设置。

```sh
adb devices -l
AB_USB_SERIAL=T11206HXH6600007
adb -s "$AB_USB_SERIAL" tcpip 5555
```

在平板的无线网络详情页查看当前 IP，替换下面的示例地址。也可以在仍连接 USB 时读取 `adb -s "$AB_USB_SERIAL" shell ip -4 addr show wlan0`，以实际网络接口和地址为准。

```sh
AB_SERIAL=192.168.1.100:5555
adb connect "$AB_SERIAL"
adb devices -l
adb -s "$AB_SERIAL" shell su 0 id
```

必须看到 `IP:5555` 对应的 `device` 状态，且 Root 输出包含 `uid=0`。`device` 仅证明 ADB 连接可用，不能证明系统完成启动。此后每个框架命令都显式使用 `--serial "$AB_SERIAL"`；此值是传输地址，平板硬件身份仍须由框架读取核对，不可把 IP 地址写成新的硬件序列号。

现在实际拔掉平板 USB，确认没有另外的充电线或底座供电，再运行无线预检。每次使用新的目录；下例目录已经存在时应更换名称。

```sh
adb devices -l
caffeinate -i "$AB_PYTHON" -m abbench doctor \
  --serial "$AB_SERIAL" --out runs/mac-wireless-preflight-001
caffeinate -i "$AB_PYTHON" -m abbench supply \
  --serial "$AB_SERIAL" --profile profiles/t11206.json \
  --out runs/mac-wireless-supply-001.json
```

在 `runs/mac-wireless-preflight-001/` 检查原始证据：

- `identity.txt` 保存 Root 身份、设备构建、启动标识、设备运行时间及内核运行身份。确认是目标平板，且无线连接没有回到另外的设备。
- `battery.txt` 的 USB、AC、无线供电状态均应明确为未供电，充放电状态应与实际拔线后的状态一致。信息缺失、模拟状态或相互矛盾时不能认可电池独立供电。
- `power-supply.txt` 保存每个供电节点的类型和 `uevent`。核对实际 USB 节点的 `POWER_SUPPLY_ONLINE=0`，以及所有实际外部输入；仅 USB 节点为零不足以排除 AC、无线充电或底座供电。
- `snapshot.json` 与各 `*.stderr.txt` 保存超时及缺失原因。预检采集完成不代表传感器已校准或功耗边界已验证。

开始和结束窗口都必须保存同一启动的供电证据。不要用 `dumpsys battery unplug`、`dumpsys battery set` 或停充设置代替物理拔线，不要手工把配置的 `input_supply_verified_off` 改成 `true` 作为证据。内置电池电流、电压和累计电荷的可用性及分辨率仍按 [功耗方法](power-method.md)检查；拔线本身不保证短时待机累计量足够精确。

`supply` 返回零仅表示该次观察中配置对应设备的已报告外部输入均关闭，传感器仍未经校准。USB 还在供电、节点未知、设备身份不匹配或清单变化时返回非零并保存原因。采集器自动保存功耗窗口前后的原始供电证据；分析器根据同启动、同物理设备、窗口覆盖和实际状态选择电池侧边界，静态配置中的隔离标志不作为证明。

2026-10-08 实机枚举了九个电源节点。`ac`、`usb`、`wireless`、`sc8989x_charger` 与 `sprd-tcpm-source-psy-sc27xx-pd` 的 `online` 均必须为零；后两个虽然类型为 `Unknown`，仍按可能的外部输入检查，不能忽略。`battery.online=1` 表示电池接口在线，不表示外部充电。

另外三个没有 `online` 的节点按实机软件接口分类：`sc27xx-fgu` 绑定 `sprd-fgu` 驱动并暴露电池电流、电压和电量属性；`sc27xx_fast_charger` 绑定同名驱动，遥测属性仅有 `charge_type`、`voltage_max`；`sprd_bcl` 绑定同名驱动，遥测属性仅有 `temp`。完整属性读回保存在本地验证记录 `runs/runner-validation/supply-roles-001.txt`，不提交设备原始数据。配置将这些辅助接口角色绑定到物理序列号和九节点清单摘要；新增节点、类型变化或角色缺失会拒绝升级边界。此分类依据设备属性观察，辅助作用包含推断，不能证明内部硬件供电拓扑。因此测量时必须实际拔除 USB、底座等所有外部输入，并在全窗口保持断开。

熄屏窗口同样只在端点读取供电证据，不在等待期间新增网络轮询。累计电荷在本机尚未通过验证，配置继续保留 `counter_validated: false`；即使拔线预检通过，待机功耗也可能保持缺项，不能修改标志把静止计数解释为零耗电。

## 执行无线性能、内存和功耗窗口

无线功耗轮保持当前启动，不请求自动重启。现有 `suite` 的 JSON 选项支持 `reboot: false`；复制覆盖配置，仅关闭其中的重启请求，其余预算和截止时间仍保留。输出目录、模式、正式镜像清单和配置由执行者按已经核实的材料填写。

```sh
"$AB_PYTHON" - <<'PY'
import json
from pathlib import Path
source = json.loads(Path("examples/quick-coverage.json").read_text())
source["reboot"] = False
Path("private/wireless-options.json").write_text(json.dumps(source, indent=2) + "\n")
PY

AB_MODE=xhyper
caffeinate -i "$AB_PYTHON" -m abbench suite \
  --serial "$AB_SERIAL" --mode "$AB_MODE" \
  --processor "$AB_PROCESSOR" \
  --profile profiles/t11206.json \
  --manifest private/formal-manifest.json \
  --options private/wireless-options.json \
  --out runs/mac-xhyper-wireless-001
```

`profiles/t11206.json` 是本次实机核对的设备配置；刷机后须核对接口和身份是否仍一致。`formal-manifest.json` 是镜像负责方交付并核实的正式材料，不是本仓库故意留空的示例。模式标签不能替代镜像读回核对。上述命令只关闭启动测量，不缩短正式稳定时间或温度门限；不要追加 `--reboot`，也不要把原始 `quick-coverage.json` 直接用于这轮无线运行，因为它启用三次重启。仅验证工具链时可以追加 `--validation-only`，验证数据不得进入正式 A/B。

该无线轮运行环境、内存、亮屏静置、熄屏窗口和 Geekbench CPU/GPU，由 `suite` 串行组织和采集。它会明确缺少新的启动样本；单独这轮不能满足正式活动的双侧三次启动要求。功耗结果是否能标记为设备电池供电边界，以当前框架实际保存和验证的证据为准；如果只能给出电池净能量或计量不可用，应保留该边界及原因。

Apple 的 [官方 caffeinate 手册](https://github.com/apple-oss-distributions/PowerManagement/blob/main/caffeinate/caffeinate.8)规定 `-i` 在所包装命令运行期间防止系统因空闲休眠。保持 Mac 开盖并持续连接同一网络；不要把这理解为可以合盖仍可靠测试。该操作只影响控制端，不会给平板设置唤醒锁。

待机窗口由现有 `idle` 模块等待，期间不额外轮询 ADB、运行设备监控窗口或手工操作平板。结束时无线 ADB 的读取和唤醒开销会进入端点误差边界。无线网络本身以及性能轮中必要的 ADB 通信仍有能耗，两侧必须使用同一网络、同一控制端、同一轮询策略和同一测试版本；记录中不能声称完全无观察开销。

## 启动测试单独组织

传统 `adb tcpip 5555` 是否跨重启保持，以及 Mac 能否自动恢复连接，当前没有验证。网络 IP 还可能变化；出现断连不能当作启动完成，更不能记为零秒。现有 `campaign` 调用模式切换适配器并通常自动重启，不能据此声称整场活动已在拔线无线条件下验证。

优先在 USB 下测量启动请求到系统完成的时间，保存每次新的启动标识和轮询区间。随后等待系统稳定，重新连接无线 ADB，物理拔线并重新核对供电边界，再执行该模式的无线性能及功耗轮。原生 Android 和 XHyper 两侧都使用相同分工，分别说明启动阶段使用 USB、功耗阶段实际拔线。USB 下的电池净读数不能混入拔线功耗窗口。

```sh
caffeinate -i "$AB_PYTHON" -m abbench boot \
  --serial "$AB_USB_SERIAL" --reboot --timeout 180 \
  --out runs/mac-xhyper-usb-boot-001
```

执行前应先向在场人员说明即将重启。每次重复使用新的输出目录；此命令测量的是暖重启请求到系统完成的观察代理，不是完整冷启动。要实现跨镜像切换和重启的全无线活动，需要交付方的外部适配器在真实设备上证明重连及身份核对，并保留失败处理；本文不提供未经验证的刷写或重连实现。

每轮结束后保存结果、原始输出、工具版本和 Git 提交号，再按相同方式执行另一侧。USB 启动样本与无线轮的关联必须保留各自启动标识、模式身份和供电边界，不能仅凭目录名拼接为已经完成的完整 A/B。
