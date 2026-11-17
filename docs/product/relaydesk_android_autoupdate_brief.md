# RelayDesk Android 自动更新 Brief

状态：待确认（图：`docs/archify/relaydesk_android_autoupdate_workflow.html`）
日期：2026-11-17

## 要解决的问题

手机端目前只能手动装 APK；仓库发布新 Release 后，手机不会知道。希望开机或打开 App 时检查
GitHub Release，发现新版就提示，用户确认后下载并走系统安装器升级，保留数据。

## 目标（可验收）

1. 发布新 tag（如 `v2.5.2`）后，手机开机或打开 App，联网时 30s 内出现「发现新版本」通知/横幅。
2. 用户确认后下载 APK，校验 sha256，调起系统安装器；安装后版本号变化、聊天/配对数据保留。
3. 无网络、无 Android 资产、下载失败、签名不一致、系统禁止安装时，有可读提示且不崩溃。
4. 同一版本只提示一次（记录已提示 tag），用户选择「稍后」不反复骚扰。

## 范围

**做**：开机广播 + 应用启动触发检查（WorkManager，联网约束）；GitHub `releases/latest`；
版本比较；通知 + 应用内横幅；后台下载 + sha256 校验；PackageInstaller 安装；权限与设置引导；
版本戳与签名统一。
**不做**：静默安装（Android 不允许非系统应用）；自建更新服务器；Play 商店通道；增量/差分更新。

## 关键约束（实现前置，必须一起做）

1. **签名统一**：`android-debug.yml` 与 `release.yml` 的 Android job 共用同一份 debug keystore 缓存
   （`~/.android/debug.keystore`，同一 cache key）。否则 Release APK 无法覆盖安装 CI/旧版 APK。
2. **版本戳**：Release 工作流把 tag 转成 `versionName`/`versionCode` 传给 Gradle；
   `build.gradle.kts` 读取 `-PversionName/-PversionCode`。debug 构建用 `-dev` 后缀。
   否则所有 APK 都报 2.3.0，无法判断新旧。
3. **Android 限制**：安装必须经系统安装器并由用户确认；需要 `REQUEST_INSTALL_PACKAGES`
   与「安装未知应用」授权。开机广播受 OEM 自启动限制（OPPO 需允许自启动），因此同时在
   应用启动时检查。
4. **资产命名**：Release 资产 `RelayDesk-<版本>-android.apk` + `.sha256`；只选该后缀。
5. **安全**：只信任 `api.github.com` 的 `XinwangTao/flix-cli` 最新正式 Release；
   下载后必须校验 sha256（API digest 或 `.sha256` 资产）。

## 借鉴（1 回合）

- 复用本仓库桌面端 `autoupdate.py` 的 GitHub API 资产选择、`sha256:` digest 校验与「已安装」状态。
- Android 侧用 WorkManager（联网约束、开机可入队）+ PackageInstaller（系统确认、保留数据）。
- 不采用自建服务器/静默安装：超出个人应用权限模型。

## 待确认（≤3）

1. 检查时机：推荐「开机 + 应用启动 + 每 24h（联网时）」；或仅开机。
2. 下载策略：推荐「Wi-Fi 下自动下载，安装前系统确认」；或「用户点更新后才下载」。
3. 发布通道：推荐「GitHub latest 正式版（排除 draft/prerelease）」；是否包含预发布？
