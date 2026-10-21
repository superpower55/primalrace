# Gemma 4 12B on Oracle Cloud (Always Free)

在 Oracle Cloud 免费的 Ampere A1 虚拟机（4 OCPU / 24 GB 内存，ARM，纯 CPU）上自托管
Gemma 4 12B，替代 Ollama Cloud 上的 Gemma 4 31B。

```
GitHub Actions ──HTTPS + Bearer token──▶ Caddy (:443) ──▶ Ollama (127.0.0.1:11434) ──▶ gemma4:12b
```

Ollama 只监听本机；对外只有 Caddy，它负责自动 HTTPS 证书和 token 校验，没有 token 一律 401。

## 1. 创建虚拟机

1. Oracle Cloud Console → **Compute → Instances → Create instance**
2. Image：**Canonical Ubuntu 24.04**（或 22.04）
3. Shape：**Ampere → VM.Standard.A1.Flex**，**4 OCPU / 24 GB**（Always Free 上限）
4. 启动盘 ≥ 50 GB（模型约 8 GB）
5. 上传 SSH 公钥 → Create

A1 资源经常显示 "Out of capacity"，换个 Availability Domain 或过几小时再试。

## 2. 开放端口

Instance → **Subnet → Security List → Add Ingress Rules**：

| Source CIDR | Protocol | Port |
|---|---|---|
| `0.0.0.0/0` | TCP | 80 |
| `0.0.0.0/0` | TCP | 443 |

（80 只用于 Let's Encrypt 签证书。系统内的 iptables 由脚本处理。）

## 3. 域名（可选）

建议加一条 DNS A 记录，比如 `ollama.howtoraiseiq.com → <VM 公网 IP>`。
不加的话脚本会用 `<ip>.sslip.io`，也能拿到 HTTPS 证书。

## 4. 运行安装脚本

```bash
scp news/oracle/setup.sh ubuntu@<VM IP>:
ssh ubuntu@<VM IP>
sudo DOMAIN=ollama.howtoraiseiq.com bash setup.sh
```

脚本会安装 Ollama 和 Caddy、拉取 `gemma4:12b`、配置防火墙并预热模型，
最后打印出需要填到 GitHub 的值。

## 5. 配置 GitHub

repo → **Settings → Secrets and variables → Actions**：

| 类型 | 名称 | 值 |
|---|---|---|
| Variable | `OLLAMA_BASE_URL` | `https://ollama.howtoraiseiq.com` |
| Variable | `OLLAMA_MODEL` | `gemma4:12b`（可省略，默认就是它） |
| Secret | `OLLAMA_SELFHOST_TOKEN` | 脚本打印的 token |

`cron.yml` 以 `OLLAMA_BASE_URL` 是否设置作为开关：
- **设置了** → 走 Oracle VM 上的 12B，`num_ctx=32768`，文章正文截断到 12000 字符，每轮最多 3 篇；
- **删掉它** → 立即回退到 Ollama Cloud 的 `gemma4:31b-cloud`（原来的 `OLLAMA_API_KEY` 不动）。

## 性能预期

纯 CPU 推理，比云端慢很多：

- 系统提示（含书摘）约 2.5 万 token。第一次请求要完整算一遍，可能十几分钟；
  之后模型常驻内存（`OLLAMA_KEEP_ALIVE=-1`），同样的前缀会复用 KV cache，只需处理新闻正文。
- 生成速度大约每秒几个 token，一篇评论（中英双语约 800–1300 token）预计 5–10 分钟。
- 所以每轮上限 3 篇，workflow 超时放宽到 45 分钟，并且同一时间只跑一个 workflow
  （`concurrency`），避免多个请求同时压在一台机器上。抓到的其余链接留在 `urls.txt` 排队。

如果积压太多，可以把 `MAX_PER_RUN` 调低、增大 cron 间隔，或者临时删掉 `OLLAMA_BASE_URL` 切回云端。

## 维护

```bash
systemctl status ollama caddy        # 服务状态
journalctl -u ollama -f              # Ollama 日志
sudo cat /etc/ollama-proxy-token     # 查看 token
sudo TOKEN=<new> DOMAIN=... bash setup.sh   # 换 token（同步更新 GitHub secret）
ollama pull gemma4:12b               # 更新模型
```

Oracle 会回收长期闲置的 Always Free 实例（CPU 利用率持续很低时）。这里每 10 分钟
都有推理任务，一般不会被判定为闲置。
