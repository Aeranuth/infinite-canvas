# @basketikun/canvas-proxy

Infinite Canvas 的本地转发代理。浏览器直连第三方 AI 接口时经常被 CORS 拦截，启动它之后，网页会把请求先发到本机，再由本机转发到目标地址。

代理只做转发：不改写请求体，不校验 API Key，不落盘任何日志。

## 使用

```bash
npx @basketikun/canvas-proxy@latest
```

默认监听 `http://127.0.0.1:23210`。把这个地址填进 Infinite Canvas 的「配置 → 本地代理」，并打开开关即可。

带上 `@latest` 是因为 npx 会缓存已下载的版本，不加就可能一直运行旧版本。

可选参数：

```bash
npx @basketikun/canvas-proxy@latest --port 23210 --host 127.0.0.1
```

也支持 `PORT` / `HOST` 环境变量。

### Docker Compose

在项目根目录启动独立代理服务，镜像直接使用当前源码，无需等待 npm 发布：

```bash
docker compose up -d --build canvas-proxy
```

执行完整的 `docker compose up -d --build` 也会启动该服务。更新代理源码后，重新执行上述命令即可重建并替换代理容器，不需要重建前端。

网页「配置 → 本地代理」仍需手动开启，地址填写 `http://127.0.0.1:23210`。如果已有 npx 或 Node 代理占用该端口，先停止旧进程。

容器内部监听 `0.0.0.0:23210`，Compose 仅发布到宿主机 `127.0.0.1:23210`，不对局域网或公网开放。此配置要求浏览器与 Docker 宿主机在同一台电脑；远程访问网页时，浏览器的 `127.0.0.1` 指向访问者电脑，而不是部署服务器。

查看日志或停止代理：

```bash
docker compose logs -f canvas-proxy
docker compose stop canvas-proxy
```

## 转发规则

把完整目标地址接在代理地址后面：

```
http://127.0.0.1:23210/https://api.openai.com/v1/models
        └─── 代理地址 ──┘└──────── 目标地址 ────────┘
```

请求方法、请求头（除 `host` 等逐跳头外）、请求体原样转发；响应状态码、响应头和响应体原样返回，并补上宽松的 CORS 头。SSE 流式响应按块透传，不做缓冲。

视频 `GET .../videos/{id}/content` 的 301、302、303、307、308 跳转由代理逐次跟随，支持相对地址，并在后续请求中保留相同的 `Authorization`；最多跟随 20 次，与原生 fetch 一致，超过后返回 502。其他请求保持原有跳转行为。

这会将视频请求的凭据发送给上游指定的跳转目标，请仅使用可信渠道。浏览器直连无法保证跨域跳转保留鉴权，此能力需要开启本地代理并运行包含该修改的代理版本。

访问根路径 `/` 会返回代理版本信息，可用于连通性检测，不会记入转发日志。

## 转发日志

每转发一条请求就在终端打印一行，包含时间、方法、完整目标地址、上游状态码和耗时：

```
4:05:42 PM GET https://api.openai.com/v1/models -> 200 0.9s
4:05:43 PM POST https://api.openai.com/v1/images/generations -> 200 26.4s
4:05:45 PM GET https://api.example.com/v1/models -> failed (fetch failed) 1.6s
```

发生重定向并收到最终响应时，会额外输出原始地址和最终地址（包含最终返回 404 等错误的情况），不逐跳列出中间地址：

```text
4:05:45 PM GET redirect https://api.example.com/v1/videos/task/content -> https://cdn.example.com/video.mp4
4:05:45 PM GET https://api.example.com/v1/videos/task/content -> 200 0.9s
```

直接返回的 `metadata.url` 不属于 HTTP 重定向；下载它时会出现在普通请求日志中。

日志在收到上游响应头时打印，所以流式请求会立刻出现一行，而不是等整段响应结束。程序输出到标准输出，不主动写日志文件；Docker 可收集这些日志。日志不包含 Authorization 等请求头或请求体，但完整 URL 可能包含 API Key 或临时签名，分享前务必打码。

上游最终响应包含 `X-Request-ID` 时，状态日志末尾追加 `X-Request-ID=...`；没有该响应头则省略。重定向后记录最终响应的 ID，不是中间跳转响应的 ID；不会使用客户端请求头代替。

## 安全提示

代理默认只监听 `127.0.0.1`，仅本机可访问。它会转发任何请求到任何地址，请不要用 `--host 0.0.0.0` 暴露到公网。API Key 依然由浏览器持有并随请求转发，代理本身不存储。

## 环境要求

Node.js >= 18。
