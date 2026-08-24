# cc-retry-watchdog

Claude Code 的响应流被中途掐断时，会直接停死：

```
● API Error: Connection lost mid-response. The response above may be incomplete.
```

同一件事有十一种措辞，见[什么算掉线](#什么算掉线)。

没有倒计时，没有重试——这一轮被定稿，会话就停在那儿等人敲字。长时间无人值守的任务，
这就是"回来看到活干完了"和"回来发现 40 分钟前就死了"的区别。

这个守护就是那个"人"。它发现掉线后，把你的重试提示敲进**那一个**终端，别的什么都不做。

[English](README.md) · [中文]

---

## 为什么内置重试救不了它

以下结论来自对已安装二进制（v2.1.226）的字符串核对和本地转录统计，不是猜的：

**流中断确实有重试，但前提是"到目前为止什么都还没吐出来"。** 掉线后 Claude Code 会检查
是否已经产出过非 thinking 的块。只吐过 thinking 就重试；一旦有任何 text 或 `tool_use`
块流出去，就转而走 finalize 路径，合成一个停止原因，写下你看到的那条报错。
次数是硬编码的（2 次陈旧连接 + 1 次空闲超时），没有环境变量能改。

结果和期望正好相反：**回合越长、越有价值，就越必然不会被重试。**

**钩子也救不了。** 因 API 错误结束的回合触发的是 `StopFailure` 而不是 `Stop`，
而且是 fire-and-forget——输出和退出码都被忽略。所以在 `Stop` 上返回
`{"decision": "block"}` 让它自动续跑的那套招数，在这里完全无效。

**那些超时变量是真的，但没用。** `CLAUDE_ENABLE_BYTE_WATCHDOG`、
`CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS`、`CLAUDE_ENABLE_STREAM_WATCHDOG`、
`CLAUDE_STREAM_IDLE_TIMEOUT_MS`、`keepPartialMessageOnAbort` 在二进制里都存在，
但它们管的是**检测**，没有能力重新发出一个回合。

**`CLAUDE_CODE_AUTO_RESUME_ON_DROP` 根本不存在。** 它是
[anthropics/claude-code#69415](https://github.com/anthropics/claude-code/issues/69415)
里的*提案*，不在二进制里，export 了也是空转。自查：
`strings -a "$(readlink -f "$(command -v claude)")" | grep AUTO_RESUME`

关于成因的一个本地数据点：160 次掉线的耗时中位数 22 秒、最大 203 秒，
**没有在 180s / 300s 这两个 watchdog 阈值上聚集**——说明是网络路径掐的，不是 watchdog 主动中止。
调那些超时没有意义。

---

## 什么算掉线

触发条件是一份**精确措辞白名单**，来自 `claude` 二进制里编译进去的字符串，
而不是"某次在谁的屏幕上见过什么"。Claude Code 在 **2.1.226** 改写了这组文案：
`closed` 改成了 `lost`，两条笼统的消息按成因拆成了四条。两代措辞都收录，
所以升级或回滚都不影响：

| ≤ 2.1.225 | ≥ 2.1.227 |
|---|---|
| `Response stalled mid-stream` | `The response stopped arriving` |
| `Connection closed mid-response` | `Connection lost mid-response` |
| `Server error mid-response` | `Server error mid-response` |
| — | `Your computer went to sleep mid-response` |

这几条对应"已经吐出过内容"的那一轮，所以结尾是 *"The response above may be
incomplete."*。同样的故障发生在"还没产出任何内容"时，结尾是 *"Try again."*，
也一并收录——旧版是 `Response stalled while thinking` / `Connection closed
while thinking`，新版是 `The response stalled` / `Connection lost` /
`Your computer went to sleep before a response was produced`——外加
`Connection to the API was lost (<code>)`，那条是在流外面抛出来的。

**故意不匹配**的（重试只会白烧一轮）：`Request was aborted`（你自己按了 esc）、
`401 Invalid API key`、tool-use 并发和重复 `tool_use` ID 这两个 `400`、
`The model has reached its context window limit`，以及兜底的
`Please wait a moment and try again`。判定从不只看 `API Error:` 这个前缀。

升级后想重新导出这份清单，见 `tests/test_messages.py` 的文件头。

---

## 安装

```bash
git clone https://github.com/S313S/cc-retry-watchdog.git
cd cc-retry-watchdog
./install.sh --hook     # 不加 --hook 就只打印配置片段，不动 settings.json
```

需要 Python 3.6+（只用标准库）。`--hook` 会先备份 `~/.claude/settings.json`，可重复执行。

```bash
ccwatch check     # 看它现在怎么判断你的各个终端，绝不动手
ccwatch start     # 启动守护
ccwatch hook      # 钩子注册了吗？有没有待处理工单
```

**已经在运行的** Claude Code 会话要重启才会加载新装的钩子，在那之前它们走轮询兜底。

---

## 支持的终端

| 平台 | 后端 | 状态 |
|---|---|---|
| macOS — Terminal.app | AppleScript | 已实测 |
| macOS — iTerm2 | AppleScript | 已实测 |
| 任意平台 — tmux | `capture-pane` / `send-keys` | 已实测 |
| Linux / WSL / Windows 且无 tmux | — | 不支持 |
| VS Code 内置终端 | — | 不支持 |

非 macOS 只能走 tmux：总得有办法读到终端屏幕并往里敲字，tmux 是唯一可移植的那个。

**macOS 上必须从真实终端窗口里启动。** 驱动 Terminal/iTerm 需要 AppleScript 自动化权限，
这个权限跟着「负责进程」走；launchd 拉起的进程没有这个身份，AppleEvent 会一直卡死——
所以这里**故意没有**提供 LaunchAgent。纯 tmux 环境没这个问题。

### 让它自动启动

既然必须从终端窗口来，那就让你开的第一个终端窗口来做。加进 `~/.zshrc`（或 `~/.bashrc`）：

```bash
[[ -o interactive ]] && command -v ccwatch >/dev/null 2>&1 && ccwatch autostart
```

`autostart` 是静默的，守护已在跑时零开销直接返回，并且**拒绝从没有控制终端的 shell 启动**
——沙箱里的工具 shell、CI 步骤、钩子进程都会被挡住。这条守卫很关键：
没有终端父进程的守护会在每次 AppleEvent 上卡到超时，同时还占着健康实例需要的 pidfile。

守护能扛住"关掉启动它的那个窗口"，但扛不住注销和重启——上面这行正是补这个缺口。

---

## 它怎么判断

两条独立路径。

**① StopFailure 钩子——准确，约 1 秒。** 钩子不能让回合自己续跑，但能留一张写明
"哪个 tty 刚死了"的工单，守护 1 秒内响应。全程不看屏幕，掉线是确知的事实。

**② 屏幕轮询——兜底，约 5 秒。** 读每个终端显示的内容，识别"停在报错上"的版面。
覆盖装钩子之前就已启动的会话，以及守护当时没运行的情况。

无论哪条路，必须**同时**满足才动手：

- 报错是这一轮最后发生的事（轮询），或有工单确认（钩子）；
- 会话空闲——没有 spinner、没有 `esc to interrupt`，尤其没有内置的
  `Retrying in Ns · attempt n/m`，绝不打断它自我修复；
- 输入框是空的，你打了一半的字不会被冲掉；
- 那里确实有 claude 进程在跑；
- 过了冷却期（30 秒）且该会话连续重试没超上限（6 次）；
- 以及——在真正下键的那一瞬间，上面这些依然成立。敲字之前会**单独重读那一个 tab**，
  因为会话可能就在这个空档里醒过来（后台 agent 回来的一条 task notification 就够了）。
  往刚醒的会话里打字，文字会**没提交**地卡在输入框里，而非空输入框正是本工具拒绝
  触碰的状态——一次没掐准的注入，就能让这个会话此后再也得不到救援。

明确忽略的情况：任务正常做完在等你输入、已经恢复并继续输出、正在等权限确认、
子 agent 报错但主循环还在跑、以及只是对话正文里提到了这段报错文字。

### 当一次注入卡住的时候

上面那条防线自己也有失效模式。`do script` 会把文字和回车一次性写给 Terminal，
而 TUI 可能把这当成一次粘贴：文字进了输入框，但什么都没提交。输入框从此永远非空——
而非空输入框正是本工具拒绝触碰的状态，于是**一次没掐准的注入，就让这个会话此后
再也得不到救援**。这不是假想：它让一个真实会话在掉线状态下躺了 9 小时 27 分，
而日志每一秒都在如实地写「input box not empty」。

所以：如果输入框里**正好等于**重试语，而这个会话本来就该被救，那就认定它是我们自己
卡住的注入，用 Ctrl-U 清掉；待处理的 ticket 故意保留，下一轮扫描正常注入。判定用
相等而不是包含——用户自己打的、只是以重试语开头或包含重试语的文字，永远不动。
至于「再补一个回车」这个显而易见的替代方案：实测在这个状态下提交不了，是先拿真实
会话验过 Ctrl-U 才落地的。

但**后台 agent 比死掉的那一轮活得更久**不算「已经恢复」。主循环早就停住了，它的
面板还在报错下面继续画，这曾被误判成「这一轮往下走了」，导致轮询兜底整夜失明——
真实发生过一次掉线 11 小时无人重试。现在这类装饰会被识别；主循环自己写出来的东西
出现在报错之后，依然会让这个会话出局。

两套测试把这些全钉死了。改任何一条规则后都跑一遍：

```bash
python3 tests/test_analyze.py    # 45 个手工复刻的终端版面
python3 tests/test_messages.py   # 25 条真实 CLI 文案，该触发 / 绝不能触发
```

两套里都有一半以上是"绝不能触发"——出 bug 的代价在这一边：
误判会往你正在用的会话里敲字。

---

## 一个需要理解的副作用

往一个已经死掉的回合里敲任何字，都是**开一轮新的**。默认文本是 `please, continue`——
这样措辞是有意的，`please, retry` 读起来像"从头再来"；又因为半截响应还留在转录里，
模型通常会接着断掉的地方往下写。但它终究是新的一轮：如果掉线发生在 `Edit` 或 `Bash`
调用之后，模型可能重复那次副作用。这是"靠重新提示来救回"这件事本身的性质，
不是本工具引入的；自动化只是让它更常发生。
介意的话把 `max_consecutive` 调小，或者先用 `dry_run` 跑一段时间。

---

## 配置

`~/.claude/cc-autoresume/config.json`，热读，改完立即生效。

| 键 | 默认 | 说明 |
|---|---|---|
| `retry_text` | `please, continue` | 敲进去的文本 |
| `poll_interval_sec` | `5` | 轮询间隔 |
| `confirm_polls` | `2` | 连续观测到几次才动手（仅轮询路径） |
| `cooldown_sec` | `30` | 同一会话两次注入的最小间隔 |
| `max_consecutive` | `6` | 单会话连续重试上限，超了就停手等人 |
| `dry_run` | `false` | 只记录不注入 |
| `notify` | `true` | 注入时弹桌面通知（macOS） |
| `use_hook_triggers` | `true` | 是否采信钩子工单 |
| `trigger_ttl_sec` | `180` | 工单多久算过期 |
| `fast_poll_sec` | `1` | 有工单待处理时的间隔 |
| `exclude_title_regex` | `""` | 标题命中就跳过 |
| `exclude_tty` | `[]` | 如 `["/dev/ttys003"]` |
| `watch_terminal_app` / `watch_iterm` / `watch_tmux` | `true` | 分后端开关 |
| `tail_lines` | `80` | 只看屏幕末尾多少行 |

状态、日志、工单都在 `~/.claude/cc-autoresume/`（可用 `CC_AUTORESUME_HOME` 覆盖），
故意放在代码目录之外，这样 `git pull` 不会和它们打架。

---

## 实现笔记（踩过的坑）

- **ticket 绝不能一声不吭地过期。** 被拦下的 ticket 现在每次原因变化都会记一行
  （`ticket held | <tty> | <原因>`），过期那行也会带上最后一次判定。在此之前，
  漏掉一次掉线在日志里只留下 `expired`，根本分不清当时是判成忙、没认出版面、
  还是压根没枚举到那个会话。
- **Terminal.app 的脚本字典有两个静默失败点。** `repeat with w in windows` 取不到东西，
  必须用 `window wi` 下标；`set tb to tab ti of window wi` 之后取 `contents of tb`
  返回空，必须每次写完整限定符。AppleScript 的 `try` 会把这两个都吞掉，
  所以代码里专门加了「应用在跑却读到 0 个会话」的告警。
- **tty 不能当会话唯一键。** 进程已退出的 Terminal 窗口仍报告它原来的 tty，
  而这个号会被新窗口复用；两条记录撞键、互相覆盖计数，防抖就永远攒不满。
  改用「窗口 id : 标签序号」。
- **钩子进程自己没有控制终端**（`ps` 显示 `??`），tty 要沿父进程链往上找。
- **钩子 payload 的 `error` 只是泛化分类**——断流和 502 都叫 `"server_error"`，
  真正的报错原文在 `last_assistant_message`。
- **文本和回车要分两次发。** TUI 如果在一次读取里同时拿到文本和换行，
  可能当成粘贴而插入一个换行，而不是提交。
- **不能用 AppleScript 模拟按键**，除非用户给了 osascript 辅助功能权限，
  否则 `System Events` 会报"osascript 不允许发送按键"。所以全部走各终端自己的脚本接口。

---

## 来源

起于 [anthropics/claude-code#69415](https://github.com/anthropics/claude-code/issues/69415)，
`StopFailure` 的行为和那些未公开的 watchdog 环境变量最早是在那里从二进制里挖出来的。
本仓库是那个 issue 里提议的"自动恢复层"的外部替代品，直到官方实现落地为止。

MIT 许可。
