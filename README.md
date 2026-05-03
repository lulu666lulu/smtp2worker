# smtp2worker

一个小型 SMTP 到 Cloudflare Worker 邮件网关：旧工具只需要按普通 SMTP 发邮件，网关会把邮件解析成 JSON，转发给部署在 Cloudflare Workers 上的 HTTP 接口，再由 Worker 通过 Cloudflare Email Routing 的 `send_email` binding 发送验证码邮件。

这个版本使用 Cloudflare 原生 Email Workers 发信，不依赖 Resend 或其他第三方发信 API。

## 目录

- `smtp2worker.py`：SMTP 服务器，接收 `MAIL/RCPT/DATA` 后 POST 到 Worker。
- `worker/src/worker.js`：Cloudflare Worker，校验 token 后调用 Cloudflare `send_email` binding。
- `config.example.env`：SMTP 网关环境变量示例。
- `worker/wrangler.toml.example`：Worker 部署配置示例。

## 部署 Worker

进入 `worker` 目录，复制配置文件：

```bash
cp wrangler.toml.example wrangler.toml
```

先在 Cloudflare 对应域名启用 Email Routing，并至少验证一个目标邮箱。SMTP 邮件里的 From 必须是已启用 Email Routing 的域名下的发件地址，例如 `noreply@example.com`。

复制 `wrangler.toml.example` 后，确认其中有 `[[send_email]]` binding：

```toml
[[send_email]]
name = "SEND_EMAIL"
```

然后设置网关密钥并部署：

```bash
wrangler secret put BRIDGE_TOKEN
wrangler deploy
```

`BRIDGE_TOKEN` 必须和 SMTP 网关使用的 `BRIDGE_TOKEN` 相同。部署完成后得到类似：

```text
https://smtp2worker.your-subdomain.workers.dev
```

## 运行 SMTP 网关

本机直接运行，Python 3.8+ 即可：

```bash
python smtp2worker.py ^
  --worker-url https://smtp2worker.your-subdomain.workers.dev ^
  --bridge-token change-me-to-a-long-random-secret ^
  --smtp-username smtp-user ^
  --smtp-password smtp-password ^
  --listen-host 127.0.0.1 ^
  --listen-port 2525
```

Linux/macOS shell 把 `^` 换成 `\` 即可。

也可以用环境变量：

```bash
copy config.example.env .env
```

PowerShell 载入示例：

```powershell
Get-Content .env | ForEach-Object {
  if ($_ -and -not $_.StartsWith("#")) {
    $name, $value = $_.Split("=", 2)
    [Environment]::SetEnvironmentVariable($name, $value, "Process")
  }
}
python .\smtp2worker.py
```

旧工具里的 SMTP 配置：

```text
Host: 127.0.0.1
Port: 2525
Username: smtp-user
Password: smtp-password
Encryption: none
From: 你在 Cloudflare Email Routing 域名下的发件地址
```

Worker 默认优先使用 SMTP 邮件里的 From。`FROM_EMAIL` 只是兜底值；如果某个旧工具不能配置合法发件地址，可以在 Worker 变量里设置 `FORCE_FROM_EMAIL=true` 来强制覆盖。

## Docker

```bash
docker build -t smtp2worker .
docker run --rm -p 2525:2525 ^
  -e WORKER_URL=https://smtp2worker.your-subdomain.workers.dev ^
  -e BRIDGE_TOKEN=change-me-to-a-long-random-secret ^
  -e SMTP_USERNAME=smtp-user ^
  -e SMTP_PASSWORD=smtp-password ^
  smtp2worker
```

## TLS

默认建议只监听 `127.0.0.1` 或内网。如果必须暴露 SMTP 端口，请设置 SMTP AUTH，并考虑 TLS。

STARTTLS 需要 Python 3.11+：

```bash
python smtp2worker.py ^
  --worker-url https://smtp2worker.your-subdomain.workers.dev ^
  --bridge-token change-me ^
  --smtp-username smtp-user ^
  --smtp-password smtp-password ^
  --tls-cert-file cert.pem ^
  --tls-key-file key.pem ^
  --require-starttls
```

如果客户端需要 SMTPS，或你在 Python 3.8/3.9/3.10 上需要 TLS，把 `IMPLICIT_TLS=true` 或 `--implicit-tls` 打开。

## HTTP Payload

SMTP 网关会向 Worker POST：

```json
{
  "envelope": {
    "from": "sender@example.com",
    "to": ["user@example.net"]
  },
  "message": {
    "from": "Sender <sender@example.com>",
    "to": "user@example.net",
    "subject": "验证码",
    "text": "你的验证码是 123456",
    "html": "<p>你的验证码是 <b>123456</b></p>",
    "headers": {},
    "rawBase64": "..."
  },
  "smtp": {
    "helo": "app.local",
    "peer": "127.0.0.1",
    "receivedAt": 1777777777
  }
}
```

Worker 支持两个认证头，二选一即可：

```text
Authorization: Bearer <BRIDGE_TOKEN>
X-SMTP2WORKER-Token: <BRIDGE_TOKEN>
```

## 测试

```bash
python -B -m unittest discover -s tests
```
