"""etf-copilot: 面向小资金(¥500 级)的 A 股 ETF 半自动投资助手。

模块分工:
    config      配置加载与校验(费率/风控的唯一入口)
    quotes      行情抓取(腾讯/新浪/东财,自动降级)
    universe    候选池构建(买得起 + 排雷 + 流动性)
    indicators  技术指标(纯函数)
    strategy    信号生成(趋势跟随 + 分位过滤)
    risk        风控硬闸门(AI 不可覆盖)
    ai_review   多模型交叉复核(AI 只能反对,不能加仓)
    ticket      半自动下单工单
    journal     持仓与审计日志
    backtest    含真实费用的回测
"""

__version__ = "0.1.0"
