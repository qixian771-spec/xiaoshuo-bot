# WorkBuddy 经 CPA 登录与模型核验操作指南（给老公的中文步骤）

> **安全红线提醒**
> - CPA 只监听服务器本机 `127.0.0.1:8317`，**绝不开放公网端口**，全部管理操作通过 Mac 上的 SSH 本地端口转发隧道完成。
> - 服务器上 `auto_checkin_enabled` 已强制设为 `false`（**关闭自动签到**）；每日积分签到只在老公自己的 Mac 上手动领取。
> - 管理密钥（`secret-key`）和本地客户端密钥（`api-keys`）全部在服务器上现场随机生成，不进 Git 仓库。

---

## 第一步：在服务器上运行安装脚本（仅首次或重装时）

1. 登录服务器（假设主机别名为 `kr`），确认已安装 Go `1.27.1+` 与 `gcc`：
   ```bash
   ssh kr
   go version   # 需 >= go1.27.1（cpa-workbuddy 源码编译要求）
   gcc --version
   ```
2. 进入仓库目录后执行安装脚本（脚本会固定下载 CPA `v8.0.69` 并校验官方 SHA256，从源码拉取 `cpa-workbuddy` 并校验完整提交号 `2a1a608fb0ff28c51996393822a7f36df4367d5b` 后本地编译 `.so`，并现场生成 64 位随机密钥写入 `/opt/cli-proxy-api/config.yaml`）：
   ```bash
   sudo bash deploy/install_cpa_workbuddy.sh
   ```
3. 确认服务已启动且**只监听在 `127.0.0.1:8317`**：
   ```bash
   sudo systemctl status cpa.service --no-pager
   ss -lntp | grep 8317
   ```
   正常应只看到 `127.0.0.1:8317`（不能出现 `0.0.0.0:8317` 或 `* :8317`）。
4. 查看现场生成的密钥（稍后登录面板和写 `xiaoshuo.env` 要用，**不要贴到聊天记录或 Git 里**）：
   ```bash
   sudo grep -E 'secret-key:|api-keys:' -A 1 /opt/cli-proxy-api/config.yaml
   ```

---

## 第二步：在 Mac 上开 SSH 隧道打开 CPA 管理页面

1. 在老公的 Mac 终端里保持运行下面这条隧道命令（开着别关）：
   ```bash
   ssh -N -L 8317:127.0.0.1:8317 kr
   ```
2. 在 Mac 浏览器打开 CPA 管理界面（CPAMC）：
   - `http://127.0.0.1:8317`
   - 输入第一步查到的 `remote-management.secret-key` 登录管理台。
3. 进入「插件（Plugins）」页面，确认：
   - `cpa-workbuddy` 已加载且处于启用状态；
   - 打开插件面板 `http://127.0.0.1:8317/v0/resource/plugins/cpa-workbuddy/panel`，再次确认**自动签到（`auto_checkin_enabled`）处于关闭状态**（切勿在服务器上打开自动签到）。

---

## 第三步：老公亲自点授权链接完成 WorkBuddy 登录

1. 在 CPAMC 管理台进入 **OAuth 登录**（Auth / OAuth Login）。
2. 选择要登录的站点入口：
   - **WorkBuddy国内**（模型默认带 `wb/` 前缀）
   - 或 **WorkBuddy国际**（模型默认带 `wb-ai/` 前缀）
3. 点击生成登录链接，在 Mac 浏览器中打开该授权链接，使用老公自己的 WorkBuddy 账号完成扫码/授权确认。
   - 提示：单次登录会话有效期 **5 分钟**，超时重新点一次即可。
4. 回到 CPAMC 的「认证文件（Auth Files）」列表，确认刚刚授权的 WorkBuddy 账号已经出现且状态正常。

---

## 第四步：查询真实 `/v1/models` 并把 4 个白名单模型 ID 写入 `xiaoshuo.env`

1. 隧道保持开启时，在 Mac（或服务器上）用第一步生成的 `api-keys` 查询账号实际返回的模型列表：
   ```bash
   curl -s http://127.0.0.1:8317/v1/models \
     -H "Authorization: Bearer <替换为服务器config.yaml里的api-key>" \
     | python3 -m json.tool
   ```
2. 从返回结果里找出下面 **4 个白名单模型** 对应的**真实完整 ID**（带 `wb/` 或 `wb-ai/` 前缀，以 `/v1/models` 实际返回为准，不要猜）：
   - **Hy4 flash** → 填入 `WORKBUDDY_MODEL_HY4_FLASH`
   - **Hy4**（仅手动指定，不进自动轮换）→ 填入 `WORKBUDDY_MODEL_HY4`
   - **DeepSeek-V4.1-Flash** → 填入 `WORKBUDDY_MODEL_DEEPSEEK_V4_1_FLASH`
   - **Xiaomi MiMo-V2.6-Pro** → 填入 `WORKBUDDY_MODEL_MIMO_V2_6_PRO`
3. **严禁填入**：`DeepSeek-V4-Pro`、自动路由（`auto` / `快速` / `均衡` / `极致`）或白名单之外的任何模型；`xiaoshuo-bot` 启动自检会自动拦住并报错。
4. 在服务器编辑 `/etc/xiaoshuo-bot/xiaoshuo.env`（确保权限 `chmod 0600`），更新：
   ```ini
   CPA_MANAGEMENT_URL=http://127.0.0.1:8317
   CPA_MANAGEMENT_KEY=<服务器现场生成的 secret-key>
   XIAOSHUO_API_BASE=http://127.0.0.1:8317/v1
   XIAOSHUO_BASE_URL=http://127.0.0.1:8317/v1
   XIAOSHUO_API_KEY=<服务器现场生成的 api-key>

   WORKBUDDY_MODEL_HY4_FLASH=<实测 /v1/models 里的 Hy4 flash 精确 ID>
   WORKBUDDY_MODEL_HY4=<实测 /v1/models 里的 Hy4 精确 ID>
   WORKBUDDY_MODEL_DEEPSEEK_V4_1_FLASH=<实测 /v1/models 里的 DeepSeek-V4.1-Flash 精确 ID>
   WORKBUDDY_MODEL_MIMO_V2_6_PRO=<实测 /v1/models 里的 Xiaomi MiMo-V2.6-Pro 精确 ID>
   ```

---

## 第五步：重启 `xiaoshuo-bot` 验证启动自检

1. 重启机器人服务并查看日志：
   ```bash
   sudo systemctl restart xiaoshuo-bot.service
   sudo journalctl -u xiaoshuo-bot.service -n 50 --no-pager
   ```
2. 机器人启动时会自动请求 `http://127.0.0.1:8317/v1/models` 核对上面 4 个白名单模型：
   - 如果 4 个都存在且均符合白名单规则 → 服务正常启动；
   - 如果有任何一个填错、不存在，或误配了白名单以外的模型 → 服务会直接拒绝启动，并在日志里明确报出是哪一个模型有问题。
3. 完成后，在 Mac 上按 `Ctrl+C` 关闭 SSH 隧道即可。
