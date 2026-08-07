# ADR-0028：以 bash 取代 argv 执行，并补上只读搜索

状态：Accepted
日期：2026-08-07
关联：ADR-0020（Approval 挂起与 execution 授权轴）、ADR-0001（Host 沙箱）

## 背景

原 `shell` 工具接收 `argv` 数组，`HostBackend` 用 `create_subprocess_exec(*argv)` 直接执行 ——
**没有 shell 解析**。因此 `ls | grep foo`、`cd x && make`、`*.py`、`$HOME` 全部不工作，而名字
却叫 `shell`，等于邀请模型写它不支持的语法。`run_tests` 与它共用同一份实现，只是默认超时不同。

更实际的问题：查找代码只能走 `shell`，而 `shell` 声明 `process.exec`，于是**每一次搜索都要
人工审批**。这是审批疲劳的主要来源，而搜索本身是只读的。

## 决策

### 1. `bash` 取代 `shell` 和 `run_tests`

工具接收命令**字符串**，因为那正是模型会产出的东西。实现上仍显式 exec bash：

```python
await context.sandbox.run_command((bash_path, "-c", command), ...)
```

`SandboxBackend.run_command(argv)` 契约不变，任何地方都没有 `shell=True`，不存在二次解析。
bash 在构造时定位，找不到就失败（`SandboxViolation`），不静默降级。

`run_tests` 删除：它没有独立语义，项目的测试命令属于应用配置。

### 2. 明确安全性来自哪里

`bash` 保留 `required_capabilities=("process.exec",)`，因此：

- 默认模式**每次调用都需要显式审批**，人看到的就是将要执行的那行命令；
- `accept_edits` 不覆盖它（ADR-0020）；`plan` 模式直接拒绝；
- 沙箱仍执行 workspace 根、超时、输出上限和进程组清理。

`DefaultPolicyEngine` 的敏感路径规则**降级为启发式**并在代码中如此标注。实测：

| 命令 | 结果 |
|---|---|
| `cat ~/.ssh/id_rsa` | deny |
| `cat $HOME/.ssh/id_rsa` | deny |
| `cat ~/.s""sh/id_rsa` | **allow** |

它原本也只是字符串黑名单（AGENTS.md 早就写明「命令工具不能只靠字符串黑名单」）。保留它作为
纵深防御，但不再让任何人误以为它是边界。**边界是审批和沙箱。**

### 3. `glob` / `grep`：免审批的只读搜索

两者 `mutates=False`、`concurrency_safe=True`、无能力需求，因此 Policy 直接 ALLOW，
且 Runtime 会并行执行（ADR-0023 之后的并行规则）。

`SandboxBackend` 新增 `list_paths(pattern, limit)`：workspace 内的有界枚举。共享实现放在
`sandbox/walk.py`，但**授权仍属各后端**——每个后端用自己的 root 与 `resolve_path`。
枚举拒绝绝对路径与 `..`，并丢弃解析后落在 workspace 之外的符号链接。
`.git`、`node_modules` 等目录在遍历时跳过：这是遍历成本控制，不是安全边界，
显式指向其中的路径仍可读取。

`grep` 的正则有长度上限、文件数上限、单文件字节上限和匹配数上限。Python 的 `re` 没有超时，
因此病态正则仍可能耗尽工具超时——上限降低影响，不消除它。

## 后果

- 工具面变成三类且语义与名字一致：只读（`read_file`/`glob`/`grep`）、写入（`write_file`/
  `edit_file`）、执行（`bash`）；
- 日常「找代码」不再打断用户；
- 与模型的训练先验一致，不再产出无法执行的 argv；
- `cd` 不跨调用。常驻 shell 会话需要会话状态与 cwd 跟踪，是独立特性，本 ADR 不做，
  工具描述中明确告知模型用 `&&` 串联。
