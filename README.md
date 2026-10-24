# Exam Answer Assistant · 在线考试答题助手

一个面向 **易班优课（yooc.me）形势与政策考试** 的作答辅助系统。

**核心特点**：题库命中即秒答，未命中则自动遍历电子教材求解，全程可视化操作。

> ⚠️ **仅限个人学习与技术研究使用。** 请遵守你所在学校的考试规定。
> 本项目的价值在于演示：SSO 登录链路逆向、浏览器保活、教材检索算法、分层置信度闸门。

---

## 目录

- [技术栈](#技术栈)
- [快速开始](#快速开始)
- [工作原理](#工作原理)
- [项目结构](#项目结构)
- [配置说明](#配置说明)
- [命令速查](#命令速查)
- [常见问题](#常见问题)
- [免责声明](#免责声明)

---

## 技术栈

### 运行时

| 组件 | 版本 | 用途 |
|---|---|---|
| Python | 3.10+ | 主语言 |
| aiohttp | ≥3.9 | Web 服务（操作台后端 + SSE 日志流） |
| Playwright | ≥1.40 | 浏览器自动化（Chromium） |
| requests | ≥2.31 | HTTP 请求（SSO / API 调用） |

### 前端

纯手写 HTML + CSS + 原生 JavaScript，**无构建、无框架、无 CDN 依赖**。
直接由 aiohttp 提供静态文件。这样做的原因：

- 零安装负担，`python src/app.py` 就能跑
- 不受 npm 生态版本漂移影响
- 单文件 `index.html` 便于审阅

### 数据

| 格式 | 用途 |
|---|---|
| JSON | 题库、会话配置、答案计划、回读快照 |
| 纯文本 | 教材语料（供检索） |

无数据库依赖。题库是单文件 JSON，可直接 diff、可版本控制（注意脱敏）。

### 关键技术点

| 技术 | 说明 |
|---|---|
| **SSO 凭证交换** | `yiban_user_token` → yooc 三项 cookie，必须 `allow_redirects=False` |
| **浏览器保活** | 单例 + 专属线程，避免反复过人机验证 |
| **双向夹逼检索** | 用 suffix 定位 + prefix 定边界，从教材原文反解填空答案 |
| **分层置信度闸门** | 按校准阈值决定「自动填 / 待复核 / 留空」 |
| **按文本匹配填写** | 用选项文字而非字母，规避前端渲染顺序错位 |

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
playwright install chromium
```

> `playwright install chromium` 是必须的，它会下载浏览器内核（约 150MB）。

### 2. 配置凭据

复制模板并填写：

```bash
cp data/session.example.json src/session.json
```

两种方式获取凭据，**推荐第一种**。

#### 方式 A：SSO 一键登录（推荐）

1. 浏览器打开 `https://www.yiban.cn/`，确认已登录
2. 按 `F12` → `Console` → 输入 `document.cookie` → 回车
3. 复制返回的整段字符串（**是 yiban.cn 的，不是 yooc.me 的**）
4. 运行：

```bash
python src/yiban_sso.py "yiban_user_token=xxxx; YB_SSID=yyy; ..."
```

脚本会自动走官方 SSO 换取 `user_token / yiban_id / user_id` 并写入 `src/session.json`。

校验：

```bash
python src/yiban_sso.py --check
```

#### 方式 B：直接粘贴 yooc cookie

在 `www.yooc.me` 登录后，F12 拿到 `user_token / yiban_id / user_id`，
手工填入 `src/session.json`。

### 3. 启动操作台

```bash
# Windows 双击
start.bat

# 或命令行
python src/app.py
```

浏览器打开 **http://127.0.0.1:8765**

### 4. 准备教材语料（可选，但强烈建议）

题库未命中时依赖教材求解。需要你把教材转成纯文本放进 `data/`：

```
data/corpus_textlayer.txt
```

支持任意份语料，程序会按文件名前缀 `corpus_*` 加载。
生成方式：PDF → 文本层直取（推荐）或 OCR。**PDF 本身不要提交到仓库**（版权）。

### 5. 完整作答流程

```
1. 操作台点「易班 Cookie 一键登录」→ 粘 cookie
2. 点「检查登录状态」确认有效
3. 点「⓪ 打开考试页」→ 手工点「重做试卷 → 开始考试」
4. 点「① 拉取试卷」      ← 需人工过人机验证
5. 点「② 生成答案」
6. 补红色「需人工」题（如有）
7. 点「③ 填写到网页」    ← 确认空值为 0
8. 前置浏览器窗口 → 复核 → 手工点「交卷」
9. 点「④ 回捞」（交卷后）
```

**人工只需两件事：过人机验证、点交卷。**

---

## 工作原理

### 登录链路

```
yiban.cn 登录
    ↓ 下发 yiban_user_token
GET https://www.yooc.me/yiban_account/sso?access_token=<yiban_user_token>
    ↓ 302 + Set-Cookie（关键：响应头里才有）
user_token / yiban_id / user_id
    ↓
写入 session.json，后续所有 API 调用使用
```

> **踩坑提醒**：三项 `Set-Cookie` 只存在于那个 `302` 响应头。
> 如果 HTTP 库自动跟随重定向，就看不到它们，表现为「成功但没 token」。
> 必须 `allow_redirects=False`。

### 答案分层

| 层级 | 条件 | 处理 | 实测准确率 |
|---|---|---|---|
| L0 题库 | 题干归一化命中 | 直接采用 | **100%** |
| L1 教材·自动 | 置信 ≥ 0.90 | 自动填入 | **98.6%** |
| L2 教材·待复核 | 0.70 ~ 0.90 | 填入并标黄 | ~88% |
| L3 低置信 | < 0.70 | **留空** | — |

**设计原则：宁可留空，也不瞎填。** 填错扣分，空着还能人工补。

### 教材求解：双向夹逼

问题：题干被改写过、页码不准、OCR 有断行，直接「找题干」不可行。

做法：

```
教材原文：…防范化解重点领域风险，房地产领域，核心在于稳市场、保民生。…
                          ↑答案        ↑suffix 锚点
题干：    防范化解重点领域风险，{input}，核心在于稳市场、保民生。
                          ↑prefix        ↑suffix
```

1. 用 `suffix`（空位后文字）在教材中定位，要求唯一命中
2. 用 `prefix`（空位前文字）向左对齐左边界
3. 两者之间即答案

对着真实试卷调优后，修复了三类系统性缺陷：

| 缺陷 | 现象 | 修法 |
|---|---|---|
| 填空溢出 | 答案带引号或跨句粘连 | `clean_answer()` 剥离两端标点句读 |
| 判断题全错 | 陈述匹配不上就判「错」 | 分块覆盖率 + 缺证据倾向「真」 |
| 多选漏选 | 检索窗口过窄 | 窗口扩至 1600 字 + 完备性回补 |

效果：**74% → 86%（+12pp）**，高置信段（≥0.90）达 **98.6%**。

### 置信度阈值校准

阈值不是拍脑袋定的，是跑评测扫出来的：

| 阈值 | 采纳数 | 准确率 | 覆盖率 |
|---|---|---|---|
| 0.85 | 98 | 85.7% | 49% |
| **0.90** | 72 | **98.6%** | 36% |
| 0.92 | 66 | 100.0% | 33% |

选 0.90 —— 准确率逼近上限且覆盖率不至于太低。

### 浏览器保活

早期实现每次操作都 `launch → close` 一个全新浏览器，后果：

- 每次全新 context，会话不共享 → **人机验证反复过**
- 窗口开了又关，观感差
- 流程结束关闭浏览器 → **无法人工交卷**

现架构：全局单例 `BrowserKeeper`，一条专属工作线程
（Playwright 同步对象有**线程亲和性**，跨线程会崩），
首次操作时打开，之后全部复用，**流程结束保持打开**。

---

## 项目结构

```
.
├── start.bat                 # Windows 一键启动
├── requirements.txt
├── README.md
├── .gitignore
│
├── src/
│   ├── app.py                # Web 服务 + 全部 API
│   ├── browser_keeper.py     # 浏览器保活单例
│   │
│   ├── yiban_sso.py          # 官方 SSO 换凭证
│   ├── login.py              # Cookie / 密码登录
│   ├── qr_login.py           # 易班 App 扫码（线程安全）
│   ├── scan_to_yooc.py       # 扫码 → SSO → 考试页
│   │
│   ├── answer_engine.py      # 答案引擎 v2（教材求解核心）
│   ├── orchestrator.py       # 编排 + 置信度闸门
│   ├── qbank.py              # 题库：harvest/stats/conflicts
│   │
│   ├── textbook_verify.py    # 教材反向验证（三证据打分）
│   ├── boundary_solver.py    # 早期边界求解器（v1，保留对照）
│   │
│   ├── auto_run.py           # 浏览器自动化 JS 与流程
│   ├── gate.py               # 交卷前三道门禁
│   ├── session.py            # 会话配置读写
│   ├── preflight.py          # 开考前自检
│   ├── yooc_answers.py       # 答案字段解密
│   │
│   ├── eval_engine.py        # 多卷对比评测
│   ├── eval_fallback.py      # 兜底准确率专项
│   └── build_eval_set.py     # 构建评测集
│
├── data/
│   ├── qbank.json            # 题库（开源版为空，运行时增长）
│   ├── session.example.json  # 配置模板
│   └── corpus_*.txt          # 教材语料（用户自备，不入库）
│
└── docs/
    ├── PROJECT.md            # 架构与算法详解
    └── USAGE.md              # 面向使用者的操作说明
```

---

## 配置说明

### session.json

```json
{
  "token":       "user_token",
  "yiban_id":    "你的 yiban_id",
  "user_id":     "你的 user_id",
  "group_id":    "课群 ID",
  "exam_id":     "考试 ID",
  "examuser_id": ""
}
```

也可用环境变量覆盖（优先级更高）：

```
YOOC_TOKEN / YOOC_YIBAN_ID / YOOC_USER_ID / YOOC_GROUP_ID / YOOC_EXAM_ID
```

### 置信度阈值

在 `src/orchestrator.py` 顶部：

```python
GATE_AUTO = 0.90    # ≥ 自动采纳
GATE_SOFT = 0.70    # ≥ 采用但需复核
GATE_MIN  = 0.55    # < 一律留空
```

**改阈值前必须跑评测**，确认准确率没有下降：

```bash
python src/eval_engine.py --compare
```

---

## 命令速查

```bash
# 登录
python src/yiban_sso.py "yiban_user_token=xxx; ..."   # SSO 登录
python src/yiban_sso.py --check                       # 校验（走业务接口）
python src/login.py paste "<cookie>"                  # 粘 yooc cookie
python src/scan_to_yooc.py                            # 扫码→SSO→考试页

# 启动
python src/app.py                                     # 操作台 :8765

# 题库
python src/qbank.py stats                             # 规模
python src/qbank.py conflicts                         # 冲突检查
python src/qbank.py harvest <examuserId>              # 交卷后回捞

# 评测
python src/eval_engine.py --compare                   # 多卷对比
python src/eval_engine.py --mode single --corpus data/corpus_textlayer.txt

# 门禁
python src/gate.py --paper <paper.json> --answers <ans.json> --readback <rb.json>
```

---

## 常见问题

### SSO 返回「成功但没有 token」

HTTP 库自动跟随了 302，`Set-Cookie` 被吞。
本项目已用 `allow_redirects=False` 处理，若你二次开发请保留。

### 登录显示成功但其实无效

注意：站点的 `/user/check` 是**宽松接口**，对任意 token 都返回 `result:true`。
判断登录有效性必须用业务接口 `list/get`。本项目已改用业务接口。

### 提示「浏览器未打开」

浏览器单例只在**服务进程内**有效。
在独立脚本里调 `get_keeper()` 拿到的是另一个实例。
请通过 `/api/browser/open` 或操作台按钮打开。

### 题库一直是空的

开源版 `data/qbank.json` 刻意留空（原始题库含个人作答数据，已脱敏移除）。
每次交卷后点「回捞」会逐步积累。

### 教材语料从哪来

自行准备。推荐用 PDF 文本层直取（最准确），而不是 OCR。
**不要把教材 PDF 提交到仓库**，有版权问题。

### 人机验证能绕过吗

不能。数美 / 网易易盾是商业级验证码，设计上就不可自动化。
这是站点行为，不是工具缺陷。

---

## 免责声明

本项目仅供**技术学习与研究**，演示以下工程实践：

- Single Sign-On 凭证交换链路的分析与实现
- 长生命周期浏览器进程的管理（线程亲和性、单例）
- 基于语料的模糊检索与置信度评估
- 分层决策闸门（宁缺毋滥的取舍）

**请勿用于违反考试纪律的用途。** 使用者需自行承担全部责任，
作者不对任何滥用行为及其后果负责。

若相关平台方认为本项目存在不妥，请联系删除。
