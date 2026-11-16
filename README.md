# Save WeChat chat history as common files

把 Linux 微信 4.x 的本地聊天记录解密并导出为通用格式文件（CSV / TXT / Word / SQLite / Markdown / JSONL），
带 Web 图形界面和 MCP 服务，**零第三方 Python 依赖**（仅标准库 + 系统 `sqlcipher` / `gdb`）。

> 适用于：把自己电脑上、自己微信账号里的聊天记录导出备份为普通文件（如归档、打印、数据分析、
> 喂给本地大模型）。全程本地处理，不上传任何数据。

## ⚠️ 使用范围（先读这个）

本工具**仅限导出使用者本人设备、本人微信账号的聊天记录**：

- ✅ 你在自己电脑上导出自己的聊天记录
- ✅ 你把本工具发给亲友，他们在**他们自己的电脑**上自行运行、自行导出（工具目录 `./start.sh` 一条命令）
- ❌ 未经他人知情同意，在他人设备上收集、读取、外传他人的聊天记录
- ❌ 收集他人的微信账号密码等凭据（本工具任何功能都不需要、也绝不应索取账号密码）

违反上述范围使用，可能触犯《刑法》第 253 条之一（侵犯公民个人信息罪）、第 285 条
（非法获取计算机信息系统数据罪）等规定。

## 功能

- **解密**：Linux 微信 4.1+ 的 WCDB/SQLCipher4 加密数据库（自动抓取密钥，无需手工逆向）
- **选择**：单个 / 多个 / 全部联系人（好友、群聊、公众号），按名称/备注/wxid 模糊匹配
- **格式**：`.csv`（Excel 友好）、`.txt`、`.docx`（零依赖生成）、`.db`（标准 SQLite）、`.md`、`.jsonl`，多联系人可打包 zip
- **界面**：零依赖 Web UI（`./start.sh` 一条命令，浏览器操作：抓密钥向导 → 勾选联系人 → 勾格式 → 下载）
- **MCP**：内置 MCP server，可在支持 MCP 的 AI 客户端中用自然语言调用
  （`wx_status` / `wx_list_contacts` / `wx_export_chats`）
- **解析**：zstd 压缩消息解压、引用回复、音乐/链接卡片、文件、动画表情、通话时长等常见消息类型

## 简单架构

```
┌─────────────────────────────────────────────────────────────┐
│ ELF 静态分析     在微信二进制中定位 WCDB Cipher 配置函数        │
│      ↓                                                       │
│ gdb 断点抓取     微信「退出账号→重新登录」瞬间读寄存器           │
│                 得 32 字节 passphrase（4.1+ 仅登录时经过内存）  │
│      ↓                                                       │
│ PBKDF2 派生      passphrase + 各库 salt → enc_key             │
│                 HMAC-SHA512 逐库验证（SQLCipher4 参数）         │
│      ↓                                                       │
│ sqlcipher 解密   只读快照 → 明文 SQLite（缓存于 ~/.wxdump/plain）│
│      ↓                                                       │
│ 消息解析         三库合并 / server_id 去重 / zstd 解压 /        │
│                 local_type 低32位 / appmsg 子类型渲染           │
│      ↓                                                       │
│ 多格式导出       CSV TXT DOCX SQLite MD JSONL + zip            │
└─────────────────────────────────────────────────────────────┘

入口层（CLI / Web UI / MCP）共用同一核心 wxdump_core/：
  elf_analyzer  key_capture  key_derive  db_access
  chat_store    msg_render   exporters   web_server  mcp_server  android
```

## 快速开始

**一键方式（推荐）：**

```bash
./start.sh        # 依赖检查+自动安装、权限放行、启动界面并自动打开浏览器
```

**手动方式：**

```bash
sudo apt install sqlcipher gdb            # 系统依赖
sudo sysctl kernel.yama.ptrace_scope=0    # 抓密钥期间放开（之后可改回 1）

python3 wxdump.py status                  # 环境体检
python3 wxdump.py capture-key             # 首次：按提示在微信中退出账号并重新登录
python3 wxdump.py contacts                # 列出联系人及消息数
python3 wxdump.py export --contacts "张三" --formats csv,txt,docx,db,md,jsonl --out ./exports
python3 wxdump.py export --all --formats txt,md --zip
python3 wxdump.py ui --port 8765          # Web 界面
python3 wxdump.py mcp                     # MCP server（stdio）
```

要求：Linux x64、Python ≥ 3.10（3.14 最佳）、微信 4.x 已登录。分发包：`./pack.sh`
生成 `dist/wxdump-linux-x64.zip`，发给对方解压 `./start.sh` 即用。

## 使用限制

| 平台 | 支持情况 |
|---|---|
| Linux 微信 4.x（x64） | ✅ 完整支持（已在微信 4.1.13.9 实测） |
| Windows | ❌ 不支持（密钥提取机制不同：PE/内存扫描，需另行移植，本工具不含） |
| macOS | ❌ 不支持（LLDB/ Mach-O 路径未实现） |
| Android | ⚠️ 引擎复用型：`android scan/pull/key` 支持 root 拉库与密钥导入；微信 8.0+ 密钥需自行用 Frida 等工具提取后导入；解析器当前面向 Linux 库结构 |

其他限制：
- 微信 4.1+ 密钥函数仅在登录时被调用，因此**首次抓取密钥必须配合一次「退出账号 → 重新登录」**（工具会引导）
- 导出为文本快照，不含图片/语音/视频的二进制内容（以占位符标注）
- 微信版本大更新可能改变挂点/库结构，需等待适配

## 免责声明

1. 本项目仅供**使用者本人设备、本人微信账号**的数据导出与备份之用。使用者对自身使用行为的合法性负责。
2. 本项目与腾讯无关，未获得腾讯授权，不修改微信程序本体、不接触微信网络协议；密钥抓取仅在
   登录瞬间于本机内存短暂读取（<2 秒）即释放。但无法保证使用方式符合《微信个人帐号使用规范》，
   由此产生的账号风险（如功能限制）由使用者自行承担。
3. 密钥缓存于 `~/.wxdump/`（权限 0600），**等同于你全部聊天记录的钥匙**，请妥善保管、
   不需要时删除；Web UI 仅绑定 127.0.0.1 本机回环。
4. 作者不对任何人使用本工具造成的任何直接或间接后果（包括数据丢失、账号异常、法律风险）承担责任。
5. 如你所在司法辖区禁止此类工具，请勿下载或使用。

## 二次开发

- 许可证：**MIT**（见 [LICENSE](LICENSE)，附使用范围附加条款），允许自由修改、再分发、二次开发、
  商用；**前提是保留版权与许可声明，并遵守上述使用范围**。
- 代码结构（全部标准库，无第三方依赖，便于阅读改造）：
  - `wxdump_core/elf_analyzer.py` — 挂点定位（锚字符串交叉引用算法，可扩展 PE/Mach-O）
  - `wxdump_core/key_capture.py` — gdb 断点抓取（两种寄存器布局）
  - `wxdump_core/key_derive.py` — PBKDF2 派生 + HMAC 验证（注意 pgno 为小端）
  - `wxdump_core/db_access.py` — 只读快照 + sqlcipher 明文导出
  - `wxdump_core/chat_store.py` / `msg_render.py` — 联系人/消息模型与渲染
  - `wxdump_core/exporters.py` — 六格式导出（DOCX 为 zipfile+OOXML 零依赖实现）
  - `wxdump_core/web_server.py` / `mcp_server.py` — 两个入口层
- 欢迎提交 Issue / PR（新格式、新消息类型渲染、新平台适配等）。**不接受任何以读取他人数据为目的的功能请求。**
- 致谢：密钥挂点定位思路参考了 [wxchat-export](https://github.com/lopleec/wxchat-export) 与
  [wcdb-key-tool](https://github.com/TANGandXUE/wcdb-key-tool) 的公开研究。
