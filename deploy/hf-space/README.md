# KOXPilot 服务的公网托管包（Hugging Face Spaces）

仓库里 `api/` 那份服务部署在内网环境，外部访问不到（DNS 解析到 `10.x` 私网地址）。
这个目录是同一份服务的**公网托管包**，用 Hugging Face Spaces 的 Docker 运行时，免费、公开、不需要信用卡。

跑起来之后，公网 Demo 就能真的调到 Python 服务，而不是只靠浏览器内引擎。

## 为什么选 HF Spaces

- 免费额度足够（这个服务只读数据、不写库、CPU 就够）
- 公开访问，不需要登录
- 直接吃 Dockerfile，不用改代码适配平台
- 冷启动后常驻，48 小时无访问会休眠，被访问时自动唤醒

## 十分钟部署

### 1. 建 Space

1. 打开 https://huggingface.co/new-space
2. Space name：`koxpilot-api`
3. License：随意（`mit` 即可）
4. **Select the Space SDK：选 `Docker` → `Blank`**
5. Space hardware：`CPU basic · free`
6. 可见性：**Public**
7. Create Space

### 2. 推代码

拿到 Space 的 git 地址后（形如 `https://huggingface.co/spaces/<你的用户名>/koxpilot-api`）：

```bash
# 在仓库根目录执行
python3 deploy/hf-space/build_space.py            # 把服务 + 数据打包到 deploy/hf-space/_out/

cd deploy/hf-space/_out
git init && git branch -M main
git remote add origin https://huggingface.co/spaces/<你的用户名>/koxpilot-api
git add -A
git commit -m "KOXPilot 服务"
git push -u origin main --force
```

推送时要求账号密码：用户名填 HF 用户名，密码填 **Access Token**
（https://huggingface.co/settings/tokens → New token → 权限选 `write`）。

数据文件 7.7 MB，走普通 git 就行，不需要 git-lfs。

### 3. 等构建完成并自测

Space 页面会显示 Building → Running。跑起来后：

```bash
curl -s https://<你的用户名>-koxpilot-api.hf.space/api/health
```

看到 `{"ok":true,"status":"ready",...}` 就成了。首个请求会懒加载 5,000 条数据集，
可能要几秒，第二次就快了。

### 4. 让公网 Demo 连上它

编辑 `web/public/api-config.json`，把 `apiBase` 填成 Space 地址（字段名就叫 `apiBase`，留空表示"不指定"）：

```json
{ "apiBase": "https://<你的用户名>-koxpilot-api.hf.space" }
```

提交并推到 GitHub，Pages 会自动重新构建。**这个文件是前端在首屏探活前运行时读取的，改它不需要改代码、也不需要重新构建前端**——已经发出去的静态产物里直接改 `dist/api-config.json` 同样生效（已实测：一份构建时没有任何服务地址的产物，只靠这个文件就连上了服务，页头显示"计算源：Python 服务"）。

优先级是 `api-config.json` 的非空 `apiBase` > 构建期 `VITE_API_BASE` > 同源。

改完打开 Demo，顶部应该显示「计算源：Python 服务」，而不是「浏览器引擎」。

## 可选：让 A1 真调模型

Space 页面 → Settings → Variables and secrets，加 Secret：

| 名称 | 值 |
| --- | --- |
| `MODEL_PROVIDER` | `ark` 或 `azure` 或 `openai` |
| 对应的 key / endpoint / model | 见仓库根 `.env.example` |

不配也能跑：A1 走确定性规则解析，响应里 `brief.parse_path` 会写 `rule`，
`brief.notes` 会说明原因。**不配置不会导致接口报错。**

## 注意

- 这个包里的服务代码和数据由 `build_space.py` 从 `api/` 复制而来，**不要手改 `_out/`**，
  改了下次构建会被覆盖。要改逻辑就改 `src/koxpilot/` 或 `api/`，然后重新跑一次脚本。
- 服务只读数据。数据集是固定种子的合成数据，不含任何真实达人信息。
