# API 契约（现行有效版）

> **这是唯一按接口组织的现行契约**。历史决策与踩坑见 `docs/history/`。
> **维护规则**：任何改动 `src/serve.py` 的 `/api` 的人（人或 AI），改完必须同步更新本文件对应条目，并在文末「变更记录」追加一行。

## 一、总则

- 服务端口 **8766**（测试惯例用 8799）；仅本机 HTTP，无 HTTPS（公网走 ngrok 隧道）。
- **来源三级判定**（`serve._peer_class`，按 TCP 对端 IP，ngrok 转发流量按 `X-Forwarded-For` 最右侧一节还原）：
  - `local` 回环直连 —— 全权
  - `lan` 私有网段（10.x / 172.16-31.x / 192.168.x / 169.254.x）—— **视同本机信任**
  - `public` 其他（ngrok 访客/互联网）—— 完整安全链
- **鉴权矩阵**：

| 请求 | local | lan（未设密码） | lan（已设密码） | public |
|---|---|---|---|---|
| GET 数据接口（白名单外） | 放行 | 放行 | 需令牌 | 需令牌 |
| 敏感静态 `result.json` / `silver_cache.json` | 放行 | 放行 | 放行 | **需令牌** |
| POST 写接口 | 放行 | 放行 | 需令牌 | 需令牌 |
| `/api/pass-set`、配对管理、清库、`launch/config`、`ngrok/start`、`ngrok/token` | 允许 | **403 仅限本机** | **403 仅限本机** | **403 仅限本机** |

- **令牌三级 scope**（`/api/auth` 签发，内存态，exe 重启即失效）：
  - `local-ui` —— 全权（本机页面、局域网密码登录）
  - `device` —— 已配对设备：可读写报表/触发启动，**不能**改密码/清库/改启动配置/ngrok（`_SCOPE_SENSITIVE`）
  - `legacy-readonly` —— 公网未配对旧客户端：只读（写接口除 `/api/lowstock` 外 403）
- **安全开关 4 个**（`/api/pair/switches`，仅本机可改，默认值加粗）：
  `pairing_required`=**开**（公网必须配对才能看数据，App/浏览器一视同仁）、`allow_legacy_readonly`=**开**（pairing 关时公网密码登录降级只读）、`allow_remote_launch`=**关**（公网手机远程启动估价器）、`ngrok_need_pair`=**开**
- 全部 POST 要求 `Content-Type: application/json`（否则 415）；`/api/auth`、`/api/pair/request` 免令牌。
- Host 白名单（防 DNS-rebinding）：Host 必须是 localhost/本机 IP/当前 ngrok 域名，否则 403。
- 路径双斜杠自动归一（`//api/x` → `/api/x`）。
- **大响应自动 gzip**（2026-08-30）：JSON 响应 >64KB 且客户端声明 `Accept-Encoding: gzip` 时自动压缩（生涯全量 48MB→7MB）。浏览器/HttpURLConnection 透明解压，客户端无需改动。
- **令牌可用 Cookie 携带**（2026-08-30）：`bk_token=<token>`（Cookie 头）与 `X-Auth-Token` 头等效。App 连接成功时把令牌写入 WebView Cookie，页面首个请求即带令牌，消灭"页面先请求、令牌后注入"的 401 竞态。
- 静态资源白名单：`.html/.js/.css/.csv/.json/.png/.jpg/.ico/.svg/.woff2` + `result.json/silver_cache.json/v233_items.json`；点开头文件一律拒绝。
- **静态文件条件请求**（2026-08-30）：所有静态响应带 `ETag`（mtime-size）+ `Cache-Control: no-cache`；请求带 `If-None-Match` 且命中 → **304 空响应**。客户端 fetch 别再写 `cache:"no-store"` 才能吃到 304（result.json/silver_cache.json 已改）。

## 二、认证

### POST /api/auth（免令牌）
两种登录：
- **设备登录** `{device_id, nonce, ts, sig}`：`sig = HMAC-SHA256(device_key, "auth|"+server_id+"|"+nonce+"|"+ts)`，nonce 经 challenge 获取且一次性（2 分钟），时钟偏差 ≤5 分钟 → `scope=device`
- **密码登录** `{pwd}`（2026-08-30 起**公网带试错锁定**：连续错 5 次 → 锁 15 分钟，锁定期内一切密码尝试返回 **429** `{"error":"尝试过于频繁","msg":"…约 N 分钟后可再试"}`；本机/局域网不受限；校验通过即清零）：
  - local/lan → `scope=local-ui`（全权）
  - public → `pairing_required` 开（默认）→ **403** `{"error":"该设备尚未配对，公网访问需要在电脑端「安全与配对」点同意","code":"pairing_required"}`（App/网页凭「尚未配对」转配对流程）；关 → `allow_legacy_readonly` 开=200 `legacy-readonly` / 关=403
- 密码错误 → 401 `{"ok":false,"error":"密码错误"}`
- 成功 → `{"ok":true,"token","scope","pass_enabled"}`

### GET /api/auth/challenge?device_id=
免令牌 → `{"nonce","server_id","ts"}`

## 三、设备配对（公网准入）

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | /api/pair/request | 免令牌 | `{device_id,name,model,pwd}` → `{rid}`；错误：密码错误 / 待处理过多（上限 3，5 分钟过期）/ **429 尝试过于频繁**（公网密码试错锁定，同 /api/auth，2026-08-30）。**2026-08-30 起**：device_id 已在配对表中时改为**自动重新绑定**——密码正确 → 200 `{ok,auto_paired:true,device_key,server_id,token}`（免二次人工同意，同一设备只需同意一次）；密码错误 → 400 `密码错误`。不再返回 400「该设备已配对」 |
| GET | /api/pair/status?rid&did | 免令牌 | `{state}`；**approved 时一次性下发** `device_key` + `browser_token`（device 级令牌，给网页用）+ `server_id`，随后该请求作废 |
| GET | /api/pair/pending、/api/pair/devices | 仅本机 | 待处理列表 / 已配对设备+开关 |
| POST | /api/pair/approve、/reject、/revoke、/switches | 仅本机 | approve `{rid}`；revoke `{device_id}`（立即作废其令牌）；switches `{switches:{...}}` |
| GET | /api/pair/notice | 仅本机 | 换机/拷贝安全重置提示（读后即删） |

## 四、密码

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | /api/pass-set | 仅本机 | `{pwd}`（空=清除）；PBKDF2 存储；改密即清空全部令牌 |
| GET | /api/pass-info | 仅本机 | `{pass_enabled, hint}`（不再返回明文） |

## 五、报表数据

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/status | 解析状态 `{status, code, msg}`（App 自动同步靠它） |
| GET | /api/server-info | 版本协商：`version/port/pass_enabled/api_version/min_app/app_version/app_signature/peer/pairing_required/pairing_needed/server_id`；`lan_url` 仅对本机/局域网来源返回 |
| GET | /api/players | 候选玩家 |
| GET | /api/career-db-status | 生涯库状态 `{enabled,total_games,...}` |
| GET | /api/career-data?uid=&since_ts= | 生涯对局（无分页，量大，>64KB 自动 gzip）。**2026-08-30 起**：①每条对局注入 `uid` 字段（前端本地分账过滤用）；②`since_ts` 给定时只返回 `ts >= since_ts` 的新局（增量拉取，配合客户端 IndexedDB 缓存；`>=` 语义防同秒新局漏拉） |
| GET | /api/item-prices | 当前生效价格表（基础+覆盖） |
| POST | /api/item-prices | 保存覆盖价格并触发重解析 |
| POST | /api/item-add、/api/item-delete | 自定义物品（基础表内 cid 拒绝增删） |
| GET | /api/missing-items、/api/missing-items/auto | 缺失拍品扫描（auto 消费型标志） |
| POST | /api/lowstock | 低库存道具（`_LEGACY_POST_ALLOW` 唯一放行的只读 POST） |
| POST | /api/refresh | `{uid}` 触发后台解析（scope：local-ui/device 可用） |
| POST | /api/exit | **恒 403**（远程退出已停用，接口名保留） |
| GET | /api/json-save-warning；POST /api/json-save/save、/discard、/ack | result.json 局数减少提醒 |

## 六、启动估价器（launcher）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/launch | 触发启动白名单程序（`竞拍之王全自动估价器.exe`，请求体忽略）。local/lan 放行；public 需 `allow_remote_launch` 开。已运行则置顶+点卡密弹窗 |
| GET | /api/launch/status | `{state: idle/launching/confirming/running/exited/error, confirm_pending, confirm_detail, ...}` |
| GET/POST | /api/launch/config | POST **仅本机**；只接受 `exact_path`（文件名必须匹配白名单）、`search_dirs`；SHA-256 哈希锁定 |

安全保留：文件名硬白名单、路径校验（拒 UNC/可移动盘根）、哈希锁定、请求体忽略。

## 七、ngrok 公网

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/ngrok/start | **仅本机 + 需已设密码 + 已存 token**；失败时 `state.error` 附 ngrok 日志原始原因（`.ngrok_last.log`） |
| POST | /api/ngrok/stop | 停隧道 |
| POST | /api/ngrok/token | 保存 authtoken（DPAPI 加密存储） |
| GET | /api/ngrok/state | `{running,url,error,token_saved,token_masked}` |
| POST | /api/ngrok/domain | **已删除**（2026-08-30，固定域名功能移除），现 404 |

## 八、生涯库管理

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/career-db-clean | 按 retention_days 裁剪（**仅本机**） |
| POST | /api/career-db-clear | 清空（**仅本机**） |
| POST | /api/career-import | ZIP 导入（body 二进制例外，限 50MB） |

## 九、网页与 App 约定

- 页面 4 个：`bidking_report.html`（主报表）、`bidking_career.html`、`bidking_item_prices.html`、`bidking_standalone.html`（拖拽解析）。
- **App 模式**：URL 带 `?app=1` → 页面裁剪电脑端专属 UI、不抢刷新控制锁；App 原生注入令牌到 `sessionStorage.bk_token`（页面 fetch 包装器自动附加，`result.json`/`silver_cache` 也附加）。
- 公网网页访客：页面加载先走 `ensureAccess()` —— 本机/局域网直放；公网走「密码 → 403 尚未配对 → 配对 → 拿 browser_token」；已配对浏览器可用 WebCrypto 挑战-响应静默重登。

## 十、变更记录（倒序）

- 2026-08-31：PC 启动加速（无 /api 接口变更，仅启动时序与本地缓存）：launcher 改为**先起服务+开报表页、后台再跑解析**（页面秒出旧结果，`/api/status` 轮询沿用现链路）；`extract_inventory` 引入 `inventory_cache.json`（日志同目录，按 log_size+tail_offset 增量，未变零 IO / 变大扫尾部 / 变小或损坏全量重建）；`_send_json` 静默 ConnectionAbortedError/BrokenPipeError（客户端提前断开的无害噪音）。接口行为与参数均不变，App 无需改动。
- 2026-08-31：App 端 v1.11 连接层重做（纯 App 端改动，无 /api 变更）：令牌按服务器 URL 加密持久化，回前台以 `GET /api/status`（401/403 判失效）做轻量续连探测替代重新登录；多服务器 tab 秒切；报表页注入 IndexedDB 本地缓存层 + result.json ETag 对账 + 软刷新（`window.__bkSoftRefresh`）。接口行为与参数完全不变，PC 端不随本版发布。
- 2026-08-30：公网密码在线试错锁定（连续错 5 次→锁 15 分钟，仅 public 计数、局域网不受限；/api/auth 与 /api/pair/request 的密码校验共用，锁定期 429）；报表页「🌐 公网 IP」改名「🌐 公网访问」（该域名非公网 IP，避免误导）；App 状态栏不再回显服务器地址；PC v1.4.0 / APK v1.8 (versionCode 9)。

- 2026-08-30：性能与体验优化三件套——`/api/career-data` 新增 `since_ts` 增量参数并给每条对局注入 `uid`；静态文件支持 ETag/304 条件请求；生涯页改 IndexedDB 缓存+增量合并+前端分账过滤（ngrok 打开从 2-3 分钟 → 版本未变时秒开）；App 端 fetch/XHR 包装器提前到 `onPageStarted` 注入 + 道具/生涯页加重试（修 ngrok 免费版警告页导致「无法连接价格数据库」）；PC v1.3.0 / APK v1.3 (versionCode 4)。
- 2026-08-30：`/api/pair/request` 已配对设备改为凭密码自动重绑定（`auto_paired`，免二次同意，修 App 重新添加/换地址后配对死锁）；App 端 device_id 改用 ANDROID_ID（重装不变）；`/api/pass-info` 本机判定改走 `_peer_class`（修 ngrok 访客可读密码状态的真实漏洞）；PC v1.2.0 / APK v1.2 (versionCode 3)。
- 2026-08-30：`/api/server-info` 新增 `app_version` / `app_signature`（版本署名，页面页脚用）；APK 开启 R8 混淆（v1.1）。
- 2026-08-30：本文档建立（自两份按日期追加的历史交接文档提炼，历史见 `docs/history/`）；`/api/ngrok/domain` 已随固定域名功能移除。
