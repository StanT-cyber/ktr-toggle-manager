# Ktr启停管理工具

一个用于管理 Pentaho Data Integration（Kettle / Spoon）`.ktr` 转换文件中 Hop 启停状态的本地 Windows 工具。

它适合需要频繁单独调试某个视图或数据链路的场景，可以查看当前启用、停用状态，批量调整 Hop，并在写回文件前进行结构安全校验和变更预览。

<p align="center">
  <img src="assets/icon-preview.png" alt="Ktr启停管理工具图标" width="180">
</p>

## 主要功能

- 读取 `.ktr` 中 `<order>` 下的全部 Hop。
- 显示来源步骤、目标步骤、所属分支和当前启停状态。
- 支持单选、多选、搜索和状态筛选。
- 批量启用、停用或“仅启用所选 Hop”。
- 自动识别完整连通分支，可按分支启用、停用或仅启用所选分支。
- 检查启用链路中的数据流循环。
- 检查悬空 Hop、缺失步骤、重复步骤名和重复 Hop。
- 单次操作产生新循环时自动撤销该次操作。
- 保存前逐条预览 `原状态 → 保存后状态`。
- 支持保存副本，以及创建时间戳备份后覆盖原文件。
- 检测原文件是否被 Spoon 或其他程序并发修改。

## 下载与使用

从 [Releases](https://github.com/StanT-cyber/ktr-toggle-manager/releases/latest) 下载最新版 `Ktr启停管理工具.exe`，无需安装 Python。

1. 关闭 Spoon 中正在编辑的目标 KTR，或确保 Spoon 不会同时保存它。
2. 双击运行工具，点击“打开 KTR”；也可以把 KTR 文件拖到 EXE 图标上。
3. 选中需要调整的 Hop，或选中分支中的任意 Hop 后使用分支操作。
4. 点击“安全校验”。
5. 优先选择“保存副本”，在变更预览中确认每一项修改后再保存。
6. 使用 Spoon 打开保存后的文件，确认箭头状态后再执行转换。

详细操作请参阅[界面按钮功能说明](docs/界面按钮功能说明.md)。

## 完整分支的定义

工具把步骤视为节点、Hop 视为边，并忽略箭头方向计算弱连通分量。属于同一连通网络的步骤和 Hop 会被识别为一个完整分支。

如果多条业务链路共享同一个公共步骤，它们会被视为同一个连通分支。此时可以继续使用单条 Hop 操作进行更细粒度调整。

## 安全机制

- 循环、悬空 Hop、缺失步骤、重复步骤名和重复 Hop 会阻止保存。
- 分支部分启用会作为提示显示，但不会禁止单 Hop 调试。
- 覆盖原文件前先生成时间戳备份。
- 写入前再次解析 XML，并确认只有 Hop 的 `<enabled>` 在 `Y/N` 之间变化。
- 原文件在打开后被外部修改时，工具拒绝覆盖，避免覆盖 Spoon 中的新改动。
- 工具不连接数据库，也不会执行 KTR。

## 源码运行

环境要求：Windows、Python 3.10 或更高版本。运行功能本身只使用 Python 标准库。

```powershell
py -3.12 ktr_hop_manager.py
```

运行测试：

```powershell
py -3.12 -m unittest -v test_ktr_core.py
```

## 构建 EXE

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\build.ps1
```

构建结果位于 `dist\Ktr启停管理工具.exe`。

## 项目结构

```text
├─ ktr_core.py                 KTR 解析、分支识别、安全校验和安全写入
├─ ktr_hop_manager.py          Tkinter 图形界面
├─ test_ktr_core.py            核心自动化测试
├─ version_info.txt            Windows EXE 版本信息
├─ build.ps1                   PyInstaller 构建脚本
├─ assets/                     应用图标
└─ docs/                       按钮说明和版本更新记录
```

## 当前版本

`v0.3.0`：新增结构安全检查、保存前变更预览和完整分支启停。详见 [v0.3.0 更新说明](docs/v0.3.0更新说明.md)。

## 使用提醒

修改生产 KTR 前请保留原文件，并先在 Spoon 中检查保存副本。对于来自 PDI Repository 的转换，请先导出为文件，修改后再遵循现有发布流程导入。
