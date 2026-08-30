# AGENTS.md —— 本项目协作守则（AI 助手与开发者改代码前必读）

BidKing 竞拍盈亏统计工具：解析游戏日志生成竞拍盈亏报表，网页查看，手机 App 远程查看 + 远程启动估价器。
PC 端 Python（`src/`，Nuitka 单文件 exe），安卓端 Kotlin WebView 壳（`app-android/`）。

## 改动流程（硬性）

1. **改前**：`git status` 确认干净；涉及 `/api` 先读 `docs/api-contract.md`；涉及端边界先读下文「端边界」。
2. **改后**：`python src/tests/run_tests.py` 必须全绿；改 html 必过 JS 语法检查（打包已内置，手动改时自查）。
3. **踩坑**：每踩一个新坑，解决后**当场**按模板追加到 `docs/防踩坑指南.md` 对应分类——这是本项目的存续规则，坑不记录就会重踩。
4. **提交**：一个逻辑改动一个 commit；凭据/用户数据永不入库（见 `.gitignore`）。
5. **契约**：改了 `/api` → 同步更新 `docs/api-contract.md` 对应条目 + 文末变更记录加一行。
6. **部署**：exe 改动 → `python src/build_nuitka.py`（产物进根目录 + 拷贝 `最终交付/`）；App 改动 → gradle 构建并拷贝 APK。**两端都改 = 两个产物都要重打并同步。**

## 端边界纪律

- **App 端需求必须在 App 端实现**（WebView 注入 `MainActivity.kt`），不得塞进 exe 内嵌网页——否则用户不重打 exe 就看不到。
- exe 的 `/api` 是两端唯一契约：改前确认对 App 的影响，改后在契约文档写清「影响 App 的点」。
- 生涯/价格等数据归 exe 管；App 只读接口 + 少量触发（refresh/launch/lowstock）。

## 安全模型（既定原则，改动须用户明确同意）

- **局域网视同本机信任**：未设密码免鉴权，设了密码凭密码即全权；不要求配对。
- **公网（ngrok）走完整安全链**：密码 + 电脑端人工「同意」+ 设备密钥；「新设备必须配对」默认开。
- **安全管理仅限本机**：改密码、配对管理、清库、启动配置、ngrok 启动，远程一律 403。
- **用户数据不可触碰**：`生涯数据库/career.db`、`item_prices.db`、`result*.json` 只读使用；任何重置/清理不得动它们。
- **凭据绝不入库**：`.bidking_sec.bin`、`.bidking_pass`、`.ngrok_token*`、keystore。

## 红线（历史事故沉淀，勿再踩）

- exe 入口是 `src/bidking_launcher.py`，新增命令行参数两处都要接（见 `docs/开发指南.md` 第六节）。
- 改 html 必过 JS 语法检查；Proxy 包原生对象必须补 `get` 陷阱。
- 验证 `/api/launch` 前先把配置指向不存在的路径，否则会真实拉起估价器。
- 临时诊断 UI 用完当版就删；临时脚本用完即删，不入库。
- 打包/验证前清理 8766 端口僵尸进程。

## 文档地图

- 使用者：`README.md`、`安全须知.md`、`最终交付/使用说明.md`
- 开发者/AI：`docs/开发指南.md`、`docs/api-contract.md`、`docs/防踩坑指南.md`（每踩一坑必追加）、`AGENTS.md`（本文）
- 历史：`docs/history/`（只增不改；含个人信息，**不入库、不随开源分发**）
