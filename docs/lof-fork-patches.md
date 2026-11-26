# TSP LOF 支持 — fork 补丁清单

> 本 fork 基于上游 `shy3130/tick-stock-panel` v0.3.37（commit f9cb383c），新增 LOF 作为一等公民资产类型。
> 上游**没有**新增资产类型的扩展点（官方文档 L1/L2/L3 分级中属 L3 改核心），因此每次 upstream 升级需按本清单核对冲突。

## 为什么必须改核心

- TickFlow（上游主数据源）免费档实测：`instrument_type="fund"` 返回 0 条、`"lof"` 直接 400、LOF 代码日K空。
- fuyao（同花顺）官方 API：维表 `asset_type=fund-lof`、场内基金快照明确支持 ETF+LOF、NAV 有独立端点；但**场内历史日线明确"仅支持 ETF"**。
- 雪球 `v5/stock/chart/kline.json` 实测覆盖沪深 LOF 日线（SZ160216 / SH501060 / SH502010 / SZ161725），
  自带 volume/amount/涨跌幅，可作主源。
- 结论：LOF 日K 走 **雪球 → 东财 push2his** 回退；维表/NAV 优先 fuyao（需 `FUYAO_API_KEY`），
  无 Key 时维表回退东财基金总表代码段；实时走 **fuyao → 腾讯 qt.gtimg.cn → 东财 push2**。

## 数据源与单位口径（改代码前必读）

| 数据 | 源 | 单位/口径 |
|---|---|---|
| 维表 | fuyao `fund-lof` → 东财基金总表代码段兜底 | 深 16x / 沪 501,502,505,506；排除 508(REITs)、51x/15x(ETF) |
| 日K | 雪球 `kline.json` 主源 → 东财 push2his `klt=101&fqt=0` 兜底 | 雪球 **volume 是股 → /100 折手**；东财 **volume 已是手**；东财 klines 列序 `日期,开,收,高,低,量,额,振幅`（收在高之前） |
| 实时 | fuyao `/api/fund/market/snapshot` → 腾讯 `qt.gtimg.cn` → 东财 push2 | 腾讯 volume 已是手、amount 万元→元、换手率 %→小数；fuyao `change_pct` 百分数→小数 `/100`；无 CN_LOF universe，按码拉 |
| NAV | fuyao `/api/fund/performance/nav` | `unit_nav`/`adj_nav`，T+1 披露，按净值日分区 |

### 雪球会话注意

`https://xueqiu.com/` 会 301 到 `https://www.xueqiu.com/`，`xq_a_token` 只在**第二跳**的
Set-Cookie 里。httpx 默认跟随跳转时该 cookie 会被吞掉，接口返回
`400016 遇到错误，请刷新页面或者重新登录帐号后再试`。
`lof_data._xueqiu_session()` 用 `follow_redirects=False` 手动走两跳解决。

## 改动文件清单（rebase 核对表）

### 新增文件
- `backend/app/asset_types.py` — 资产类型集中常量（全集/基金类/instrument_type 映射）
- `backend/app/services/lof_data.py` — LOF 维表/日K/实时/NAV 数据访问层
- `backend/app/services/lof_nav_service.py` — NAV 扩展表建表与同步
- `backend/tests/test_lof_sync.py` — 12 个单测（含雪球日K/腾讯快照口径与回退顺序）

### 后端改动
| 文件 | 改动摘要 |
|---|---|
| `data_providers/base.py` | `AssetType` Literal 加 lof |
| `data_providers/tickflow_provider.py` | lof→fund instrument_type 映射（预留，当前不走 TickFlow） |
| `services/index_sync.py` | `sync_lof_instruments` / `sync_and_persist_lof_daily` |
| `services/kline_sync.py` | 日K准入集合加 lof（分钟保持 stock/etf） |
| `services/preferences.py` | `pipeline_pull_lof` / `realtime_pull_lof` 默认 False |
| `services/quote_service.py` | 分类/按码拉取/落盘/enriched table 映射加 lof（两条数据源分支都要有） |
| `services/data_integrity.py` / `daily_health.py` / `integrity_check.py` | 日K表族与中文标签加 lof |
| `tickflow/repository.py` | 目录映射、视图、缓存、读写方法全链路加 lof（15 处） |
| `jobs/daily_pipeline.py` | Step2.4 独立 try 的 LOF 分支；20:30 NAV job；stale 判定与视图路径 |
| `api/data.py` | status 聚合、schema、清库资产集合加 lof |
| `api/settings.py` | 偏好模型加 lof 字段 |
| `api/ext_data.py` | `POST /api/ext-data/lof-nav/sync` 手动 NAV 同步端点 |

### 前端改动
- `src/lib/api.ts` — asset_type 联合类型、`Preferences`、`DataStatus` 加 lof
- `src/components/data/PipelineScopeConfig.tsx` — LOF 拉取开关
- `src/components/data/PageSettingsModal.tsx` — CardKey + DATA_CARD_DEFS 加 lof
- `src/pages/Data.tsx` — LOF 统计/卡片/自检默认资产
- 8 个图表/展示组件的 `assetType` 联合类型加 lof

## 上游升级 rebase 步骤

1. `git fetch upstream && git rebase upstream/main`
2. 逐个解决冲突 —— **优先保留上游对 ETF/stock 逻辑的修改**，只在 ETF 分支旁平行加 lof 分支
3. 跑 `pytest tests/test_lof_sync.py tests/test_etf_adj_factor.py tests/test_repository_index.py -q`
4. 跑 `cd frontend && pnpm build`（tsc 类型检查会抓出新增联合类型的遗漏点）
5. 若上游新增了资产类型枚举位置，先加到 `asset_types.py` 再逐点接入

## 已知限制 / 待办

- LOF **无历史复权因子源**：compute_enriched 使用不复权价，跨分红日的历史指标可能有跳变（ETF 有 adj_factor_etf，LOF 暂无）。
- LOF **无分钟K**：`kline_sync` 分钟准入保持 stock/etf；本轮只做日K + 实时快照。
  （`freetdx` 插件的 `iter_minute` 支持 `asset_type in (stock, etf, index)`，加 `lof` 即可用通达信分时转 1m，
  但分时转出的 OHLC 相等、无影线，且基金价格需按 10 倍换算，未纳入本轮。）
- **通达信日K 通道已不可用**：2026-11 实测 `get_security_bars` / `get_index_bars` 对所有周期返回拒绝码，
  只有 `get_history_minute_time_data` / `get_minute_time_data` / `get_finance_info` / `get_xdxr_info` 可用。
  因此 TDX 只能补分钟与财务，不能替代日K。
- LOF 实时**付费档未实测**：本机无 `TICKFLOW_API_KEY` 与 `FUYAO_API_KEY`；fuyao 路径按官方文档契约实现，配 Key 后需验证 `/api/fund/market/snapshot` 的 volume 单位（代码里暂按 fuyao A股口径 /100，若实测为手需改 `lof_data._map_fund_snapshot`）。
- 东财 16 段含少量 B 份额/后端份额（如 160107 南方高增长后端），维表有约 610 只，比实际可交易 LOF 略多；接 fuyao 官方清单后自动收敛。
- NAV 自动同步已注册 20:30 工作日 job，但无 Key 时静默跳过；用户需自行配置 `FUYAO_API_KEY`。
