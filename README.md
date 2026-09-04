# Jupyter 概念验证

## portal-web: 测试演示前端

**门户前端**，负责：

- 展示 notebook
- 编辑 cell
- 点击 Save
- 点击 Run
- 显示 runtime 状态
- 显示 outputs

由React实现的最简前端实现，用于概念验证和接通示例。

## notebook-service: 文件服务端

**Document plane**，负责：

- 创建 notebookId
- 保存 `.ipynb` 内容
- 维护 revision
- 作为 notebook authoritative state（权威状态）

当前提供两套 route：

- **v1（正式开发基础）**：`POST /api/v1/notebooks`、`GET /api/v1/notebooks/{notebookId}`、
  `PUT /api/v1/notebooks/{notebookId}`、`GET /api/v1/notebooks/{notebookId}/revisions/{revision}`，
  以及 `/health/live`、`/health/ready`。基于 SQLite（metadata）+ content-addressed
  Blob（`data/v1/`），支持 revision CAS、幂等、ETag/条件读取、统一错误 envelope。
  契约见 [docs/api/notebook-service-v1.openapi.yaml](docs/api/notebook-service-v1.openapi.yaml)。
- **旧 POC route（过渡期兼容入口）**：`POST/GET/PUT /api/notebooks` 原样保留，
  继续使用 `data/notebooks/`，不出现在 v1 OpenAPI 中。**两套 route 暂不互通**，
  新前端和新测试不得调用旧 route。

启动、配置与限制详见 [notebook-service/README.md](notebook-service/README.md)。


## Runtime Service

**runtime plane 的控制面**，负责：

- 创建 runtime
- 查询 runtime 状态
- 删除 runtime
- 屏蔽底层 provider 差异

目前底层 provider 是 Docker，后面换 K8s 时，把“创建 container”替换成“创建 pod”

## Runtime Gateway

**Jupyter通信/代理层**，负责：

- 根据 `runtimeId` 找到对应 Jupyter Server
- 把前端发来的 Jupyter HTTP / WebSocket 请求转发过去

---

## 其他模块

### ProductJupyterClient

前端里的 Jupyter 客户端适配层。复用了 `@jupyterlab/services`，不会直接连某个固定 Jupyter Server，而是通过 `/runtime-proxy/<runtimeId>/...` 连接。

执行标准 Jupyter 通信动作：

- KernelManager
- startNew({ name: 'python3' })
- kernel.requestExecute(...)

### RuntimeManager

前端的 runtime 编排层（不是 Jupyter SDK 本身的一部分），负责先保证算力存在，再让 Jupyter client 去连：

`createRuntime(notebookId)`
轮询 `getRuntime(...)`
等到 `READY`
stop 时调用 `deleteRuntime(...)`


# 完整运行流程

## 流程一：打开 notebook

```Browser
Browser
  -> GET /api/notebooks/<notebookId>
  -> Notebook Service
  -> 返回 { notebookId, revision, content }
  -> useNotebookDocument 放入前端 state
  -> NotebookPage 遍历 content.cells 渲染
```

## 流程二：编辑并保存 notebook

```
用户改 textarea
  -> updateCellSource(...)
  -> 前端 document.content.cells 被更新
  -> dirty = true
  -> 点击 Save
  -> PUT /api/notebooks/<notebookId>
  -> Notebook Service 写入新 revision
  -> 返回新的 revision
  -> 前端 dirty = false
```

## 流程三：运行 code cell

```
点击 Run
  -> runtimeManager.ensureReady(notebookId)
  -> Runtime Service 创建/返回 READY runtime
  -> productJupyterClient.execute(runtimeId, code)
  -> 通过 Runtime Gateway 连上对应 Jupyter Server
  -> 启动/复用 kernel
  -> requestExecute(code)
  -> 收集 outputs / execution_count
  -> saveExecutionResult(...)
  -> PUT 回 Notebook Service
  -> outputs 持久化进新的 notebook revision
```

# 测试
## 默认notebook服务端口是8000，runtime service端口是8100，gateway端口是8200，前端是5173
# !!!注意：以下是本地+服务器两端测试方案，默认认为runtime-gateway和runtime-service以及docker在服务器，不可直接复用
# 需要根据本地开发情况进行测试（比如如果不是在服务器端开发，就没必要开ssh隧道）


按下面顺序测试，尽量保持环境干净

## 先准备四个进程

Mac 终端 1，Notebook Service：

```bash
cd ~/Downloads/notebook-proj/jupyter-poc/notebook-service
source .venv/bin/activate

uvicorn app.main:app \
  --reload \
  --host 127.0.0.1 \
  --port 8000
```

远程 Linux，Runtime Service：

```bash
cd ~/jupyter-poc/runtime-service
source .venv/bin/activate

uvicorn app.main:app \
  --reload \
  --host 127.0.0.1 \
  --port 8100
```

远程 Linux，Runtime Gateway：

```bash
cd ~/jupyter-poc/runtime-gateway
source .venv/bin/activate

uvicorn app.main:app \
  --reload \
  --host 127.0.0.1 \
  --port 8200
```

Mac 再开 SSH tunnel：

```bash
ssh -i <pem路径> \
  -L 8100:127.0.0.1:8100 \
  -L 8200:127.0.0.1:8200 \
  weiyk@<服务器IP>
```

这时 Mac 应该是：

```text
localhost:8000 → 本地 Notebook Service

localhost:8100
    ↓ SSH
远程 Runtime Service

localhost:8200
    ↓ SSH
远程 Runtime Gateway
```

---

## 再确认 Vite proxy

你的 `vite.config.ts` 至少应该有这三段：

```ts
server: {
  proxy: {
    '/api': {
      target: 'http://127.0.0.1:8000',
      changeOrigin: true,
    },

    '/runtime-api': {
      target: 'http://127.0.0.1:8100',
      changeOrigin: true,
      rewrite: (path) =>
        path.replace(/^\/runtime-api/, ''),
    },

    '/runtime-proxy': {
      target: 'http://127.0.0.1:8200',
      changeOrigin: true,
      ws: true,
    },
  },
},
```

然后 Mac：

```bash
cd ~/Downloads/notebook-proj/jupyter-poc/portal-web

npm run dev
```

不用 `npm run build`。

---

# 正式测试前，先清理现有 Runtime

之前的：

```text
rt_b7cc67dead18
```

如果还活着 先删掉

远程：

```bash
curl -sS \
  -X DELETE \
  http://127.0.0.1:8100/v1/runtimes/rt_b7cc67dead18 \
| jq
```

如果它已经 STOPPED/不存在也没关系。

然后检查：

```bash
docker ps \
  --filter label=qmentor.managed-by=runtime-service
```

理想状态没有任何 Jupyter Runtime：

```text
CONTAINER ID   IMAGE   ...   NAMES
```

也就是空。

这一步我们要观察：

> 点击 Run 之前 0 Container，点击 Run 后自动出现 Container。

---

# 打开 Portal

浏览器：

```text
http://localhost:5173
```

当前应该显示类似：

```text
Notebook: nb_e3b520ca7c36
Revision: ...
State: Saved

Runtime: Disconnected

[Stop Runtime] [Save]
```

其中 Stop Runtime 应该 disabled。

先打开 DevTools：

```text
Console
Network
```

---

# 第一个实验 Cell 建议用：

找一个 Code Cell，把内容改成：

```python
print("Phase 1D works")
2026 + 1
```

先点击：

```text
Save
```

确认页面：

```text
State: Saved
Revision: N
```

然后远程再检查：

```bash
docker ps \
  --filter label=qmentor.managed-by=runtime-service
```

这时仍然应该没有 Runtime。

这验证：

```text
打开 Notebook
编辑 Notebook
保存 Notebook

都不会创建计算资源
```

---

# 然后点击这个 Cell 的 Run

现在重点观察三处。

### 浏览器页面

应该短暂看到：

```text
Runtime: STARTING (rt_xxx)
```

随后：

```text
Runtime: READY (rt_xxx)
```

因为这里实际上正在：

```text
Run
 ↓
runtimeManager.ensureReady()
 ↓
POST /runtime-api/v1/runtimes
 ↓
Runtime Service
 ↓
DockerProvider.create()
```

---

### 远程服务器

同时执行：

```bash
docker ps \
  --filter label=qmentor.managed-by=runtime-service
```

应该自动出现：

```text
jupyter-runtime-rt_xxxxxxxxxxxx
```

并且你这一次**没有手工 `docker run`**。

这是 Phase 1D 第一项关键结果。

---

### 浏览器 Network / Console

正常情况下会出现：

```text
POST /runtime-api/v1/runtimes
GET  /runtime-api/v1/runtimes/rt_xxx
...
POST /runtime-proxy/rt_xxx/api/kernels
WS   /runtime-proxy/rt_xxx/api/kernels/.../channels
```

WS 应该：

```text
101 Switching Protocols
```

这部分实际上复用了刚才已经通过的 Phase 1C。

---

# 最关键：看 Cell Output

运行结束以后，Code Cell 下面现在还是 JSON renderer，所以正常应该看到类似：

```json
[
  {
    "output_type": "stream",
    "name": "stdout",
    "text": "Phase 1D works\n"
  },
  {
    "output_type": "execute_result",
    "execution_count": 1,
    "data": {
      "text/plain": "2027"
    },
    "metadata": {}
  }
]
```

页面顶部 Revision 应该同时：

```text
N
↓
N + 1
```

因为执行结束后调用了：

```text
saveExecutionResult()
```

也就是：

```text
Kernel
 ↓
outputs
 ↓
NotebookDocumentState
 ↓
PUT Notebook Service
 ↓
new revision
```

这是这一轮最重要的部分。

---

# 然后直接检查 Notebook Service 里的真实文件

Mac：

```bash
cd ~/Downloads/notebook-proj/jupyter-poc/notebook-service
```

看：

```bash
cat data/notebooks/nb_e3b520ca7c36/meta.json | jq
```

应该：

```json
{
  "notebookId": "nb_e3b520ca7c36",
  "currentRevision": <新 revision>
}
```

然后：

```bash
LATEST=$(jq -r '.currentRevision' \
  data/notebooks/nb_e3b520ca7c36/meta.json)

jq '.cells' \
  data/notebooks/nb_e3b520ca7c36/${LATEST}.ipynb
```

应该在刚才的 Code Cell 中看到：

```json
"outputs": [
  {
    "output_type": "stream",
    "name": "stdout",
    "text": "Phase 1D works\n"
  },
  ...
]
```

也就是说 Output 已经真正离开 Kernel，进入 Document Store。

---

# 然后点击 Stop Runtime

页面：

```text
Stop Runtime
```

正常应该变成：

```text
Runtime: Disconnected
```

远程：

```bash
docker ps \
  --filter label=qmentor.managed-by=runtime-service
```

应该再次为空。

此时：

```text
Jupyter Server ×
Kernel ×
Runtime Container ×
```

但是页面上的 Output 仍然存在。

---

# 最后一个实验：刷新页面

直接刷新：

```text
http://localhost:5173
```

这时候页面重新从：

```text
Notebook Service
```

加载最新 revision。

预期仍然显示：

```text
Code:

print("Phase 1D works")
2026 + 1
```

以及保存过的：

```text
Phase 1D works
2027
```

同时远程：

```bash
docker ps \
  --filter label=qmentor.managed-by=runtime-service
```

仍然没有 Runtime。

如果这个最终状态成立：

```text
Runtime = 0

Notebook source ✓
Notebook output ✓
revision ✓
```

那么 最核心的实验就跑通了

---

测试重点只看这 6 个结果：

```text
打开/Save 时 Container = 0

点击 Run 后自动出现 Container

Runtime STARTING → READY

Cell 得到 Phase 1D works / 2027

Revision + 1

Stop + 刷新后 Container = 0，但 Output 仍存在
```
