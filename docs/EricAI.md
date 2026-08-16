# 接入 Ericsson EricAI 网关

本项目支持把所有 agent 的 LLM 调用路由到 **Ericsson 内部 EricAI 网关**（OpenAI 兼容，SSO 设备码认证）。默认模型为 `openai/gpt-oss-120b`（在本项目中以 `ericai-gpt-oss-120b` 选择）。

## 工作原理

`ericai` 提供的 `EricAI` 类是 `openai.OpenAI` 的子类：它自动处理内网 `base_url`、CA 证书，并在**每次请求**前通过 `_prepare_options` 注入一个自动刷新的 SSO token（无需静态 API key）。

集成时（`src/core/llm.py`）没有去提取会过期的 token，而是把 `EricAI` / `AsyncEricAI` 客户端实例直接注入 LangChain 的 `ChatOpenAI`：

```python
sync_client = EricAI(timeout=settings.ERICAI_TIMEOUT)
async_client = AsyncEricAI(timeout=settings.ERICAI_TIMEOUT)
ChatOpenAI(
    model="openai/gpt-oss-120b",
    streaming=True,
    client=sync_client.chat.completions,
    async_client=async_client.chat.completions,
    root_client=sync_client,
    root_async_client=async_client,
)
```

这样既保留了 EricAI 的自动 token 刷新，又完整复用了 LangChain 的 tool-calling 与流式能力，agent 无需任何改动。

## 前置条件（一次性）

必须连接 **Ericsson 内网 / VPN**。

1. 在**本项目的虚拟环境**里安装 `ericai`（内部 index）：

   ```powershell
   uv pip install --index https://arm.sero.gic.ericsson.se/artifactory/api/pypi/proj-swtech-pypi-local/simple ericai
   ```

   > `ericai` 是私有包，未加入 `pyproject.toml`（否则公共 PyPI 的 `uv sync` 会失败）。它按需惰性导入：不装也不影响其它 provider。

2. 完成一次 SSO 设备码登录（之后 token 由库自动刷新）：

   ```powershell
   ericai --ericsson-test-connectivity
   ```

3. 在**启动服务的终端**里设置代理绕过（每个新终端都要设）：

   ```powershell
   $env:NO_PROXY=".gic.ericsson.se,.sero.gic.ericsson.se,localhost,127.0.0.1"
   ```

## 配置 `.env`

```dotenv
USE_ERICAI=true
DEFAULT_MODEL=ericai-gpt-oss-120b
ERICAI_TIMEOUT=180
```

`USE_ERICAI=true` 时 EricAI 会作为首选 provider；若未显式设置 `DEFAULT_MODEL`，默认即 `ericai-gpt-oss-120b`。

## 运行（推荐本地，非 Docker）

```powershell
uv sync --frozen
.\.venv\Scripts\Activate.ps1
uv pip install --index https://arm.sero.gic.ericsson.se/artifactory/api/pypi/proj-swtech-pypi-local/simple ericai
$env:NO_PROXY=".gic.ericsson.se,.sero.gic.ericsson.se,localhost,127.0.0.1"
python src/run_service.py
```

另开一个终端跑界面：

```powershell
streamlit run src/streamlit_app.py
```

在 Streamlit 的模型下拉框里选择 `ericai-gpt-oss-120b`，或依赖 `.env` 里的 `DEFAULT_MODEL`。

> **Docker 说明**：SSO 设备码登录 + 内网 + CA 证书使容器化运行较复杂（需把已登录的 token 缓存、CA、`NO_PROXY` 和内网可达性带进容器）。建议本地 Python 运行。

## 可用模型

| 本项目选择名 | 实际网关模型 ID |
| --- | --- |
| `ericai-gpt-oss-120b`（默认） | `openai/gpt-oss-120b` |
| `ericai-gpt-oss-20b` | `openai/gpt-oss-20b` |
| `ericai-deepseek-v4-flash` | `deepseek-ai/DeepSeek-V4-Flash-0731` |
| `ericai-deepseek-v4-pro` | `deepseek-ai/DeepSeek-V4-Pro` |

> 两个 DeepSeek V4 走网关时同样关掉 thinking(`extra_body={"thinking":{"type":"disabled"}}`),原因与直连 DeepSeek 一致:ChatOpenAI 不会在 tool call 轮次之间回放 `reasoning_content`,开着会断多智能体交接。网关接受该字段,已用 `DeepSeek-V4-Flash-0731` 验证。

> 选择名带 `ericai-` 前缀是刻意的：它与 Groq 同名的 `openai/gpt-oss-*` 值区分开，避免路由到错误的 provider。

### 新增其它 EricAI 模型

在已登录的机器上列出网关模型：

```powershell
ericai api models.list
```

然后改两处：

1. `src/schema/models.py` 的 `EricaiModelName` 加一个成员（值用唯一的 `ericai-` 前缀名）。
2. `src/core/llm.py` 的 `_ERICAI_MODEL_MAP` 加一条：`"ericai-<你的名字>": "<网关真实模型 ID>"`。

## 故障排查

| 现象 | 原因 / 解决 |
| --- | --- |
| `USE_ERICAI is set but the 'ericai' package is not installed` | 按前置条件第 1 步在本项目 venv 内安装 `ericai` |
| 连接超时 / 卡住 | 未设 `NO_PROXY`，或未连内网 / VPN |
| 认证失败 | 重新执行 `ericai --ericsson-test-connectivity` 登录 |
| 长回答被截断 / 慢模型超时 | 调大 `ERICAI_TIMEOUT` |
| agent 报工具调用错误 | 确认所选模型支持 function calling（`gpt-oss-120b` 支持）|

## 端到端验证

无内网 / 未安装 `ericai` 时无法真正跑通网关请求。具备条件后，最小自检：

```powershell
python -c "from ericai import EricAI; print(EricAI(timeout=180).chat.completions.create(model='openai/gpt-oss-120b', messages=[{'role':'user','content':'ping'}]).choices[0].message.content)"
```

能打印回复即说明认证与网关可用；之后启动服务用 Streamlit 选 `ericai-gpt-oss-120b` 对话即可。
