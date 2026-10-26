# Emby 智能模拟播放保活系统 (Emby Keepalive)

专为 Emby 公益服设计的防清理、高仿真实播放保活系统。

## ✨ 特性

- **匀速平滑拉流 (Paced Streaming)**：采用 64KB 细粒度分块与时间流控，像真实播放器一样按播放秒数均匀消耗带宽，绝无瞬间突发打满带宽的下载尖峰。
- **流量与时长严密对应**：根据影视真实码率（16~26 Mbps）动态计算播放时长，流量（150MB~250MB 浮动）与播放时长在物理上 100% 严丝合缝。
- **日期与时间双随机**：
  - **周内随机日期**：每日唤醒，周内随机挑选一天打卡，周末自动保底。
  - **时间点随机延迟**：唤醒后随机延迟 0~15 分钟再执行，时间点绝不固定。
- **定时心跳同步**：边匀速拉取流边每 10 秒向服务器上报播放进度。
- **代理支持**：原生支持 HTTP/HTTPS 代理，配套支持 SOCKS5 代理。
- **兼容性**：完美兼容 Emby 4.8 / 4.9+ 新版 PlaySession 租约机制。

---

## 🚀 部署与使用

### 1. GitHub Actions 全自动云端运行（免服务器）

仓库已内置工作流 `.github/workflows/keepalive.yml`。

在仓库的 `Settings` -> `Secrets and variables` -> `Actions` 中配置以下 Secrets（用户名和密码必填，不再在代码中保存凭据）：

| Secret 名称 | 说明 |
| :--- | :--- |
| `EMBY_HOST` | 服务器地址 (如 `https://lite.cn2gias.uk`) |
| `EMBY_USER` | 账号用户名 |
| `EMBY_PASS` | 账号密码 |
| `PROXY_URL` | 代理地址 (可选，如 `http://proxy:port`) |

### 2. 本地 / VPS 运行

先通过环境变量设置 `EMBY_HOST`、`EMBY_USER`、`EMBY_PASS`。`pass.txt` 仅供本地保存凭据，脚本不会自动读取，也不要将其提交到仓库。

```bash
# 快速测试（跑 5MB 流量、10秒时长）
python3 emby_keeper.py --test

# 立即强制执行一次完整保活
python3 emby_keeper.py --force

# 指定代理运行
python3 emby_keeper.py --proxy http://127.0.0.1:7890
```

### UA 白名单

默认使用 `hills` 预设，Actions 也显式设置 `CLIENT_PRESET: hills`。登录、API 和媒体流请求统一使用 `Hills/1.3.1 (iOS; iPhone15,2; Scale/3.00)`，认证客户端标识同步为 Hills。此值为固定兼容预设，并非实时获取的播放器最新 UA。

### 登录失败排查

- `401 invalid_credentials`：请求已到达服务器，但认证被拒绝。核对服务器地址及 Actions Secrets，注意 Secret 中不要额外添加引号或换行。网页登录成功不代表 Actions 使用的是同一组配置或同一网络出口。
- 登录请求同时提交 `Pw` 和 `Password`，兼容采用不同密码字段的服务端/认证网关，不自动重试登录。
- `429 rate_limited`：服务器已限制认证请求，应暂停运行，等待限制解除后再验证；不要连续点击运行。
- 凭据曾经写入代码时，删除当前代码中的值并不能清除 Git 历史，建议更换密码并更新 Secrets。
