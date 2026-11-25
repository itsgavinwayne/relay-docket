# 模型和记录格式覆盖

下面是当前软件的格式覆盖。表内为代表型号，模型 ID 以配置账户所在区域的 AWS 官方目录为准。每组首选和差异用例使用合成数据运行。当前交付环境没有独立 AWS 沙箱，以真实调用结果复核后再在团队内标记为已上线。

| 厂商 | 代表基础模型 ID | 对账格式 |
|---|---|---|
| Anthropic | `anthropic.claude-opus-4-7` | Claude 原生 Invoke、Converse |
| Anthropic | `anthropic.claude-sonnet-4-6` | Claude 原生 Invoke、Converse |
| Anthropic | `anthropic.claude-haiku-4-5-20251001-v1:0` | Claude 原生 Invoke、Converse |
| DeepSeek | `deepseek.v3.2` | Converse |
| 智谱 | `zai.glm-5` | Converse |
| 阿里 Qwen | `qwen.qwen3-235b-a22b-2507-v1:0` | Converse |
| 月之暗面 | `moonshotai.kimi-k2.5` | Converse |
| MiniMax | `minimax.minimax-m2.5` | Converse |

Converse 适配器用于输入和输出语义相同、日志记录完整的模型。非 Claude 的供应商私有 Invoke 格式超出当前覆盖。流式对账以日志中可完整重建的事件为准。工具声明、调用、返回值和 reasoning 等只有在两边都有可比较表示时才会计入通过。对于无法还原的记录，保留 AWS 证据中能定位到的模型 ID，并报告未验证。

型号登记时记录 AWS 实际调用区域、模型和 inference profile、Bedrock 操作、客户端是否流式、验收日期和结果。

[AWS 模型卡片](https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards.html) · [AWS API 兼容性](https://docs.aws.amazon.com/bedrock/latest/userguide/models-api-compatibility.html)
