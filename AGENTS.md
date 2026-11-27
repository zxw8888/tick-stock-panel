# AI 开发入口

修改、调试或审查本仓库前，必须完整阅读并遵循根目录的 [`CONTRIBUTING.md`](CONTRIBUTING.md)。其中定义了项目架构、数据契约、数据源插件化、缓存与性能要求、测试矩阵以及 PR 复审和合并标准。

涉及代码二次开发、前端插槽、后端可替换策略、扩展注册或上游升级兼容时，还必须阅读 [`docs/secondary-development.md`](docs/secondary-development.md)。该文档区分当前已实现能力与目标扩展契约；不得根据设计示例虚构尚不存在的 API。

同时遵守以下规则：

- 先理解调用链和现有测试，再进行修改。
- 保持实现简单、改动范围最小，不处理无关问题。
- 不覆盖工作区已有修改，不虚构测试或审查结果。
- 以实际验证结果作为完成标准。

## 按任务找现行契约

- 对接已部署的 TSP、选择 MCP 或调用 Open API，先读 [AI 接入指南](docs/ai-integration.md)。
- 编写可导入的配置、指标、资源或插件，先查 [文档导航](docs/README.md) 和 [统一导入](docs/extension-packages.md)，再读对应契约。
- 运行策略回测或引擎研究，先核对 [回测证据](docs/backtest-evidence.md) 与 [引擎研究](docs/engine-platform/workbench-research-jobs.md)。配置、包版本和验收结果按目标部署实际返回值记录。

---
# 本机工作空间协议

> 上一段是上游规则，管代码规范；这一段管本机怎么干活，两段都读。
> `.codex/`、`docs/knowledge/` 是未跟踪文件，忽略规则在 `.git/info/exclude`。

## 1. 这是什么

`D:\projects\github\tick-stock-panel` 是自改版 TSP（A 股量化面板）：产出**源码**（含 LOF 定制）与**桌面 EXE**（装在 `D:\software\TSP`）。
边界：不改上游仓库；本机脚手架不进 git；不代提交、不代推送，除非明确要求。

## 2. 路由

| 接到什么任务 | 先读什么 |
|---|---|
| 改后端 / 前端代码 | 上方上游规则 → `CONTRIBUTING.md` |
| 改数据口径、LOF 相关 | `docs/lof-fork-patches.md` → `.codex/rules/global-baseline.md` |
| 打包 EXE、升级桌面版 | `.codex/rules/build-and-package.md` |
| 同步上游 | `.codex/rules/upstream-sync.md` |
| 要沉淀新经验 | `.codex/rules/rule-evolution.md` |
| 想知道为什么这么做 | `docs/knowledge/README.md` |

## 3. 硬规则

1. `D:\software\TSP\data` 是唯一真实数据；覆盖安装只动 `TSP.exe` 和 `_internal`。
2. dev 模式与 EXE 都从 3018 起，不同时开。
3. EXE 是冻结包，改完代码必须重打包才算交付。
4. 打包前必须 `cd frontend; pnpm build`。
5. `.codex/`、`docs/knowledge/`、`sync-upstream.ps1` 不进 git。
6. 打包脚本不许用 `packaging/build_release.py`（路径写死在别的机器）。

## 4. 知识在哪

- 硬规则/经验：`.codex/rules/*.md`（开工前、遇到问题）；过程：`docs/knowledge/`（索引 `README.md`）、外部库 `D:\notes`（想理解/改结构）；技能：`.codex/skills/`（当前只有候选池，留空是刻意的）。
- 体检：`python .codex/scripts/workspace_audit.py`（改完脚手架/收尾）。

## 5. 怎么进化

- 新经验先过四闸：可复用、有验证、非显然、可执行。能照做 → `.codex/rules/`；要理解 → `docs/knowledge/`。
- 推翻旧条目直接删，并在 `docs/knowledge/README.md` 的修正记录留一行（写触发原因，不写「优化」）。
- 技能：同类任务第 2 次才建，先进 `.codex/skills/候选池.md`；改过体检脚本必须跑 `--selftest`，全部注入用例都要报警。
