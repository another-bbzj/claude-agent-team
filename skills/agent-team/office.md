# `.team/` 办公目录（成员之间唯一的交流渠道）

桌面版子代理没有私信工具，所以团队像真实办公室一样靠**文件**交流：工单板、每人一个收件箱、交接单。看板会把「写别人的收件箱」画成私信，「读别人写的文件」画成交接。

```
<project>/.team/
  SPEC.md                 # 契约：目录、模块导出签名、数据模型、DOM/class 约定、测试命令
  tickets/NN-slug.md      # 工单板（一张一文件，编号按依赖顺序）
  inbox/<subagent_type>.md# 收件箱：谁都可以追加留言，收件人开工时先读
  handoffs/NN-slug.md     # 交接单：工单完成时由负责人写
  decisions.md            # 队长记录的裁决（口径、命名、取舍），全员只读
```

## 工单模板 `tickets/NN-slug.md`

```
# NN: <标题（中文，也用作派工 description）>
owner: <subagent_type>          status: todo | doing | done | blocked
blocked_by: [NN, NN] 或 none
files: <本工单独占的文件清单——不与其他工单重叠>
deliver: <从使用者角度描述的端到端行为，一句话>
accept:
- [ ] <可验证标准 1，如 node --test 全绿 / 375px 不横向滚动 / 控制周期抖动 < 0.2 ms>
- [ ] <标准 2>
```
一张工单 = 一条**竖切片**（穿过所需的每一层，单独可演示），大小能装进一个新上下文窗口。大范围机械改动（重命名、改共享类型）用「扩张—迁移—收缩」拆成多张。

## 收件箱 `inbox/<subagent_type>.md`

追加式，每条一段：
```
## <时间> 来自 <写信人 subagent_type>
<一段话：需要对方知道/做的事；涉及接口就给签名，涉及文件就给路径:行号>
```
收件人开工第一步读自己的收件箱，处理后在交接单里回应；不需要回信。

## 交接单 `handoffs/NN-slug.md`（≤ 25 行）

```
# NN: <标题>  by <subagent_type>
changed: <文件清单>
exports: <新增/变更的接口签名>
deviations: <与 SPEC 的偏差 + 原因>（无则写 none）
verified: <跑过的命令与结果>
to-inbox: <已写进谁的收件箱>（无则写 none）
risks: <遗留风险，按严重度排序>
```

## 派工 prompt 模板（只放指针，不贴内容）

```
你是 <部门>·<角色>。项目：<path>。
先读 .team/SPEC.md、.team/tickets/NN-slug.md、.team/inbox/<你的 subagent_type>.md。
只改工单 files 列出的文件。上游：<成员名> 已交付 .team/handoffs/MM-xxx.md（需要就读它）。
完成后：写 .team/handoffs/NN-slug.md；把需要同事知道的事追加到 .team/inbox/<对方>.md；把工单 status 改为 done。
汇报 ≤ 200 字：交接单路径 + 三行要点（产出 / 偏差 / 风险）。
```

## 队长的日常

- 收到汇报：只读交接单里的 `deviations`、`risks`、`to-inbox`；需要裁决的写进 `decisions.md`。
- 转达：成员汇报里"需要转达给 X"的内容，队长追加进 `inbox/X.md`，或在派 X 时在 prompt 里点名引用（如"阿服 已交付 store.js，导出 createStore(storage)"）。
- 解阻塞：某工单 done 后，所有 `blocked_by` 只剩它的工单立即派出。
