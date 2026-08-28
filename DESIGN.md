# 云龙挑战赛系统设计文档（YunLong Challenge System, YCS）

Version: 1.0
Status: Final

---

## 一、项目定位

云龙挑战赛（YCS）是一个运行于 Linux VPS 的 ETH 永续合约自动交易系统。

项目特点：

- 个人项目
- AI 辅助开发
- 单人维护
- 单交易所
- 单账户
- 单品种
- 单策略
- 长期实盘运行

交易所：**OKX**

交易品种：**ETH-USDT-SWAP**

项目目标：以挑战赛模式实现小资金成长。

参考路线：

```
2U
 ↓
10U
 ↓
50U
 ↓
100U
 ↓
500U
```

系统目标不是预测市场。系统目标是：

> 发现机会 · 控制风险 · 保住利润 · 长期存活

---

## 二、设计原则

### 1. 简单优先

拒绝过度设计。不支持：

- 多交易所
- 多账户
- 多品种
- 多策略

### 2. 风控优先

优先级：

```
风控 > 仓位管理 > 交易执行 > AI 分析
```

AI 永远没有最终决定权。

### 3. 交易所状态优先

任何时候：`OKX 状态 > 本地状态`

发生冲突：以交易所状态为准。

### 4. 自动恢复

VPS 重启后，系统必须自动恢复。

### 5. 利润保护

盈利单优先保住利润，禁止盈利单大幅回撤。

---

## 三、技术栈

| 类别 | 技术 |
| --- | --- |
| 开发语言 | Python 3.12 |
| Web 框架 | FastAPI |
| 交易所接口 | ccxt、ccxtpro |
| 数据模型 | Pydantic |
| 日志 | Loguru |
| 配置 | YAML |
| 数据存储 | JSON / JSONL |
| 包管理 | uv |
| 部署 | Ubuntu VPS + Systemd |

---

## 四、AI 架构设计

### AI 接口统一层

禁止代码直接依赖 `DeepSeekClient` 或 `OpenAIClient`。统一抽象：

```python
class AIProvider:
    async def analyze_market(self, market_data):
        pass
```

实现：

- `DeepSeekProvider`
- `OpenAIProvider`
- `ClaudeProvider`
- `GeminiProvider`
- `OpenRouterProvider`

系统调用：`provider.analyze_market()`，不关心底层模型。

### 推荐方案

使用 **LiteLLM** 作为统一 AI 网关。支持：DeepSeek、OpenAI、Claude、Gemini、OpenRouter、Qwen、Grok、Mistral。

更换模型时：仅修改 `provider`、`api_key`、`model`，无需修改代码。

### AI 职责

AI 仅负责**市场状态分析**。禁止负责：开仓、平仓、止损、仓位。

AI 输出：

```json
{
  "market_regime": "TREND_UP",
  "confidence": 88,
  "reason": "趋势结构完整"
}
```

允许状态：

- `TREND_UP`
- `TREND_DOWN`
- `RANGE`
- `HIGH_VOLATILITY`
- `LOW_VOLATILITY`

---

## 五、系统架构

```
FastAPI Dashboard
        │
        ▼
   Controller
        │
 ┌──────┼──────┐
 ▼      ▼      ▼
AI    Risk   State
        │
        ▼
 PositionManager
        │
        ▼
  OrderManager
        │
        ▼
     Broker
```

Broker 实现：`PaperBroker`（纸盘）、`OKXBroker`（实盘）。

---

## 六、项目目录

```
yunlong/
├── app/
│   ├── core/        # 核心配置、常量、工具
│   ├── ai/          # AIProvider 统一抽象与实现
│   ├── broker/      # Broker 抽象 + PaperBroker + OKXBroker
│   ├── exchange/    # 交易所专用适配层（OKX）
│   ├── risk/        # 风控引擎
│   ├── trading/     # OrderManager / PositionManager / Controller
│   ├── recovery/    # 自动恢复逻辑
│   ├── storage/     # JSON / JSONL 存储
│   ├── services/    # 业务服务编排
│   └── api/         # FastAPI Dashboard
├── data/            # state.json、trades.jsonl
├── logs/            # system.log、trade.log、error.log
├── config.yaml
└── run.py
```

---

## 七、配置文件

项目根目录：`config.yaml`，仅保存密钥与运行模式：

```yaml
okx:
  api_key: xxxxx
  secret: xxxxx
  passphrase: xxxxx

ai:
  provider: deepseek
  api_key: xxxxx
  model: deepseek-chat

trading:
  live: false
```

- `live: false` → 纸盘模式
- `live: true` → 实盘模式

不支持 OKX Testnet。

---

## 八、Broker 设计

统一接口：

```python
class Broker:
    get_balance()
    get_position()
    get_open_orders()
    place_order()
    cancel_order()
```

- `PaperBroker`：本地模拟成交
- `OKXBroker`：真实交易

业务代码永远依赖 `Broker`，而不是 `ccxt`。

---

## 九、OrderManager

职责：统一管理订单生命周期。

流程：`创建订单 → 提交订单 → 等待成交 → 订单完成`

所有订单必须生成 `client_order_id`，示例：

```
YL-20260828-00001
```

用于：恢复、去重、幂等控制。

---

## 十、风控系统

系统最高权限模块，负责：

- 是否允许开仓
- 仓位大小
- 杠杆大小
- 止损计算
- 熔断控制

规则：

- 连续亏损 3 次 → 暂停 12 小时
- 每日亏损 15% → 停止交易

---

## 十一、仓位规则

V1 仅允许**单仓位**（同时最多一个持仓）。

禁止：马丁、网格、加仓、锁仓、双向持仓。

---

## 十二、成交规则

核心原则：**Maker First**

- 开仓：优先挂单，等待 20 秒；超时撤单并重新评估
- 禁止：追单、追涨杀跌
- 止损：市价单
- 止盈：Maker 优先

---

## 十三、利润保护

| 盈利达到 | 执行动作 |
| --- | --- |
| +3% | 移动至保本 |
| +8% | 锁定 +3% |
| +15% | 锁定 +8% |
| +30% | 锁定 +15% |

---

## 十四、时间同步

- 统一时间源：OKX 服务器时间
- 启动同步：一次
- 运行期间：每 5 分钟同步一次
- 时间漂移超过 10 秒 → 系统进入「暂停开仓」状态

---

## 十五、状态存储

目录：`data/`

文件：

- `state.json`：账户状态、持仓状态、系统状态
- `trades.jsonl`：全部交易记录

---

## 十六、Trade Journal

记录每一笔交易决策，用于策略分析、盈利/亏损分析：

```json
{
  "time": "2026-08-28",
  "market_regime": "TREND_UP",
  "confidence": 89,
  "entry_reason": "趋势突破",
  "result": "+2.5R"
}
```

---

## 十七、自动恢复

启动流程：

```
读取配置 → 同步时间 → 连接 OKX → 查询余额 → 查询持仓 → 查询订单 → 恢复状态 → 开始运行
```

恢复原则：交易所状态优先。

---

## 十八、日志系统

日志框架：Loguru

文件：

- `system.log`：系统启动/关闭、AI 分析、恢复、异常
- `trade.log`：开仓、平仓、止损、止盈
- `error.log`：仅错误级别

---

## 十九、代码规范

全部代码使用**中文注释**。示例：

```python
# 获取当前持仓信息
# 检查是否允许开仓
# 执行移动止损
```

禁止大量英文业务注释。

---

## 二十、前端规范

Dashboard 全部使用中文。

| 英文 | 中文 |
| --- | --- |
| Position | 当前持仓 |
| Balance | 账户余额 |
| Profit | 累计收益 |

状态统一中文：

| 英文 | 中文 |
| --- | --- |
| RUNNING | 运行中 |
| STOPPED | 已停止 |
| RECOVERING | 恢复中 |
| PAPER | 纸盘模式 |
| LIVE | 实盘模式 |
| LONG | 做多 |
| SHORT | 做空 |
| FILLED | 已成交 |
| PARTIAL | 部分成交 |
| CANCELED | 已撤销 |
| ERROR | 异常 |

---

## 二十一、开发路线

- **阶段 1**：Config · Broker · Storage
- **阶段 2**：OrderManager · RiskEngine · Recovery
- **阶段 3**：PaperBroker · Dashboard
- **阶段 4**：AI 接入 · 实盘验证

---

## 二十二、项目成功标准

满足：

- 连续运行 30 天
- 自动恢复正常
- 无重复下单
- 无持仓丢失
- 无状态错误
- 风控正常
- 利润保护正常

即可进入长期实盘运行。

---

## 最终目标

构建一个：

> AI 市场分析 + Maker 优先成交 + 严格风控 + 自动恢复 + 利润保护

的 ETH 永续挑战赛自动交易系统。

项目核心不是预测未来，而是：**控制风险 · 稳定执行 · 持续复利**。
