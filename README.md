# 无障碍助手（Accessibility Assistant）

**静默截屏 → AI 识别 → 手机语音播报 + 触觉提示**：为视障用户设计的电脑操作辅助工具。

在电脑上按下快捷键（或鼠标触碰屏幕角落、横向划一下、点一下手机），电脑会**静默截取屏幕内容**，
经你配置的 AI 模型识别后，把「当前界面该选什么」以**大字结果 + 语音播报 + 按选项序号震动**
的方式送到手机上——不用看屏幕，也能完成选择。

> 设计初衷：视障用户使用的系统会把界面简化成「带选项的选择形式」。本工具把屏幕上的
> 选择转化为**听得见、摸得着**的提示：结果直接念出来，选项按序号振动（第 2 个选项震 2 下）。

<img width="1264" height="841" alt="image" src="https://github.com/user-attachments/assets/baa285b2-c213-4118-afad-d9f7bdbd73dd" />


## 功能特性

- **静默截屏**：截图不弹窗、不打断操作；支持全屏 / 框选区域 / 免按键触发（鼠标触碰屏幕角落、横向划一下）。
- **AI 识别**：兼容任意 OpenAI 格式的模型服务（chat / responses / anthropic 三种协议），多渠道按顺序回退。
- **手机端反馈**：
  - 大字结果 + 简短解析；
  - **语音播报**（可加播解析、B/D 读作 Bravo/Delta）；
  - **触觉提示**：第几个选项震几次（C = 三下）；多选、多条内容依次提示。
- **iPhone 原生触觉**：iOS 端通过配套 App 的 CoreHaptics 桥实现精确可数的震动（iOS 网页没有震动接口）。
- **连接方式**：局域网直连或自建中转服务器；手机端支持扫码 / 自动发现 / 手填地址。
- **电脑端控制台**：状态、运行日志、设置一体化；也支持浏览器直接打开。
- **悬浮窗**：常驻屏幕的小浮层实时显示识别结果；「识别」按钮可直接截屏识别（无需手机）。
- **隐私选项**：窗口可从任务栏/Alt-Tab 隐身，可开启系统级防截屏（截图/录屏中不显示工具窗口）。

## 工作原理

```
电脑（静默截屏）──► AI 模型识别 ──► 手机端
     │                                ├─ 大字结果 + 解析
     │                                ├─ 语音播报
     └─ 快捷键 / 角落 / 划动 / 遥控    └─ 按选项序号震动
```

- 电脑端（Windows）：Python + pywebview（WebView2 界面），截屏用 mss，无控制台窗口。
- 手机端：同一套网页，手机浏览器直接打开；iPhone 使用配套 App 获得精确触觉反馈。
- 通信：局域网直连（WebSocket，断连时 HTTP 兜底）或中转服务器。

## 快速开始

### 1. 电脑端

- **方式一**：从 Releases 下载打包好的 exe（若已发布）——`无障碍助手PC.exe`（标准版）或
  `无障碍助手PC-OCR.exe`（含本地 OCR，约 100MB）。
- **方式二**：从源码运行（Windows，Python 3.10+）：

  ```bat
  install.bat    :: 创建虚拟环境并安装依赖
  start.bat      :: 启动
  ```

首次使用：把 `config.example.json` 复制为 `config.json`（或直接在程序设置页里改），
填入模型渠道（接口地址 / 密钥 / 模型名）。也可以稍后在手机端设置页里填。

### 2. 连接手机

电脑启动后，主窗口会显示二维码和连接地址：

- **iPhone**：装配套 iOS App（构建方法见下），扫码或自动搜索同网络的电脑端；
- **安卓**：手机浏览器打开显示的地址即可（震动走浏览器接口，强度不可调）。

### 3. 使用

- **截屏触发**：F8 全屏 / F9 框选 / F10 按区域截图；鼠标推到屏幕角落、横向划一下；
  或点手机底部的「遥控截图」按钮；
- 识别结果自动出现在手机上：朗读 + 震动，并显示大字结果；
- 电脑端还有悬浮窗可常驻显示结果。

## 设置

手机端与电脑端的设置页共用同一份配置，改任意一边都会实时同步：

- **提醒**：震动（总开关 / 强度 / 三挡节奏）、语音播报、B/D 北约音；
- **识别**：收到内容后自动识别、识别时附带截图、模型渠道、提示词；
- **电脑端**：防截屏、免按键触发、框选区域等。

## iOS 配套 App

iOS 上网页拿不到震动接口，配套 App 用 `WKWebView` 包住页面并注册 CoreHaptics 触觉桥。

- **有 Mac**：安装 [XcodeGen](https://github.com/yonaskolb/XcodeGen) 后：

  ```bash
  cd ios
  xcodegen generate
  open AccessibilityAssistant.xcodeproj
  ```

  在 Xcode 里选自己的 iPhone 运行（首次运行需在系统设置里信任开发者证书）。

- **没有 Mac**：把仓库推送到自己的 GitHub，用 `.github/workflows/ios-ipa.yml`
  在云端出 ipa，再用 [Sideloadly](https://sideloadly.io/) / AltStore 重签安装
  （免费 Apple ID 签名 7 天有效，需定期重签）。

## 从源码构建（Windows 发行版）

```bat
build_exe.bat        :: 标准版
build_exe_ocr.bat    :: 含本地 OCR 的版本
```

PyInstaller 单文件打包，产物在 `dist/`；运行数据（config / captures / logs）生成在 exe 旁边。

## 目录结构

```
pc/       电脑端全部逻辑（服务、截屏、OCR、模型代理、悬浮窗、控制台……）
web/      网页端：手机布局 + PC 控制台布局 + 悬浮窗，共用一份页面代码
ios/      iOS 配套 App（WKWebView + CoreHaptics 触觉桥）
tools/    实用脚本（滑动调试台、防火墙放行）
```

## 隐私

- 截屏图片只在本机与**你自己配置的模型服务**之间传输；本程序不做任何上传或遥测；
- API 密钥只保存在本机 `config.json`，不会发送到其它任何地方；
- 历史截图留在本机 `captures/` 目录，可随时删除。

## 常见问题

- **手机打不开页面**：大多是 Windows 防火墙问题——双击 `放行防火墙.bat`
  （需要管理员，执行一次即可，之后换网络不用再管）。
- **搜不到电脑端**：先用扫码 / 手填地址；「自动搜索」依赖 mDNS，受防火墙与网络影响。
- **没有震动**：iPhone 需要装配套 App；安卓浏览器需要页面处于前台。
- **结果不准 / 无法确定**：在设置里打开「识别时附带截图」，或更换视觉能力更强的模型。

## 参与贡献

欢迎提交 Issue 与 Pull Request。

## 许可

[MIT License](LICENSE)


## 🔗 LinuxDo 社区

<div align="center">
  <a href="https://linux.do" target="_blank">
    <img src="https://cdn3.ldstatic.com/original/4X/c/c/d/ccd8c210609d498cbeb3d5201d4c259348447562.png" alt="LinuxDo" height="60">
  </a>
  <p>
    <a href="https://linux.do" target="_blank"><strong>LinuxDo 社区</strong></a><br>
  </p>
    <p>@蕉灼の仓鼠</p>
    <p>本人长期活跃于L站;</p>
    <p>这里的人很好说话又好听;</p>
    <p>欢迎都来加入L站大家庭。 </p>

</div>

