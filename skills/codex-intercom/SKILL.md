---
name: codex-intercom
description: Claude Code 与 Codex session 之间的通信基础设施（对讲机），一个 Claude Code 会话可以同时管理任意多个 Codex session。向 Codex session（codex://threads/<id> 链接或 UUID）发消息：Codex 在工作时插进当前回合，停着时开新一轮；App 没打开这个 session 时自动改用 codex exec 在原目录运行。一个后台 listen 盯住你管理的所有 session，任何一个停下时通知一次，附带原因（完成、提问、等批准、模型或网络出错、中断、卡住、消息没开始）和最后回复；长任务还会定期唤醒你查看进展，取代手动设置的定时任务；等待期间 Claude Code 保持空闲。凡是要给 Codex 发消息、补充或纠正正在跑的 Codex 任务、等 Codex 做完或停下、定期查看 Codex 长任务进展、同时调度多个 Codex session、查 Codex session 状态，或拿到 codex://threads/ 链接时，都使用本 skill。
---

# codex-intercom：和 Codex session 对讲

本 skill 只提供通信能力：发消息、听停下、查状态。发什么、停下后怎么处理，由当前任务决定。

下文 `CI` 指 `python3 <skill 目录>/scripts/codex_intercom.py`，`<skill 目录>` 是加载本 skill 时给出的 Base directory（用安装脚本装的是 `~/.claude/skills/codex-intercom`，作为插件装的在 `~/.claude/plugins/` 下）。所有命令都用 `dangerouslyDisableSandbox: true` 运行：脚本要写 `~/.codex/intercom`，`codex queue` 和 `codex exec` 要联网。

第一次使用前先跑 `CI doctor`。它报告脚本未安装或 hooks 缺失时，运行 `CI install`，再请用户在 Codex 里信任新加的 hooks（见文末“安装与排障”）。

## 基本模型

- **你是管理者。** 管理者就是当前这个 Claude Code 会话，脚本从 `$CLAUDE_CODE_SESSION_ID` 自动识别，不用传参。
- **发过消息就归你管。** `send`（或 `claim`）之后，这个 Codex session 由你管理：`listen` 会盯着它；Codex 不能再用 App 弹窗提问（hook 会拒绝），而是把问题写在回复里并结束本轮，你会收到 `done`，问题在 `last_message` 里，用 `send` 回答。
- **别人管着的不能碰。** 另一个 Claude Code 会话正在管理的 session，`send` / `claim` 会被拒绝（退出码 2，`error: owned_by_another_manager`，`owner` 是对方）。确实需要接管时加 `--take-over`，对方的 `listen` 会收到 `lost`。对方超过 24 小时没有活动、也没在 listen，就不再算数，可以直接接手。
- **停下的消息不会丢。** 停下的记录在 Codex 侧的事件日志里，`listen` 每次运行时现场推算：你没在听时停下的，下次 `listen` 立刻报告。同一个 session 停了几次都没报告过时，只报最近一次，另带 `earlier_stops`（之前漏掉了几次）。

## 发消息（前台运行）

```bash
CI send --thread <uuid 或 codex://threads/uuid> --message "消息内容" [--label 短名]
```

- 长消息或多行消息：先用 Write 写进工作区文件，再执行 `CI send --thread <id> < msg.txt`（省略 `--message` 时从 stdin 读）。
- `--label`：给这个 session 起个短名（如 `auth-refactor`），之后 `listen` 和 `list` 的输出里都会带上。同时管理多个 session 时建议加。
- `--checkin 10m`：这个 session 一直在工作时，每隔多久唤醒你看一次进展（`90s`、`10m`、`1h`；`off` 关闭）。不加就用 `listen` 的默认值（30 分钟）。按任务的预计时长和需要盯得多紧来定。
- Codex 会把消息当作用户的话。只有需要标注来源时才加 `--from NAME`。
- `--queue`：不插话，总是开新一轮。`--via app|exec`：指定开新一轮的通道，默认 `auto`。

输出是一行 JSON：

| 字段 | 含义 |
|---|---|
| `mode: steer` | Codex 正在工作，消息会在它下一次工具调用后注入；如果回合正要结束，就作为续写提示 |
| `mode: queue`，`transport: app` | Codex 停着，App 已取走消息，开始新一轮 |
| `transport: exec` | App 没打开这个 session，已用 `codex exec resume` 在 session 原目录后台运行（输出在 `exec_log`） |
| `transport: queued` | 没有人在处理，消息留在 Codex 队列里，App 打开这个 session 时才会处理 |
| `transport: blocked` | App 里有接管之前就弹出的提问弹窗还没处理（`blocked_by_question`）。消息已经排队，要等用户在 App 里回答、跳过或取消弹窗后才会开始 |
| `claimed` / `taken_over_from` / `previous_owner` | 这次发送让你开始管理它 / 从谁手里接管 / 接手了谁已失效的管理 |
| `listening` | 你是否已有 `listen` 在后台运行。为 `false` 时启动一个（见下节）；为 `true` 时什么都不用做，新 session 会自动被它盯上 |
| `ok: false` | 发送失败，原因见 `codex_queue` 或 `error` |

## 听 Codex 停下（后台运行）

```bash
CI listen
```

- 用 `run_in_background: true` 运行，然后结束本轮。等待期间会话是空闲的，可以正常对话、做别的事。任何一个你管理的 session 需要处理时，后台任务结束，你收到一次通知，读它的输出（一行 JSON）。
- 一个管理者只需要一个 `listen`，它覆盖你管理的全部 session，包括它运行期间新 `send` 的。重复启动会立即返回 `already_listening`，忽略即可。
- 几个 session 差不多同时停下（2 秒内）时，会合并成一次通知。
- **定期查看进展（取代定时任务）**：session 一直在工作时，`listen` 每隔一段时间（默认 30 分钟，见 `send --checkin`）也会结束一次，`reason: checkin`，`progress` 里是进展快照。不需要再给 Claude Code 设定时任务。被唤醒时，所有已过半个间隔的工作中 session 会一起带上，免得被逐个叫醒。`listen --checkin 20m` 改默认间隔，`off` 关闭。
- 处理完通知后：`watching` 或 `progress` 不为空（还有 session 在跑），或者你刚发了新消息，就再启动一次 `listen`。
- 用户在 App 里自己对被管理的 session 发起的回合，停下时也会报告。
- 不要用 Monitor 或定时轮询来盯 Codex。

输出：

| 字段 | 含义 |
|---|---|
| `reason` | `attention`：有 session 停下或失去管理；`checkin`：只是到了查看进展的时候；`nothing_to_watch`：你没在管理任何 session；`already_listening`；`timeout`（`--timeout N` 到期）；`orphaned`（启动它的会话已经不在） |
| `stops` | 需要处理的 session 列表，每项含 `thread`、`label`、`reason`、`summary`、`at`，以及下表的附加字段 |
| `progress` | 仍在工作、到了查看时间的 session 的进展快照（见下文） |
| `lost` | 被别的管理者接管（`taken_over`，`by` 是谁）或被强制交还（`released`）的 session，你不再管理它们 |
| `watching` | 还在跑的 session（`state: working`，或 `waiting_start`：发出的消息还没开始） |

`stops` 里每项的 `reason`：

| reason | 含义 |
|---|---|
| `done` | 本轮结束，`last_message` 是 Codex 的最后回复 |
| `asking` | 本轮结束时 App 里还开着提问弹窗（`questions`），只能由用户在 App 里回答、跳过或取消，弹窗开着时新消息会一直排队。被管理的 session 不会再弹出新的弹窗，只会出现在开始管理之前就已弹出、或 hook 失效的情况 |
| `approval` | Codex 在等批准（`tool`、`detail`），已经等了 30 秒以上，只能在 App 里处理 |
| `error` | 模型请求失败后停下（`error`：容量不足、限流、网络等；`failures` 是连续失败次数）。网络错误时 Codex 还会自己重连；同一段连续失败只报一次 |
| `interrupted` | 本轮被中断（用户在 App 里点了停止） |
| `ended` | exec 通道的 `codex exec` 进程退出了，但本轮没有正常结束（`exec_log`） |
| `stalled` | 太久没有任何事件（工具运行中 30 分钟、模型请求中 15 分钟、其他情况 5 分钟），可能是 App 崩溃、卡住，或者关掉了这个 session |
| `not_started` | 发出的消息 5 分钟内没有开始处理（`queued` 是这些消息，`codex_queue` 是 Codex 队列里的条数，`session_state` 是 session 卡在哪，`blocked_by_question` 是挡路的弹窗），或者 exec 进程没开始就退出了（`exec_exit`） |

附加字段：

- `earlier_stops`：这次报告之前还停过几次、都没报告过（只报最近一次）。
- `undelivered`：插话还没送达就停了。这些消息会随下一条发给 Codex 的消息自动带上，不要重发，否则 Codex 会收到两遍。
- 消息发出后、它的回合开始之前，`listen` 不会把这个 session 之前的停下当作结果报告；要么等到这条消息自己那一轮停下，要么超时报 `not_started`。

`progress` 里每项（进展快照）：

| 字段 | 含义 |
|---|---|
| `running_s` | 本轮已经运行的秒数 |
| `in_flight`、`silence_s`、`last_event` | 现在在做什么（`model` / `tool` / `between steps`）、多久没有新事件、最后一个事件 |
| `tool_calls`、`model_requests`、`failed_requests` | 本轮的工具调用次数、模型请求次数、失败的模型请求次数 |
| `activity` | 从 Codex 自己的会话记录读出的本轮动态：`messages` 是 Codex 最近几条进度说明，`recent_commands` / `commands_run` 是最近执行的命令和总数，`files_changed` 是改过的文件，`compactions` 是上下文压缩次数，`partial: true` 表示本轮太长只读了后半段。会话记录格式不归本 skill 控制，读不到时没有这一项 |

看完进展：方向对，直接再启动 `listen`；跑偏了，用 `send` 插话纠正（会在 Codex 下一次工具调用后注入）；需要细节，用 `status` 或读 Codex 的会话记录。

## 管理与交还

- `CI claim --thread <id> [--label 短名] [--take-over]`：不发消息也开始管理（比如想盯住用户自己在 App 里跑的任务，同时禁止弹窗提问）。
- `CI release --thread <id>`：交还给用户，`listen` 不再盯它，弹窗提问恢复。任务做完、不再需要管理时就交还。只有管理者本人能交还；`--force` 强制交还别人管理的 session。
- 忘了交还也没关系：管理者 24 小时没有活动、也没有 `listen` 在跑，管理自动失效。

## 查状态

- `CI list`：你管理的所有 session，每项有 `state`（`working` / `waiting_start` / `stopped` + `reason` / `unknown`）、`unreported`（下次 `listen` 会不会报它）、`last_message`。`--all` 列出所有被管理的 session 和各自的管理者。
- `CI status --thread <id>`：单个 session 的细节：管理者（`owner`）、当前判断（`codex`）、工作中时的进展快照（`progress`）、待报告的停下（`unreported`）、还没开始的消息（`pending_sends`）、查看间隔（`checkin_s`、`last_checkin`）、未处理的提问弹窗、exec 进程、Codex 队列条数、未送达和最近送达的插话。

## 安装与排障

- `CI doctor [--thread <id>]`：检查脚本是否已安装且是最新版、hooks 是否注册齐全、各类事件最近有没有收到、识别出的管理者 id。
- 安装或更新：`CI install`。它会把脚本复制到 `~/.codex/hooks/`，并修改 `~/.codex/hooks.json`（改之前先备份）。输出里 `hooks_changed: true` 时，要请用户在 Codex 里信任 codex_intercom 的 hooks：Codex App 在 Settings > Hooks；Codex CLI 启动时会提示 Hooks need review，也可以用 `/hooks`。只更新脚本时不需要重新信任。
- 卸载：`CI uninstall [--remove-script]`。清理：`CI gc`（删除很久没动、无人管理的 session 数据）。
- hooks 没生效的表现：Codex 明显在工作，`send` 却总是开新一轮；或者消息发出后总是 `not_started`。先跑 `doctor`。
- 旧命令 `wait` 已由 `listen` 取代，`manage` 已由 `claim` 取代，调用它们只会返回替代说明。
- 无人值守时，如果命令被 Claude Code 的权限检查拦下，整条链会停住。可以给 `CI` 加放行规则。
- 调试：`touch ~/.codex/intercom/DEBUG` 会把原始 hook 输入记到 `debug.log`。日志里有提示词内容，排障完删掉 DEBUG 和 `debug.log`。脚本错误记在 `errors.log`。
- 不要通过 Codex App 的私有 IPC（`~/.codex/ipc/`）操作 session：那不是公开接口，随版本变化，出问题也无从排查。
