# 五彩高亮备份到 Snapnote

运行一次，将五彩最近 7 天尚未备份的每条高亮保存为一篇 Snapnote 笔记。确认保存后记录 `h_id`，自动提交并推送到 GitHub 私有仓库。

## 运行

需要 macOS / Linux、Python 3.11+、Git，以及可用的 GitHub Git 认证。只用 Python 标准库，无需安装 Python 包。

本机需要已有五彩 Skill：`~/.agents/skills/wucai/scripts/wucai_api.py`。脚本复用其请求入口，固定对接中国区 `cn`。

环境变量：

| 名称 | 要求 |
| --- | --- |
| `WUCAI_TOKEN` | 必填；五彩 `wct-` Token，是唯一五彩凭据来源 |
| `SNAPNOTE_MCP_TOKEN` | 必填；现有 Snapnote Bearer Token |
| `SNAPNOTE_MCP_URL` | 可选；未设置时读取 `~/.codex/config.toml` 中的 Snapnote URL |

你已在 `.zshrc` 导出 `WUCAI_TOKEN`，请从配置已生效的终端运行。脚本只读取进程环境，不主动加载 shell 配置，不读取中央 YAML 凭据。

```zsh
cd /Users/xinwu/project/wucai-to-snapnote
python3 backup.py --dry-run
python3 backup.py
```

`--dry-run` 只读检查两端连接、完整拉取最近 7 天高亮，并报告本地尚未记录的数量；不创建笔记、不创建运行目录、不写 JSON、不暂存、不提交、不推送。`unrecorded_count` 是本地未记账数，不能当作最终创建数，因为执行阶段还会查找远端来源标识。

真实运行会在开始备份前补推历史未同步记录，结束后提交并推送本次成功记录。没有新增记录时不会创建空提交。所有路径相对于脚本目录，不受启动目录影响。

## 定时运行

TaskTick 使用 [run-scheduled.zsh](run-scheduled.zsh) 作为入口，在上海时区每天每小时的 `00、15、30、45` 分运行，cron 为 `0,15,30,45 * * * *`。该入口加载现有登录 shell 环境，使用已验证的 Python 3.11，凭据仍从环境读取。

可以只读检查同一入口：

```zsh
/bin/zsh /Users/xinwu/project/wucai-to-snapnote/run-scheduled.zsh --dry-run
```

任务默认不补执行、不在应用启动时执行；实际备份仍由原脚本的单机锁和来源去重保护。

## 保存内容

正文只保存高亮原文和原生图片。图片通过 Snapnote 的 `resource_import` 下载到其媒体存储，再使用官方返回的 `delta_ops` 嵌入正文；图片笔记可以直接显示图片，不输出外部图片链接。批注保存到 Snapnote 原生批注，逗号分隔的五彩标签映射到原生标签，新建笔记传入五彩原始创建时间。兼容接口实际返回的 `annonation` 及文档中的 `annotation`。

正文通过完整正文接口和原始 Quill Delta 读写，保留长正文、空格和换行。只新增一个文本属性“来源”，值为 `[wucai:cn:h_id:…]`，用于去重和恢复；正文不添加标题、字段标签、空白批注或来源标记。首次真实备份已生成的笔记按用户选择保留原生备份时间。

## 每日记录

成功记录保存在 [records](records/README.md)，文件名按 UTC+8 **实际确认备份成功的日期**生成：

```text
records/
  YYYY-MM-DD.json
```

JSON 是 `h_id` 字符串数组。使用字符串保留 64 位整数精度，示意：

```json
["123", "124"]
```

运行时读取全部历史日文件去重，不删除旧记录。相同 `h_id` 后续文字或批注变化也不会重新创建笔记。这里只备份当前 `7d` 查询窗口内的新增 ID，不追补更早且从未进入窗口的高亮。

每条笔记必须取得有效 `note_uuid` 并读回全文核对后才写成功记录；图片还必须读回相同的 `app://images/...` 节点。日文件采用临时文件、`fsync` 和原子替换。仓库仅保存 ID 记录和代码，笔记正文、图片文件、Token 及待确认状态不进入 Git。

## 中断和恢复

创建前将当前 ID 和正文摘要写入本地 `.runtime/pending.json`。有图片时先记待导入状态，导入成功后立即补记图片的本地资源地址，再创建笔记。收到 UUID 后立即补记 UUID；正文和图片均确认成功后写日记录并清除待确认项。

超时或中断后，重新运行即可先处理待确认项。已知 UUID 时读回正文并核对来源属性；没有 UUID 时通过原生属性筛选查找，搜索包含已归档笔记。兼容搜索旧版正文中的来源标识。恢复不依赖高亮仍在最近 7 天内。

如果找不到、匹配多篇、正文或图片不一致、图片导入结果不明或记录损坏，程序返回非零并保留现场，不自动重建或重复导入。请检查 `.runtime/pending.json` 中的 ID/UUID，在 Snapnote 中人工核对。仅当你确认请求没有创建笔记时，才在另存待确认文件后手动清除该项，再重新运行。不要通过删除整个 `records/` 强制重跑。

单机文件锁阻止同一仓库重复启动。本版不支持多台机器同时对同一来源执行备份。

## Git 同步

目标固定为私有仓库 `v-yutianran/wucai-to-snapnote`，分支 `main`；接受同一仓库的 GitHub HTTPS/SSH 地址，包括本机 `insteadOf` 的协议重写。运行中只精确提交 `records/YYYY-MM-DD.json`。

存在其他文件改动、未推送的代码提交、记录删除、合并提交或远端分支不再是本地祖先时，程序停止，交由你处理。程序不会自动拉取、合并、rebase 或强推。需要更新代码时先手工同步，使工作区干净。

推送失败保留本地 JSON 和已生成的提交；下次先补同步，成功后再备份。处理日文件时请保留既有成功 ID，避免人为造成重新备份。

## 验证

```zsh
python3 -m unittest discover -s tests -v
python3 -m py_compile backup.py tests/test_backup.py
git diff --check
```

测试只使用合成数据、临时目录、本机 HTTP 服务及临时 Git bare 仓库，不连接真实笔记服务、不修改真实笔记。

终端输出 JSON 事件：`backup.preview`、`backup.started`、`backup.completed`、`backup.git_sync_failed`、`backup.failed`、`backup.cancelled`；成功退出码为 `0`，执行失败为 `1`，命令参数错误为 `2`。日志不包含凭据或笔记正文。

[开发验证记录](docs/changelog/README.md) · [五彩官方 Skill](https://github.com/makediff/wucai-openclaw)
