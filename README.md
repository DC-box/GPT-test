# AIFactory

一句话需求进去，四个智能体协作，在 Docker 沙箱里交付一个通过 pytest 的 FastAPI 模块。

例如：「帮我写一个用户管理模块，包含注册、登录和 JWT 校验」。工厂会完成需求理解、API 契约、FastAPI 实现、pytest，以及沙箱测试。测试通过就交付；失败就把日志交回编码智能体修复。

不传参数时，默认需求是：写一个图书管理 API，包含添加图书、获取图书列表、按 ID 查询图书，以及带简单库存扣减逻辑。

## 四个智能体

状态 `FactoryState` 含有 `task_description`、`api_schema`、`code`、`test_code`、`test_result`、`error_logs`、`retry_count`。

1. **architect_agent（架构师）**  
   只设计 FastAPI 的 Pydantic 模型与路由契约，不写完整实现，并把 `retry_count` 设为 0。
2. **coder_agent（编码）**  
   按契约写出完整 FastAPI 模块。重试时会带上 `error_logs`，要求它修 bug。模型输出外层的 markdown 代码围栏会被去掉。控制台打印的尝试编号就是当前的 `retry_count`。
3. **test_generator_agent（测试）**  
   用 `fastapi.testclient.TestClient` 生成 pytest，只保留代码，并去掉围栏。
4. **docker_tester_agent（沙箱）**  
   把 `main.py` 和 `test_main.py` 写到沙箱目录（默认 `./sandbox_workspace`），再用绝对路径挂载进容器执行：

   ```bash
   docker run --rm -v <沙箱绝对路径>:/app -w /app python:3.10-slim bash -c "pip install fastapi httpx pytest pydantic -q && pytest test_main.py"
   ```

   超时 60 秒。退出码 0 时 `test_result` 为 `SUCCESS`，并清空 `error_logs`。非 0 时为 `FAILED`，保存 stdout 和 stderr，`retry_count` 加 1。Docker 不存在、超时或其他异常同样记为 `FAILED`（日志是异常文本），并增加 `retry_count`，不会在写回状态之前让进程崩溃。

图的走向：架构师 → 编码 → 测试 → 沙箱。沙箱之后，`SUCCESS` 结束；`FAILED` 且 `retry_count < 3` 回到编码（编码之后会再次生成测试并进沙箱）；否则结束。

## 环境变量

密钥只从环境变量 `OPENAI_API_KEY` 读取。程序不会写死密钥，也不要提交填好的 `.env`。

模型固定为 `gpt-4o-mini`，`temperature=0`。

可选：`AIFACTORY_SANDBOX` 覆盖默认沙箱目录 `./sandbox_workspace`。

## 安装

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

本机需要可用的 Docker，沙箱镜像是 `python:3.10-slim`。镜像里的测试依赖由上面的 `docker run` 自己安装。

## 运行

```bash
export OPENAI_API_KEY="你的密钥"
python -m aifactory
python -m aifactory "帮我写一个用户管理模块，包含注册、登录和 JWT 校验"
```

生成的代码只在 Docker 沙箱里执行，工厂进程不会在宿主机上运行这些文件。成功时打印 `main.py` 和 `test_main.py` 的路径；失败时打印交付失败。

重试上限是 3 次沙箱失败：每次失败后 `retry_count` 加 1，小于 3 才回到编码节点，到达 3 就停止并结束。

## 仪表盘

另开两个终端。先启动 SSE 接口，再启动页面：

```bash
uvicorn aifactory.server:app --host 0.0.0.0 --port 8000
pip install -r requirements.txt && streamlit run dashboard.py --server.address 0.0.0.0 --server.port 8501
```

浏览器打开 http://localhost:8501 。页面把任务 POST 到 http://localhost:8000/api/factory/run ，按 SSE 事件刷新四个智能体、代码、Schema、测试和终端。

侧栏勾选「强制启用 Docker 沙盒」（默认开启）时，沙箱行为与命令行一致。取消勾选则不执行真实 `docker run`，日志里记一条已跳过，流水线仍会结束。生成代码不会在宿主机上执行。

## 自测

```bash
python -m unittest discover -s tests -t .
```

该测试只检查导入、图编译和路由（成功结束、失败且 `retry_count` 为 1 时回到编码、`retry_count` 为 3 时结束），不调用 OpenAI，也不需要 Docker。
