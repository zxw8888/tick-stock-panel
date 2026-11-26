# InStock 指标移植

从 `E:\project\stock`（InStock）移植 130 个技术指标到本项目，实现为纯 Polars +
numpy，**不引入 TA-Lib 的 C 依赖**。

## 模块

`backend/app/indicators/extended.py`

| 符号 | 说明 |
|---|---|
| `EXTENDED_INDICATORS` | 133 个列名 -> 中文说明（含 `cr-ma*` 连字符别名） |
| `EXTENDED_COLUMNS` | 全部列名元组 |
| `EXTENDED_INPUT_COLUMNS` | 84 个可作为策略输入列的指标（排除纯中间量） |
| `EXTENDED_BY_CATEGORY` | 前端字段选择器的分组 |
| `WARMUP_BARS` | 每列所需最小历史根数（回测 warmup / 前端提示） |
| `BASE_INPUTS_BY_COLUMN` | 每列需要的原始 OHLCV 列（`amount` 依赖） |
| `CORE_DEPS` | 复用的核心指标：`prev_close/ma10/ma20/atr_14/rsi_14/change_pct/vol_ma5/vol_ma10` |
| `compute_extended(df, needed=None, *, assume_sorted=False, fill_warmup_zero=False)` | 主入口 |

## 接入方式

扩展指标**默认不计算**，只在显式请求时生效：

```python
from app.indicators.pipeline import compute_indicators

# 默认路径：零行为变更，不产生任何扩展列
df = compute_indicators(raw)

# 按需计算：自动补齐 CORE_DEPS 闭包
df = compute_indicators(raw, needed={"supertrend", "cr", "mfi"})
```

回测/筛选/策略走 `needed` 裁剪，因此策略里写 `pl.col("cr")` 时，依赖解析会把
`cr` 认成合法指标列，回测自动加载并计算。

## 口径差异（重要）

移植不是逐字翻译，以下几处与 A 股常见实现不同，都按 InStock 原版：

| 项 | 口径 | 说明 |
|---|---|---|
| `macdh` | `DIF - DEA`，**不乘 2** | 与 panel 的 `macd_hist`（×2）不同 |
| `boll_ub/boll_lb` | 标准差 **ddof=0** | TA-Lib BBANDS 用总体标准差；panel 的 `boll_upper` 用 ddof=1 |
| `kdjk/kdjd` | `alpha=1/3` 的 EMA（TA-Lib STOCH matype=1） | 不是常见的 SMA(3) |
| `rsi` | Wilder 平滑 | 等价于 panel 的 `rsi_14` |
| `atr` | Wilder 平滑 | 等价于 panel 的 `atr_14` |
| `obv` | 首根 = `volume[0]` | TA-Lib 语义，不是从 0 开始累加 |
| `wr_*` | `-100 ~ 0` | TA-Lib WILLR 的负值区间 |
| `pdi/mdi/dx/adx` | stockstats 公式 | 不是 TA-Lib `PLUS_DI`，`adxr` 是 `EMA(adx, 6)` 而非标准 ADXR |
| `br` | 可为负 | 跳空高开时 `cy_l = prev_close - low < 0`，InStock 原版同样为负 |
| `cr-ma1/2/3` | 连字符列名 | Polars 表达式里会被解析成减法，另提供 `cr_ma1/2/3` 别名 |

## warmup 语义

- `fill_warmup_zero=False`（默认）：窗口不足保持 `null`，与本仓既有 30 个指标一致，
  避免"数据不足"被当成"值为 0"产生假信号。
- `fill_warmup_zero=True`：复刻 InStock 语义（NaN/Inf 一律写 0.0）。pipeline 内部
  走这个模式，因为 InStock 的选股公式是按 0 假设写的。

## 验证

`backend/tests/test_extended_indicators.py`（27 个用例）覆盖数值口径、多标的隔离、
乱序输入、缺失 `amount`、零成交额、warmup、pipeline 集成。

与 InStock TA-Lib 实现的逐列对照（合成 400 根 K 线 × 2 标的，取稳态最后 60 根）：

| 结果 | 数量 |
|---|---|
| 逐列一致（差异 ≤ 1e-9，浮点噪声量级） | 128 / 133 |
| 合成数据无平盘日，`cv/cvs` 无可比行 | 2 |
| 仅 `cr_ma1/2/3` 别名（InStock 无此列名） | 3 |

复现脚本：`E:\project\stock\_tmp\verify_extended.py`。

## 性能

5000 标的 × 300 根（150 万行）：

| 场景 | 耗时 |
|---|---|
| 仅核心指标 | 4.2s |
| 核心 + 10 个扩展列 | 7.2s |
| 核心 + 全部 84 个输入列 | 8.9s |

`sar` / `supertrend` 是 numpy 递推（状态机无法向量化），按 symbol 分段执行；
其余指标全部走 Polars 窗口表达式。
