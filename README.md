# Codex <-> Altium Designer MCP Bridge

这是一个用文件队列桥接 Codex 和 Altium Designer 的最小可用 MCP 项目。

- `server.py`: Codex 侧 MCP stdio server。
- `bridge/AltiumMCPBridge.pas`: Altium Designer 内运行的 DelphiScript bridge。
- `shared/`: 两侧交换 `request.json`、`response.json`、`heartbeat.json` 的目录。
- `scripts/mcp_smoke_test.py`: 不启动 Altium，只验证 MCP server 能被发现并列出工具。
- `scripts/mcp_call_tool.py`: 本地调试调用单个 MCP 工具。

## 当前工作方式

当前 bridge 使用“单次短窗口轮询”模式，避免 Altium UI 被脚本长时间占住：

1. Codex 发起一个工具请求。
2. 在 Altium 里运行一次 `StartMCPBridge`。
3. 脚本处理请求并自动退出。

如果一个工具连续发起多个 Altium 请求，可能需要运行多次 `StartMCPBridge`。目前已测试的比较工具和 BOM 工具都能在一次运行中完成。

## Codex MCP 配置

把下面配置放到 Codex 配置文件，例如：

```text
%USERPROFILE%\.codex\config.toml
```

```toml
[mcp_servers.altium]
command = 'python'
args = ['C:\path\to\mcp-codex-altium-designer\server.py']
cwd = 'C:\path\to\mcp-codex-altium-designer'
startup_timeout_sec = 20
tool_timeout_sec = 120
default_tools_approval_mode = 'prompt'
```

修改 `server.py` 或新增 MCP 工具后，需要重启 Codex 或重新加载 MCP server。

克隆到本机后，还需要把 `bridge/AltiumMCPBridge.pas` 顶部的 `C:\path\to\mcp-codex-altium-designer` 替换为实际仓库路径，保证 Altium 侧脚本和 Python MCP server 使用同一个 `shared/` 目录。

## Altium 侧启动

在 Altium Designer 中打开脚本项目：

```text
C:\path\to\mcp-codex-altium-designer\bridge\AltiumMCPBridge.PrjScr
```

运行过程：

```text
StartMCPBridge
```

脚本会读取：

```text
C:\path\to\mcp-codex-altium-designer\shared\request.json
```

并写回：

```text
C:\path\to\mcp-codex-altium-designer\shared\response.json
```

## 可用工具

- `altium_bridge_status`: 查看 shared 文件和 heartbeat 状态，不调用 Altium。
- `altium_ping`: 测试 Altium bridge 是否能响应。
- `altium_get_active_document`: 返回 Altium 当前聚焦文档。
- `altium_list_workspace_documents`: 列出 Altium workspace 中的项目和文档。
- `altium_find_output_jobs`: 查找 workspace 中的 OutJob/BOM 输出文档。
- `altium_export_output_jobs_report`: 导出输出作业/生产文档清单。
- `altium_list_pcb_components`: 读取 PCB 元件位号、comment 和 footprint。
- `altium_list_pcb_nets`: 读取 PCB 网络名、引脚数、过孔数、布线长度和连通状态标志。
- `altium_check_pcb_nets`: 检查 PCB 空网络名、0 引脚网络、1 引脚网络 review 项和连通异常标志。
- `altium_export_pcb_net_list`: 导出 PCB 网络清单。
- `altium_export_pcb_net_report`: 导出 PCB 网络检查报告。
- `altium_list_sch_components`: 读取原理图元件位号、comment 和元件参数。
- `altium_compare_sch_pcb_components`: 比较 SCH 和 PCB 元件一致性。
- `altium_export_sch_pcb_comparison_report`: 将 SCH/PCB 对比结果导出为 `exports/` 目录下的 CSV 或 JSON 文件。
- `altium_generate_simple_bom`: 从原理图或 PCB 生成按 comment 分组的简易 BOM。
- `altium_export_simple_bom`: 将简易 BOM 导出为 `exports/` 目录下的 CSV 或 JSON 文件。
- `altium_export_bom_with_fields`: 将 BOM 按 comment 加指定字段/参数分组并导出。
- `altium_export_component_list`: 导出原理图、PCB 或两者合并的扁平元件清单。
- `altium_export_component_parameters`: 导出元件参数明细表。
- `altium_check_component_designators`: 检查原理图/PCB 的空位号、未编号位号和重复位号。
- `altium_suggest_schematic_designator_fixes`: 为原理图未编号位号生成安全的下一编号建议，不修改工程。
- `altium_apply_schematic_designator_fixes`: 将已确认的位号建议写入当前原理图；必须 `confirm=true`，不会自动保存工程。
- `altium_export_designator_report`: 将位号检查报告导出为 `exports/` 目录下的 CSV 或 JSON 文件。
- `altium_check_component_fields`: 检查元件字段或原理图参数是否为空。
- `altium_export_component_field_report`: 将字段/参数检查报告导出为 `exports/` 目录下的 CSV 或 JSON 文件。
- `altium_update_schematic_parameters`: 按位号和参数名更新已有原理图参数；必须 `confirm=true`，不会自动创建缺失参数或自动保存工程。
- `altium_prepare_output_generation`: 检查 OutJob 是否就绪并生成生产输出前置计划，不运行输出。
- `altium_project_health_check`: 运行只读项目健康检查，组合位号、SCH/PCB 对比、字段、PCB 网络、BOM 预览和 OutJob 就绪度。
- `altium_export_project_health_report`: 将项目健康检查报告导出为 CSV 或 JSON。
- `altium_run_project_validation`: 触发 Altium 项目验证/ERC；必须 `confirm=true`。
- `altium_open_pcb_drc_dialog`: 打开 PCB DRC 对话框；必须 `confirm=true`，不自动点击运行。
- `altium_run_active_output_container`: 对当前 OutJob 中选中的输出容器生成输出；必须 `confirm=true`。
- `altium_stop_bridge`: 创建停止文件，让运行中的 bridge 尽快退出。

## 本地测试

验证 MCP server：

```powershell
python scripts\mcp_smoke_test.py
```

调用单个工具：

```powershell
python scripts\mcp_call_tool.py altium_generate_simple_bom --source schematic --timeout-seconds 60
```

PCB BOM：

```powershell
python scripts\mcp_call_tool.py altium_generate_simple_bom --source pcb --timeout-seconds 60
```

导出原理图 BOM 为 CSV：

```powershell
python scripts\mcp_call_tool.py altium_export_simple_bom --source schematic --format csv --filename schematic_bom.csv --timeout-seconds 60
```

导出 PCB BOM 为 JSON：

```powershell
python scripts\mcp_call_tool.py altium_export_simple_bom --source pcb --format json --filename pcb_bom.json --timeout-seconds 60
```

检查原理图和 PCB 位号：

```powershell
python scripts\mcp_call_tool.py altium_check_component_designators --source both --timeout-seconds 60
```

导出位号检查报告：

```powershell
python scripts\mcp_call_tool.py altium_export_designator_report --source both --format csv --filename designator_report.csv --timeout-seconds 60
```

检查 PCB 元件 `footprint` 字段：

```powershell
python scripts\mcp_call_tool.py altium_check_component_fields --source pcb --required-field footprint --timeout-seconds 60
```

导出 PCB 元件字段检查报告：

```powershell
python scripts\mcp_call_tool.py altium_export_component_field_report --source pcb --required-field footprint --format csv --filename component_field_report.csv --timeout-seconds 60
```

导出原理图和 PCB 合并元件清单：

```powershell
python scripts\mcp_call_tool.py altium_export_component_list --source both --format csv --filename component_list.csv --timeout-seconds 60
```

导出 SCH/PCB 对比报告：

```powershell
python scripts\mcp_call_tool.py altium_export_sch_pcb_comparison_report --format csv --filename sch_pcb_comparison.csv --timeout-seconds 60
```

导出原理图元件参数明细：

```powershell
python scripts\mcp_call_tool.py altium_export_component_parameters --source schematic --format csv --filename schematic_parameters.csv --timeout-seconds 60
```

检查原理图中某个参数是否填写；参数名有空格时需要加英文引号：

```powershell
python scripts\mcp_call_tool.py altium_check_component_fields --source schematic --required-field "Manufacturer Part Number" --timeout-seconds 60
```

导出带额外字段的 PCB BOM：

```powershell
python scripts\mcp_call_tool.py altium_export_bom_with_fields --source pcb --include-field footprint --format csv --filename pcb_bom_with_footprint.csv --timeout-seconds 60
```

检查 PCB 网络：

```powershell
python scripts\mcp_call_tool.py altium_check_pcb_nets --timeout-seconds 60
```

如果所有网络都返回 `connectively_invalid=true`，工具会把它作为全局连通缓存/标志 review 项，而不是按每个网络都算一个硬错误。

导出 PCB 网络检查报告：

```powershell
python scripts\mcp_call_tool.py altium_export_pcb_net_report --format csv --filename pcb_net_report.csv --timeout-seconds 60
```

查找 OutJob/BOM 输出文档：

```powershell
python scripts\mcp_call_tool.py altium_find_output_jobs --timeout-seconds 60
```

导出输出作业清单：

```powershell
python scripts\mcp_call_tool.py altium_export_output_jobs_report --format csv --filename output_jobs.csv --timeout-seconds 60
```

生成位号修复建议：

```powershell
python scripts\mcp_call_tool.py altium_suggest_schematic_designator_fixes --timeout-seconds 60
```

预览应用位号修复；不带 `--confirm` 时不会写入：

```powershell
python scripts\mcp_call_tool.py altium_apply_schematic_designator_fixes --timeout-seconds 60
```

确认写入位号修复；写入后请在 Altium 里检查，再手动保存：

```powershell
python scripts\mcp_call_tool.py altium_apply_schematic_designator_fixes --confirm --timeout-seconds 60
```

预览更新已有原理图参数；不带 `--confirm` 时不会写入：

```powershell
python scripts\mcp_call_tool.py altium_update_schematic_parameters --arguments-json '{"updates":[{"designator":"R1","parameter":"Supplier","value":"LCSC"}]}' --timeout-seconds 60
```

确认更新已有原理图参数；写入后请在 Altium 里检查，再手动保存：

```powershell
python scripts\mcp_call_tool.py altium_update_schematic_parameters --arguments-json '{"updates":[{"designator":"R1","parameter":"Supplier","value":"LCSC"}]}' --confirm --timeout-seconds 60
```

检查是否已有可用于 Gerber/Pick-and-Place 等生产输出的 OutJob：

```powershell
python scripts\mcp_call_tool.py altium_prepare_output_generation --timeout-seconds 60
```

Release-preflight additions:

```powershell
python scripts\mcp_call_tool.py altium_project_health_check --timeout-seconds 60
python scripts\mcp_call_tool.py altium_export_project_health_report --format json --filename project_health.json --timeout-seconds 60
python scripts\mcp_call_tool.py altium_run_project_validation --confirm --timeout-seconds 120
python scripts\mcp_call_tool.py altium_open_pcb_drc_dialog --confirm --timeout-seconds 60
python scripts\mcp_call_tool.py altium_run_active_output_container --mode folder_structure --confirm --timeout-seconds 180
```

## 发布说明

仓库默认忽略 `shared/*.json`、`shared/operations/`、`exports/` 和 `bridge/History/`，这些通常包含本机运行状态、BOM/网络/元件导出结果或 Altium 自动备份，不建议提交到公开仓库。

当前仓库还没有许可证文件。如果准备公开开源，建议发布前选择并添加一个明确的许可证，例如 MIT、Apache-2.0 或 GPL 系列。

## 下一步可扩展

- 自动运行 OutJob 或导出 Gerber/Pick-and-Place。
- 自动创建缺失原理图参数。

写操作建议继续保持工具审批模式为 `prompt`，并在工具描述中明确风险。
