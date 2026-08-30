# 云龙挑战赛（YunLong Challenge System，YCS）

运行于 Linux VPS 的 ETH 永续合约自动交易系统：OKX、ETH-USDT-SWAP、单账户单策略、长期实盘。

## 快速开始

```bash
# 安装依赖（首次）
uv sync

# 配置
cp config.yaml config.yaml.bak   # 保留模板，编辑 config.yaml 填入 OKX/AI 密钥

# 启动（纸盘模式，config.yaml 中 trading.live = false）
.venv/bin/python run.py

# Dashboard  http://127.0.0.1:8765
```

## 目录

```
app/          # 应用代码（core/ai/broker/exchange/risk/trading/recovery/storage/services/api）
data/         # state.json、trades.jsonl
logs/         # system.log、trade.log、error.log
config.yaml   # 密钥 + 运行模式
run.py        # 启动入口
DESIGN.md     # 设计文档 v1.0（Final）
```

## 设计总览（DESIGN.md 节选）

- **AI 架构**：`AIProvider` 统一抽象 + LiteLLM 网关，换模型仅改配置三行
- **Broker**：`PaperBroker`（纸盘）/`OKXBroker`（实盘）统一接口，业务不依赖 ccxt
- **风控优先**：连亏 3 次熔断 12h / 日亏 15% 停止交易
- **成交**：Maker First，挂单 20s 超时撤单重评；止损市价
- **利润保护**：盈利 3% 保本 / 8% 锁 3% / 15% 锁 8% / 30% 锁 15%
- **自动恢复**：启动全量 OKX 查询覆盖本地状态；每 5 分钟同步 OKX 时间，漂移>10s 暂停开仓
