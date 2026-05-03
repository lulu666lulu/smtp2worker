# smtp2http

一个小型 SMTP 到 HTTP 网关：旧工具只需要按普通 SMTP 发邮件，网关会把邮件解析成 JSON，转发给部署在 Cloudflare Workers 上的 HTTP 接口，再由 Worker 调用邮件服务商发送验证码邮件。

默认实现使用 Resend 的 HTTP API 发送邮件；也可以按同样的 JSON 结构改成你自己的发信接口。

## 目录

- `smtp2http.py`：SMTP 服务器，接收 `MAIL/RCPT/DATA` 后 POST 到 Worker。
- `worker/src/worker.js`：Cloudflare Worker，校验 token 后调用 Resend。
- `config.example.env`：SMTP 网关环境变量示例。
- `worker/wrangler.toml.example`：Worker 部署配置示例。

## 部署 Worker

进入 `worker` 目录，复制配置文件：

```bash
cp wrangler.toml.example wrangler.toml
```

把 `FROM_EMAIL` 改成你在 Resend 中已验证的发件地址。然后设置密钥：

```bash
wrangler secret put BRIDGE_TOKEN
wrangler secret put RESEND_API_KEY
wrangler deploy
```

`BRIDGE_TOKEN` 必须和 SMTP 网关使用的 `BRIDGE_TOKEN` 相同。部署完成后得到类似：

```text
https://smtp2http-mail-worker.your-subdomain.workers.dev
```

## 运行 SMTP 网关

本机直接运行，Python 3.8+ 即可：

```bash
python smtp2http.py ^
  --worker-url https://smtp2http-mail-worker.your-subdomain.workers.dev ^
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
python .\smtp2http.py
```

旧工具里的 SMTP 配置：

```text
Host: 127.0.0.1
Port: 2525
Username: smtp-user
Password: smtp-password
Encryption: none
From: 你在 Resend 验证过的发件地址
```

Worker 默认使用 `FROM_EMAIL` 覆盖 SMTP 邮件里的 From，这是为了避免邮件服务商因为发件地址未验证而拒绝。

## Docker

```bash
docker build -t smtp2http .
docker run --rm -p 2525:2525 ^
  -e WORKER_URL=https://smtp2http-mail-worker.your-subdomain.workers.dev ^
  -e BRIDGE_TOKEN=change-me-to-a-long-random-secret ^
  -e SMTP_USERNAME=smtp-user ^
  -e SMTP_PASSWORD=smtp-password ^
  smtp2http
```

## TLS

默认建议只监听 `127.0.0.1` 或内网。如果必须暴露 SMTP 端口，请设置 SMTP AUTH，并考虑 TLS。

STARTTLS 需要 Python 3.11+：

```bash
python smtp2http.py ^
  --worker-url https://smtp2http-mail-worker.your-subdomain.workers.dev ^
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
X-SMTP2HTTP-Token: <BRIDGE_TOKEN>
```

## 测试

```bash
python -B -m unittest discover -s tests
```
