# ADR-0004：Skill 分层发现与显式按需加载

- 状态：Accepted
- 日期：2026-08-05

## 决策

Skill 使用 `SKILL.md` 文档，frontmatter 只承载名称、描述、版本、工具提示、权限提示和标签。
Discovery 只解析 frontmatter 并计算内容 Hash，不执行正文中的代码或指令，也不把正文放入
候选索引。目录按 `builtin < user < project < workspace` 分层；高层同名条目遮蔽低层条目，
同层重复则拒绝发现。

正文只能通过显式请求加载，loader 接受 `name` 或精确的 `name@version`；索引使用后者，使调用方
可以直接复制且钉住版本。应用随包注入的 `BUILTIN` Skill 以包完整性作为隐式 Trust，配置文件
不得把外部目录声明成 `BUILTIN`，该作用域的 Trust 也不能通过 lockfile 覆盖或撤销。
其他作用域必须先通过作用域绑定的
`skill_name@version+scope+content_sha256` Trust 记录和独立的重新 Discovery/Hash 校验，随后再读取
正文并做一次 Hash 比对。Trust 默认关闭，Skill 变更后自动失效；原子 JSON lockfile 保存授权
记录。Skill 不得绕过当前 Run 的 Policy、Approval、Capability Lease 或 Sandbox。

## 原因

Skill 是知识和流程扩展，不是隐式权限授予。索引可用于选择候选 Skill，但只有用户或 Runtime
策略明确请求并通过对应作用域的信任策略后，正文才会进入 Context View，从而避免项目目录中的
文档无条件影响模型。
