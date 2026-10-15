# NanoKVM Pro 麦克风端子联动

这是面向 NanoKVM Pro 应用 **1.2.15 精确基线**的源码 overlay，保存已经使用的内核补丁、运行时、网页联动与 Windows 安装工具。它与应用主源码并列管理；不会因放入本仓库就自动集成到当前 `web/` 或 `server/` 的构建。

控制端网页开启麦克风时，浏览器先取得采集权限并建立音频连接，再由 KVM 接通 USB Audio 的麦克风端子；关闭时停止采集并断开端子。Windows 根据端子状态处理录音设备可用性。已测试电脑首次手动将该麦克风设为默认后，后续关闭会切走，重新开启会选回。KVM 没有直接调用 Windows 的默认音频设备设置接口；不同 Windows 版本、应用自身的输入选择仍需现场验收。

## 目录

| 目录 | 内容 |
| --- | --- |
| `kernel/` | 固定内核基线的 UAC2 补丁、修改前后 C/H 文件、完整 `.config` |
| `runtime/` | 安装器、清单、USB 初始化脚本及 `mic-jack.sh` |
| `web/controller/` | Python 租约控制器及受限命令包装器 |
| `web/client/` | TypeScript/ESM 客户端、React 适配器、官方网页资产转换器 |
| `web/install/` | 网页安装器、基线及目标哈希清单、systemd 服务和控制器安装输入 |
| `web/baseline/` | 官方 1.2.15 网页的固定哈希与精确替换锚点 |
| `installer/` | Windows 图形入口与新机备份、核对、安装编排源码 |
| `tools/` | 本 overlay 的离线构建、组装工具；不是 AXDL 或驱动二进制 |
| `installer/tests/`、各模块 `test*` | 离线测试；统一入口为 `tools/check-source.py` |
| `docs/` | 面向已组装发行 ZIP 的逐步安装、AXDL、更新和回退说明 |
| `sources-lock.json` | 上游提交、官方输入哈希及原始源码逐文件哈希 |

## 适用与验证

适用设备为 NanoKVM Pro eMMC 型号。安装检查同时核对应用、内核、DTB、脚本及网页哈希，不能仅凭 `4.19.125` 或 `1.2.15` 版本号判断兼容。精确基线来自公开内核提交 `94a7777d24781a1ca6671f937f55476a0d4e4181`，与后来引入音频反馈变化的其他内核提交分开管理。

截至 2026-10-15，操作人员已在一台 Pro 上确认候选内核正常启动、Windows 默认录音设备随开关切走和返回、网页开关工作、重新开启仍有声音。Windows 图形入口和统一新机安装编排完成离线测试，尚未在另一台新机完成整套安装验收。本 fork 的源码组装及测试也不等于新的硬件安装验证。

首次启用端子能力会重建一次 USB，键鼠和 USB 虚拟网卡会短暂断开。日常麦克风开关仅写端子状态，不重新枚举整个 USB；保留现有名称、VID/PID、序列号及功能链接。不要通过 KVM 的 USB 虚拟网卡执行首次安装。

## 构建和安装边界

源码 checkout 不是完整的新机安装包。需要用本目录工具生成运行时归档、网页安装 payload 和最终发行目录，再使用发行目录内的 `Start.cmd`。`docs/` 中的相对路径和按钮名称以最终 ZIP 的布局为准；不要直接在 KVM 终端执行源码树里的安装器。

网页桥接依赖**官方发布资产**，不是将 Git tag 的源码重新编译后就能得到同一网页。官方 1.2.15 tag 与实际发行资产存在差异，所以构建必须先取得 `sources-lock.json` 固定哈希的官方发布归档或 DEB，并安全提取 `/kvmapp/server/web`。然后运行 `web/client/build-web-candidate.py` 生成候选网页，再用 `tools/stage-web-payload.py` 组装 `web/install` 对应 payload；具体 CLI 参数由各脚本的 `--help` 提供。官方 15 个 JavaScript 模块统一更名并重写相互引用，避免新旧入口混用造成模块实例不一致。生成文件不提交到 Git。

`web/install/manifest.json` 保留已验证发行包的目标哈希，包含当前源码树中尚未生成的网页文件。缺少这些文件时不能运行安装；不要删去清单项或放宽检查来绕过生成步骤。安装器也不会接受任意新版应用。编排会自动补齐原厂缺失的 `/etc/kvm/scripts` 目录，并沿用原运行时和网页安装器的备份、状态及回退检查。

内核源码来自独立的 `maix_ax620e_sdk_kernel` 仓库；本应用 fork 中保存补丁和配置。对固定上游应用 `kernel/patches/0001-uac2-jack-state.patch` 后，修改的两个文件应分别与 `kernel/modified/` 字节一致。完整内核构建还需固定 SDK、MSP、GCC 9.2.1 工具链以及显式提供的 initramfs。该 initramfs 不随源码提交。签名和 AXP 组装所需工具、密钥均由构建者单独提供；仓库不包含私钥、旧机提取分区或厂商工具二进制。

Windows 发行包还需组合已核对 SHA 的候选 AXP、AXDL 和驱动。源码仓库只记录这些外部输入的来源与哈希，不将第三方 EXE、DLL、驱动、固件或历史设备备份纳入 Git。新机安装仍需现场接线、按实体按键，并在 AXDL 中核对镜像、取消重新分区及擦除选项，再确认开始烧写。

## 运行行为

网页通过现有、需要登录认证的 Script API 调用固定包装器，再访问本机 Unix socket。服务只接受约定动作和参数；同一时间只有一个所有者持有租约。开启分为预留和提交两步，定期续租；关闭停止本地采集并释放租约。断网、浏览器退出或计时器严重节流后，约 10 秒超时关闭端子，恢复连接需要重新点击开启。

端子开关只影响主机看到的录音可用性，不向 Windows 安装常驻程序。当前网页联动支持 H.264/H.265 WebRTC 模式。源码中的离线测试不会采集真实麦克风、访问真实设备节点或证明所有 Windows 与录音软件的自动切换行为；最终仍需在目标电脑执行开、关、再开及实际传声验收。

## 维护人员离线检查

在仓库根目录运行：

```text
python customizations/mic-jack/tools/check-source.py
```

需要 Python 3、Node.js；Windows 图形入口测试使用系统 Windows PowerShell 5.1，Linux 设备编排 fixture 在 Linux/WSL 运行。检查器只操作临时文件、模拟后端和临时 Unix socket，不调用真实 KVM 或烧录工具。缺少某个平台或外部发布资产时会明确输出 skip，不能将跳过解释为通过。Node 跨语言测试由检查器设置 Python 解释器；单独运行时可设置 `NANOKVM_PYTHON`。Node 的类型擦除 API 可用时还会验证 TS 与 MJS 对应关系，API 不存在时仅跳过这一项。

完整资产回归接受以下显式环境输入：

| 环境变量 | 输入 |
| --- | --- |
| `NANOKVM_BASELINE_WEB` | 已核对的官方 1.2.15 `kvmapp/server/web` 目录 |
| `NANOKVM_CANDIDATE_WEB` | 网页 builder 生成的候选目录 |
| `NANOKVM_WEB_PACKAGE` | stager 生成的完整网页安装包目录 |
| `NANOKVM_INSTALLER_ARCHIVE_DIR` | 含两份已验证 runtime/web `.tar.gz` 的目录 |
| `NANOKVM_REFERENCE_CANDIDATE` | 可选，历史已交付候选目录，用于逐字节比较 |

这些路径由维护人员提供，源码中没有历史工作电脑的绝对路径。最终新机 ZIP 的生成入口是 `tools/build-install-kit.py --help`，需要显式提供已锁定的归档、候选 AXP 和厂商工具目录；它只组装这些已验证输入，不编译内核或生成签名。普通安装用户使用发行 ZIP 的中文窗口，不需要运行以上开发命令。

此目录通过 `.gitattributes` 保留文件原始字节，避免 Git 自动转换换行后破坏已锁定 manifest 和脚本哈希。生成的 MJS 保留 TypeScript 类型擦除后的空白，内核补丁保留统一差异的上下文缩进。

## 更新、回退与许可证

正常重启会保留持久化安装。官方应用更新可能覆盖网页及 USB 初始化脚本；附带的固件更新还可能重写两个内核分区。这个 fork 不会自动阻止更新，也不会在未知版本上自动重打补丁。更新后先核对新基线再适配，不能直接重新运行旧安装器。网页回退仅恢复网页联动，不退回内核和 jack 运行时；详见 `docs/更新与回退.txt`。

应用仓库沿用其根目录 GPL-3.0 许可证；Linux 内核文件保留原版权和许可证声明。AXERA、Sipeed 工具与驱动由各自权利人提供，不因本 overlay 的存在改变其许可。
