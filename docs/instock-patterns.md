# InStock 形态 + 筹码分布移植

从 `E:\project\stock`（InStock）移植 **61 个 TA-Lib 蜡烛形态**和**筹码分布 (CYQ)**，
实现为纯 numpy，**不引入 TA-Lib 的 C 扩展**。

## 模块

### 蜡烛形态 — `backend/app/indicators/patterns.py`

| 符号 | 说明 |
|---|---|
| `PATTERNS` | 61 个列名 -> 计算函数 `fn(open, high, low, close) -> np.int64[]` |
| `PATTERN_LABELS` | 列名 -> 中文名（与 InStock `tablestructure.py` 逐字一致） |
| `TALIB_NAMES` | 列名 -> TA-Lib 函数名（`CDL2CROWS` ...） |
| `PATTERN_LOOKBACK` | 列名 -> 所需最小历史根数 |
| `PATTERN_COLUMNS` | 全部 61 个列名（有序元组） |
| `compute_patterns(open_, high, low, close, needed=None)` | 纯 numpy 入口 |
| `compute_patterns_frame(df, needed=None, *, assume_sorted=False)` | Polars 入口，按 symbol 分段 |

取值：`-100`（看跌）/ `0`（无信号）/ `+100`（看涨）。
`hikkake_pattern` 在"陷阱确认"后会叠加到 `±200`，这是 TA-Lib 的原语义，不是 bug。

### 筹码分布 — `backend/app/indicators/chips.py`

| 符号 | 说明 |
|---|---|
| `compute_cyq(*, open_, close, high, low, turnover, dates=None, index=None, accuracy_factor=150, crange=120, cyq_days=210)` | 核心实现 |
| `compute_cyq_frame(df, *, index=None, ...)` | 从单标的 Polars DataFrame 直接算 |

返回 `{x, y, b, d, t, benefit_part, avg_cost, percent_chips}`：

- `x` 每档筹码量，`y` 价格档位（长度 = `accuracy_factor`）
- `b` 当前价所在档位，`benefit_part` 获利盘比例
- `avg_cost` 平均成本，`percent_chips` 的 `90` / `70` 给成本区间与集中度

**按需调用**：单次计算是 `O(根数 × 档位)` 的 Python 循环，只适合前端点选某根
K 线时调用，不要放进逐行指标管线。

## 接入方式

形态列和扩展指标一样，**默认不计算**，只在 `needed` 里显式请求时生效：

```python
from app.indicators.pipeline import compute_indicators

# 默认路径：零行为变更，不产生任何形态列
df = compute_indicators(raw)

# 按需计算
df = compute_indicators(raw, needed={"doji", "hammer", "engulfing_pattern"})

# 全部 61 列
from app.indicators.patterns import PATTERN_COLUMNS
df = compute_indicators(raw, needed=set(PATTERN_COLUMNS))
```

形态列已注册进 `ENRICHED_COLUMNS`（后缀 `(K线形态)`）、
`ENRICHED_COLUMNS_BY_CATEGORY["pattern"]` 和 `INDICATOR_COLUMNS`，
策略里可以直接写 `pl.col("hammer") > 0`。

## API

### `GET /api/kline/patterns/meta`

返回全部 61 个形态的 `{key, label}`，前端形态选择器直接用它渲染中文名。

### `GET /api/kline/patterns`

| 参数 | 说明 |
|---|---|
| `symbol` | 标的代码，如 `600000.SH` |
| `start_date` / `end_date` | 查询区间（`YYYY-MM-DD`） |
| `patterns` | 可选，逗号分隔的形态列名；缺省 = 全部 61 个 |

只返回命中行：`{date, <pattern_key>: ±100/±200, ...}`。同一天可命中多个形态。

### `GET /api/kline/chips`

筹码分布（CYQ），按需计算，不参与 `/daily` 热路径。

| 参数 | 默认 | 说明 |
|---|---|---|
| `symbol` | 必填 | 标的代码 |
| `date` | 区间最后一根 | 目标交易日；不在本地日 K 区间内返回空 `chips` + `detail` |
| `start_date` / `end_date` | 按 `cyq_days` 推算 | 计算区间；反向区间返回 400 |
| `accuracy_factor` | `150` | 价格档位数（20–500） |
| `cyq_days` | `210` | 参与衰减的交易日数（30–500） |

返回：

```json
{
  "symbol": "600000.SH",
  "asset_type": "stock",
  "date": "2026-11-25",
  "turnover_source": "turnover_rate",
  "chips": {
    "x": [0.1, 0.2],
    "y": [8.11, 8.13],
    "b": 128,
    "d": "2026-11-25",
    "t": 210,
    "benefit_part": 0.953,
    "avg_cost": "9.30",
    "percent_chips": {
      "90": {"priceRange": ["8.52", "10.66"], "concentration": 0.111},
      "70": {"priceRange": ["8.80", "10.20"], "concentration": 0.073}
    }
  }
}
```

换手率口径：

- 股票：使用 enriched 的 `turnover_rate`，`turnover_source = "turnover_rate"`。
- ETF / LOF / 指数：没有换手率列，用「成交量 / 窗口内最大成交量 × 100」代理，
  `turnover_source = "volume_proxy"`。形状可用于比较，但绝对刻度不代表真实换手率。
- 连 `volume` 也没有：`turnover_source = "unavailable"`，`chips = {}`。

## 前端入口

### 自选 / 个股日 K 弹窗

`StockDailyKChart` 工具栏：

- **形态**：打开 61 形态多选面板；选中后在 K 线上标注命中形态。
- **筹码**：在图表右侧打开 CYQ 筹码分布；点任意 K 线切换该交易日筹码。

两个开关和已选形态都会写入 `localStorage`，下次打开保持。

### `/stock-analysis` 个股分析页

「关键价位分析」标题右侧：

- **形态**：显示/隐藏形态标注，并打开同一个 61 形态选择器。
- **筹码**：显示/隐藏右侧筹码面板；点 K 线切换日期。

`/stock-analysis` 的图表默认显示约 120 根 K 线，处于 compact 模式；
形态标签已设置为 compact 下仍显示，其他普通 annotation 标签仍按原规则隐藏。

## 口径（重要）

判定条件逐条转译自 TA-Lib 0.6.x 官方 C 源码 `src/ta_func/ta_CDL*.c`，
不是凭形态学描述重写。蜡烛尺度定义（`src/ta_common/ta_global.c`）：

| 常数 | 含义 |
|---|---|
| `BodyLong` | 实体 > 1.0 × 前 10 根实体均值 |
| `BodyVeryLong` | 实体 > 3.0 × 前 10 根实体均值 |
| `BodyShort` | 实体 < 1.0 × 前 10 根实体均值 |
| `BodyDoji` | 实体 ≤ 0.1 × 前 10 根 high-low 均值 |
| `ShadowLong` | 影线 > 1.0 × **当根**实体（avgPeriod=0，无均值） |
| `ShadowVeryLong` | 影线 > 2.0 × 当根实体 |
| `ShadowShort` | 影线 < 1.0 × 前 10 根影线和均值 ÷ 2 |
| `ShadowVeryShort` | 影线 < 0.1 × 前 10 根 high-low 均值 |
| `Near` | ≤ 0.2 × 前 5 根 high-low 均值 |
| `Far` | ≥ 0.6 × 前 5 根 high-low 均值 |
| `Equal` | ≤ 0.05 × 前 5 根 high-low 均值 |

所有均值都**不含当根**。数据不足 `PATTERN_LOOKBACK` 的位置输出 `0`。

`optInPenetration` 用 TA-Lib 默认值：弃婴 / 暮星 / 晨星系列 `0.3`，
乌云压顶 / 铺垫 `0.5`。

## 验证

`backend/tests/test_patterns.py`（18 个用例）覆盖列齐全、取值域、warmup、
多标的隔离、`needed` 裁剪、空表/超短/平盘边界。

`backend/tests/test_cyq.py`（17 个用例）覆盖输出结构、价格轴单调、筹码非负、
获利盘与成本区间关系、窗口参数、一字板与零换手。

### 与 TA-Lib 逐值对拍

| 场景 | 结果 |
|---|---|
| 61 形态 × 40 标的 × 160 根（5 种走势风格） | **61/61 完全一致** |
| 61 形态 × 30 标的 × 5 个随机种子 × {80, 300} 根 | **0 处差异** |
| 3 标的乱序输入（pipeline 集成） | 183 个"标的×形态"组合 **0 处差异** |
| 边界：1/2/3/5/10/11/13/15 根、全平盘、零振幅、1e6 价格量级 | 全部一致 |

筹码分布与 InStock `CYQCalculator` 逐位对拍：

| 场景 | 结果 |
|---|---|
| 5 个可计算位点（x / y / b / benefit_part / avg_cost / percent_chips 90,70） | **5/5 逐位相同** |
| 6 个位点 | InStock 自身抛 `IndexError`；本实现返回 `{}` |

复现脚本：`E:\project\stock\_port\harness.py`、`verify_cyq.py`。

## 性能

单标的 2000 根：

| 场景 | 耗时 |
|---|---|
| 全部 61 列 | 18.5 ms |
| 3 列（`doji` / `hammer` / `engulfing_pattern`） | 0.5 ms |

`hikkake` / `hikkake_mod` / `tristar` / `kicking_bylength` 是状态机，按 symbol
分段 Python 循环；其余 57 个全部向量化。
