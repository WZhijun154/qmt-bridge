# 回测数据指南

本文档基于对当前部署环境的**实测结果**（直接调用 `xtquant.xtdata`，逐个隔离进程验证），
说明回测场景下各端点是否真的可用、能拿到什么数据、以及推荐的取数流程。

!!! warning "结果与 miniQMT 客户端版本相关"
    下文"⚠️ 客户端不支持"的接口是因为当前安装的 miniQMT 客户端版本未实现对应的底层 RPC
    （`RuntimeError ... ErrorID 300000 "function not realize"`），不是 qmt-bridge 的代码问题。
    换一台/ 升级 miniQMT 客户端后，这些接口可能变为可用。这些端点已做了防御性处理，
    调用失败时返回 `{"error": "..."}` 而不是 500，可以据此判断某台机器是否支持。

## 回测取数推荐流程

1. **先下载，再查询**：回测需要的历史数据应先用 `/api/download/*` 端点批量下载到服务端本地缓存，
   再用 `/api/market/local_data` 等只读端点批量读取——避免在回测循环里反复触发网络请求。
2. **用 `local_data` 而不是 `market_data_ex` 做批量读取**：`get_market_data_ex` 在数据缺失时会
   自动向行情服务器发起网络请求（这也是 CLAUDE.md 中记录的 BSON 崩溃已知问题的高发点之一）。
   本地数据已下载完整后，优先用 `/api/market/local_data`（`get_local_data`，纯本地读取，不触发网络）。
3. **用 `/api/calendar/trading_dates` 驱动回测日期循环**，而不是自己拼日历。
4. **用 `/api/sector/stocks` 获取股票池**，注意历史成分股需要传 `real_timetag` 处理生存偏差；
   结合 `/api/instrument/his_st_data`（历史 ST 标记）和 `/api/instrument/ipo_info`（新股上市日期）
   排除幸存者偏差。
5. **除权因子单独取**：K 线数据本身的 `dividend_type` 参数可选前复权/后复权，但如果需要自己算，
   `/api/market/divid_factors` 单独提供原始除权因子表。

---

## ✅ 核心可用数据（已实测成功）

### 历史 K 线 / 行情

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/market/market_data_ex` | 增强版历史 K 线（DataFrame 转记录格式），支持除权类型、时间范围、count |
| GET | `/api/market/local_data` | **仅读本地缓存**，不触发网络请求，回测批量读取首选 |
| GET | `/api/market/market_data` | 原始 `get_market_data` 接口，字段可自选 |
| GET | `/api/market/market_data3` | 同上，返回 `{股票: DataFrame}` 结构 |
| GET | `/api/market/full_tick` | 实时全推快照（当前价，非历史） |
| GET | `/api/market/indices` | 主要指数实时快照 |
| GET | `/api/market/divid_factors` | 除权因子（分红、送股、配股、除权比例 dr 等），实测有真实数据返回 |
| GET | `/api/history`、`/api/batch_history` | 旧版单只/批量历史 K 线，功能等价于 `market_data`，新代码建议用新版 |

### 财务数据

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/financial/data` | 按报表名查询（Balance/Income/CashFlow 等），返回结构化记录 |
| GET | `/api/financial/data_ori` | 原始格式财务数据 |
| GET | `/api/tabular/data` | 底层与 `financial/data` 相同（`get_financial_data` 的表格视角） |

### 交易日历

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/calendar/trading_dates` | 交易日列表，支持 market/时间范围/count |
| GET | `/api/calendar/holidays` | 节假日列表 |
| GET | `/api/calendar/is_trading_date` | 判断某天是否交易日 |
| GET | `/api/calendar/prev_trading_date` / `next_trading_date` | 前/后一交易日 |
| GET | `/api/calendar/trading_dates_count` | 区间交易日数量 |

### 股票池 / 合约信息（处理生存偏差的关键数据）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/sector/list` | 所有板块名称（用于发现可用分类） |
| GET | `/api/sector/stocks` | 板块成分股，支持 `real_timetag` 查历史某天的成分股 |
| GET | `/api/sector/info` | 板块元数据（需要 `pyarrow`，已随本次修复安装） |
| GET | `/api/instrument/detail_list` | 批量合约详情（名称、上市日期等） |
| GET | `/api/instrument/type` | 合约类型判断（股票/指数/基金等） |
| GET | `/api/instrument/his_st_data` | 历史 ST 标记时间段——排除 ST 股survivorship bias 用 |
| GET | `/api/instrument/index_weight` | 指数成分权重（需先 `download_index_weight`，否则可能为空） |
| GET | `/api/utility/stock_name` / `batch_stock_name` | 股票中文名 |
| GET | `/api/utility/code_to_market` | 代码归属市场 |
| GET | `/api/utility/search` | 按关键字搜索股票代码 |
| GET | `/api/bond/list`、`/api/bond/detail` | 债券列表/详情（不含可转债） |
| GET | `/api/cb/list`、`/api/cb/info` | 可转债列表/详情 |
| GET | `/api/etf/list`、`/api/etf/info` | ETF 列表/申赎信息 |

### 期货

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/futures/main_contract` | 主力合约（按持仓/成交量），期货连续合约回测必需 |
| GET | `/api/futures/sec_main_contract` | 次主力合约（部分品种可能返回 `None`，属正常） |

### 数据下载（回测前批量预取本地缓存）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/download/history_data2` | **批量下载历史 K 线**，逐只下载并规避了 xtquant 已知的挂起 bug，回测首选 |
| POST | `/api/download/financial_data` / `financial_data2` | 下载财务数据（v2 为同步阻塞版本） |
| POST | `/api/download/sector_data` | 下载全部板块成分数据（scheduler 每天自动跑一次） |
| POST | `/api/download/index_weight` | 下载指数成分权重（scheduler 每天自动跑一次） |
| POST | `/api/download/cb_data` | 下载可转债数据（scheduler 每天自动跑一次） |
| POST | `/api/download/history_contracts` | 下载历史（含已到期）期货/期权合约，实测耗时约 20s |
| POST | `/api/download` | 旧版单只下载，等价于 `history_data2` 的单股票版本 |

!!! tip "无需手动调用"
    标记"scheduler 每天自动跑一次"的下载端点由后台调度进程 `qmt-scheduler` 每 24 小时自动刷新，
    通常不需要手动触发，除非需要立即强制刷新。

---

## ⚠️ 当前 miniQMT 客户端不支持（返回 error 字段，非代码 bug）

以下端点已确认底层调用签名正确，但当前 miniQMT 客户端版本未实现对应功能，调用会得到
`{"error": "... function not realize ..."}`（HTTP 200，不会 500）：

| 路径 | 缺失功能 | 对回测的影响 |
|------|------|------|
| `/api/meta/period_list` | 支持的 K 线周期列表 | 无影响，周期是文档已知的固定集合（tick/1m/5m/15m/30m/60m/1d） |
| `/api/market/full_kline` | 单股票完整 K 线 | 用 `market_data_ex` / `local_data` 替代 |
| `/api/market/fullspeed_orderbook` | 极速委托簿 | 回测通常不需要盘口镜像 |
| `/api/market/transactioncount` | 逐笔成交计数 | 回测通常不需要 |
| `/api/calendar/trading_calendar` | 交易日历（日历视角） | 用 `trading_dates` 替代，功能等价 |
| `/api/calendar/trading_period` | 合约交易时段 | 日线级别回测通常不需要 |
| `/api/formula/list` | 公式列表 | 不影响标准数据回测 |
| `/api/option/his_option_list` | 历史期权合约列表 | 仅影响期权回测 |
| `/api/tabular/formula` | 公式表格查询 | 用 `/api/financial/data` 替代财务类需求 |
| `/api/download/holiday_data` | 节假日下载 | `/api/calendar/holidays` 直接读取即可，无需下载 |
| `/api/download/etf_info` | ETF 申赎信息下载 | 仅影响 ETF 申赎细节，不影响 ETF 价格数据 |
| `/api/download/metatable_data` | 合约元数据表下载 | **会影响期货合约识别**——若做期货回测且 `main_contract` 查不到数据，需要先确认此端点是否可用 |
| `/api/download/his_st_data` | 历史 ST 数据下载 | 改用 `/api/instrument/his_st_data` 直接查询（该接口可用） |
| `/api/download/tabular_data` | 表格数据下载 | 不影响标准财务/K线回测 |

---

## ❌ 明确排除 / 未测试

| 分类 | 端点 | 原因 |
|------|------|------|
| L2/付费数据 | `/api/tick/*` 全部（l2_quote、l2_order、l2_transaction、l2_thousand_*、broker_queue、order_rank）、`/api/hk/broker_dict` | 按用户要求跳过，属付费逐笔数据，日线/分钟线回测通常不需要 |
| 板块写操作 | `/api/sector/create*`、`add_stocks`、`remove_stocks`、`remove`、`reset` | 会修改服务端持久化配置，未在只读排查范围内测试；回测一般不需要 |
| 公式写操作 | `/api/formula/create`、`import`、`delete` | 同上，未测试 |
| 公式计算 | `/api/formula/call`、`call_batch`、`generate_index_data` | 需要真实存在的公式名称，无通用参数可安全测试；如果你有自定义技术指标公式可以单独验证 |
| 期权 | `/api/option/chain`、`/api/option/list` | 实测用示例标的报错（`TypeError`），可能是测试用的标的代码本身不是期权标的，未确认是否为真实 bug；用真实有期权挂钩的标的（如 510050.SH/510300.SH）重新验证 |
| 期权 | `/api/instrument/ipo_info` | 空时间范围查询会报"未找到相关数据"（`RuntimeError`），当前**未做容错**，传入具体时间范围应可正常工作 |
| WebSocket 订阅 | `subscribe_quote`、`subscribe_whole_quote`、`subscribe_formula` 相关 WS 端点 | 长连接接口，一次性脚本未测试，但用于实时行情推送而非回测历史数据，回测通常不需要 |

---

## 快速示例：拉取一只股票近一年日线 + 除权因子

```bash
# 1. 先下载到本地缓存（一次性，之后重复查询走本地缓存）
curl -X POST http://<host>:18888/api/download/history_data2 \
  -H "Content-Type: application/json" \
  -d '{"stocks": ["000001.SZ"], "period": "1d", "start_time": "20250101", "end_time": "20260709"}'

# 2. 批量读取本地缓存（不触发网络请求）
curl "http://<host>:18888/api/market/local_data?stocks=000001.SZ&period=1d&start_time=20250101&end_time=20260709&dividend_type=front"

# 3. 除权因子（如需自己处理复权逻辑）
curl "http://<host>:18888/api/market/divid_factors?stock=000001.SZ"

# 4. 交易日历（驱动回测日期循环）
curl "http://<host>:18888/api/calendar/trading_dates?market=SH&start_time=20250101&end_time=20260709"
```
