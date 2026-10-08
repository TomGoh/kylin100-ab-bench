# Geekbench 6 自动运行与结果绑定

本模块负责启动已安装的应用、选择 CPU 或 GPU Vulkan/OpenCL、等待新结果和导出一致性数据库。它不刷写、重启或调整温控。GPU 跑分衡量计算性能；渲染和日常网页场景由统一编排另行执行。当前代码的离线阳性与拒绝测试已经完成；真实 CPU/GPU 界面路径仍需要统一执行方进行一次设备验证，本文件不代表已经完成正式原生 Android/XHyper 对比。

## 调用

在仓库目录内由统一入口调用。每次 `out` 必须是新的目录，不能复用历史目录。

```python
from abbench.geekbench_runner import run_geekbench

result = run_geekbench(
    serial="T11206HXH6600007",
    kind="cpu",                         # "cpu" 或 "gpu"
    out="runs/runner-validation/cpu-001",
    mode="xhyper",                      # native / xhyper；只标身份，不据此判断镜像
    api="Vulkan",                       # GPU 时严格核对；CPU 时不使用
    expected_app_version="6.7.1",
    expected_boot_id="本次实读的启动标识",
    poll_interval_s=30,
    compute_timeout_s=1200,
    persist_timeout_s=180,
    command_timeout_s=20,
    seen_uuids=(),                       # 编排器应传入此前各次成功运行的 UUID
)
if not result["valid"]:
    print(result["reason"])
```

命令必须在实际能够执行 adb 的主机运行；使用设备 `su 0`，不运行 `adb root`。调用者负责同一设备的串行排他执行、屏幕已解锁、应用许可界面已经确认、两侧屏幕条件一致及开机后温度与后台状态满足协议。界面自动化从当前 XML 中识别资源标识和标签，再计算本次按钮的中心；不硬编码上次坐标。若应用界面发生变化或目标不唯一，保留失败，不自动猜测。

已核对本平板应用的可导出入口是主活动 `com.primatelabs.geekbench.HomeActivity`；没有在已安装 manifest 中发现公开跑分意图。当前适配使用 `runCpuBenchmarks`、`runComputeBenchmark`、`computeApiSpinner` 资源标识和 CPU/GPU、Vulkan/OpenCL 标签。GPU 必须先核对选择器文本；最终还要用新结果 JSON 的接口文字证据核对，保存原始 `compute_api` 和数据库 `api`，不猜整数枚举的意义。应用语言或版本导致标签不可读时，运行会拒绝继续。

应用版本和全部安装 APK 的 SHA-256 在启动前保存。`app.apk_files` 保留各安装文件的名称与哈希；`app.apk_sha256` 是按文件名排序后的整个 APK 集合摘要，包含工作负载分包，排除会变化的安装目录名称。编排器应将这个摘要放入共同比较条件，不能只比较版本号。

## 状态、时钟和保存内容

`run.json` 持续记录准备、启动、正在计算、计算完成、结果持久化及最终验证。开始点击及每次状态观察都保存本次 `boot_id`、设备 `/proc/uptime` 观察前后值；任一观察或最终身份不属于原开机就拒绝绑定。设备启动后时钟与主机单调时钟分别使用，不相减。

默认每 30 秒读取一个文档号并观察必要界面状态，不循环唤醒屏幕，也不在计算期间反复拉数据库或完整环境。观察本身仍有开销，双方必须采用同一设置。UI 首次进入上传或结果页只是计算完成的观察上界，`compute_complete_window` 是上次观察计算与首次观察完成之间的区间；它不是精确结束时间。若新结果已经持久化但未观察到计算结束，保存空的完成窗口，不用持久化时间冒充计算结束。`internal_runtime_s` 保留应用内部 runtime，未经时钟校准不能给子项构造绝对功耗时间。

计算与上传等待分别设置上限。上传失败、应用错误、超时、界面不可识别、版本不符和结果不完整保留 `failure.json`、`run.json`、命令 stdout/stderr、失败界面及应用日志。超时或上传失败不会立即停止尚未保存结果的应用；执行方处理现场后再开始下一项。离线上传失败不能采用旧历史分数填充本次结果。

输出中的 `valid=true` 仅证明应用结果绑定和完整性检查通过。`measurement_validated=false` 表示此函数没有验证镜像、温度、功耗边界及完整正式协议；统一编排应另行作出这些判断。

## 数据库一致性与归属

当前平板已只读确认提供 `sqlite3`。首选 `.backup` 在线备份到本次独立设备目录，通过 root tar 传输，再用主机 SQLite 只读解析和 `integrity_check`。运行前和运行后快照、完整导出 JSON、原始结果 JSON 及失败文件均保留。新文档号只作为候选，JSON 的 `complete_benchmark` 必须表明完成，之后再导出并检查全部类型和分数引用。

无 sqlite3 时只有结果页观察可作为停止写者的候选条件，最终仍必须从停止后的数据库证明唯一新增完整结果。这条兼容路径不会把结果页本身当成持久化成功，并可能因为应用尚未保存而拒绝结果；当前平板无需此降级路径。兼容导出只停止 Geekbench 一个应用，并把 DB、WAL 和 SHM 一起复制；不停止其他服务，不把活动数据库的裸 `cat` 当作一致快照。

绑定必须同时满足：只有一个文档号高于运行前最大值；CPU/GPU 类型正确；UUID 非空、未出现在前快照和编排器传入的 `seen_uuids`；版本一致；应用与导出器判定完整有效；GPU 数据库与 JSON 原始 API 一致，且新文档内 API 文字证据与选择接口一致。旧结果、重复 UUID、多个新增结果和无法核对接口的结果全部拒绝。历史预检数据库可以验证解析器，不能作为本轮跑分。

现有历史 CPU 内部时长约 650 秒，GPU OpenCL 约 548 秒，不能预设完整 GPU 运行只有两三分钟。实际 Vulkan 时长以本次验证为准。统一执行方应将验证样本与正式 A/B 样本分开保存。

## 适配验证

离线运行：

```sh
python3 -m unittest tests.test_geekbench_runner -v
python3 -m unittest discover -s tests
```

真实验证由统一入口先运行一次 CPU，再运行一次 GPU Vulkan，核对界面路径、结果 UUID、文档号、原始 API、内部 runtime、启动标识及前后备份。不得并行启动两个 Geekbench。失败必须有独立目录，下一次使用新目录；有意坏输入被拒绝才算拒绝测试通过。
