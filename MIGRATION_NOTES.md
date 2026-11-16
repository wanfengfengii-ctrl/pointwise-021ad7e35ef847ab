# 迁移适配与表示说明

本说明对应 `MIGRATION.md` 的交付要求，解释 SDK1 逐行规则如何在不修改
`sdk1.py`/`sdk2.py` 的前提下，保存为可由未修改 SDK2 在后续批次独立执行的
关系 DAG，并保持事实身份、标量基数与分支异常的旧语义。

## 模块划分

- `compiler.py`：纯编译器 `compile_plan`。输入已通过 `sdk1.validate` 的
  SDK1 AST，输出经 `sdk2.validate` 接受的 SDK2 计划。不读批次、不嵌数据、
  无逐行回调、无解释器。
- `migration.py`：`upgrade(db, *, before_commit=None)`。在单个
  `warehouse.transaction` 保存点内安装 SDK2 原生表、转换全部规则、迁移任务
  绑定并把 `meta.storage_version` 置 2。
- `warehouse.py`：`Registry`/`Runner` 按存储版本分流。版本 1 的代码路径
  （`rules_v1`/`tasks_v1` + `sdk1.evaluate`）原样保留；版本 2 只读写
  `sdk2_plans`/`sdk2_tasks`，运行路径不触碰旧表、不调用 SDK1 解释器
  （`Registry.save` 仅借用旧验证器校验入参）。

## 事实身份：每个出现一行，重复事实不合并

事实源 `facts` 的 `scan` 带 `ordinal=position`，为每个事实出现分配零基位置，
重复事实因此具有不同 `position`。每个被编译的子表达式都是一个恰好含
`position` 与一个值列的关系，每个存活位置恰一行；最终输出关系恰为
`(position, value)`。`Runner` 按 `position` 排序还原原序结果列表，所以输出
关系内部次序可以不同，而结果次序与逐行解释器一致。空事实批次没有任何位置，
表达式节点物化出空关系，`1/0` 这类规则结果为 `[]` 而不会报错。

## 标量查找：零行 NULL、一行取值、多行 cardinality

每个 `lookup` 出现独立编译为一条链：

1. 取事实键列与查找表做 LEFT JOIN，连接谓词是键的 `eq`。SDK2 的等值在任一
   操作数为 NULL 时得 NULL，故 NULL 事实键、NULL 查找键都不匹配；未匹配的
   左行保留，右侧列补 NULL——对应零行匹配返回 NULL。
2. 按 `position` GROUP：`count(*)` 统计匹配出现次数（含 value 为 NULL 的行，
   因此两行 NULL 仍是多行），`min(value)` 取唯一匹配值（零行时为 NULL）。
3. `count > 1` 的位置经 project 对齐列名后进入 `error/cardinality` 节点；
   `count ∈ {0,1}` 的位置走正常分支。该 error 节点本身是输出 UNION 的输入，
   因而在 DAG 中可达，SDK2 严格按节点序执行时，非空即抛 `cardinality`，
   为空则保留空关系不影响其余位置。

直接关联被 GROUP 收敛为每位置一行，重复查找行不会扩大结果数；重复事实因
`position` 不同也永不被合并。

## 分支异常：CASE 惰性与除法急切

CASE 编译为条件投影加两个 filter：`cond = TRUE` 为真的位置进入 yes 分支，
其余（False 与 NULL，用 `is_null(cond=TRUE) OR NOT(cond=TRUE)` 表达）进入
no 分支。每个分支先与事实扫描按位置 INNER JOIN 回连，仅对被选中位置物化
分支表达式；未选中分支里的多行查找、除零都不会被执行，因此不会报错。两层
CASE 自然嵌套，外层存活位置继续向内过滤。

`div`/`eq` 是 project 标量，两个操作数关系先各自完整求值再按位置 INNER
JOIN，符合“除法两操作数都被求值”。SDK2 的 `div` 复用 `common.divide` 的
SDK1 约定：任一操作数 NULL 得 NULL（含 NULL/0），非 NULL 零分母抛
`division`。因此 NULL 操作数掩盖不了另一操作数已触发的 cardinality——
因为该 lookup 链在 JOIN 之前已经执行并抛出。多个实际错误时由 SDK2 按节点
序先遇到的那个决定错误码，满足“任选允许错误码”。

## 存储切换与失败保全

- 迁移在一个保存点事务内完成；`before_commit` 在全部写入后、RELEASE 前
  调用，抛异常则整单回滚，存储版本仍为 1、旧库可继续使用，重开后重跑
  `upgrade` 幂等。版本 2 再调 `upgrade` 直接返回 None 且不触发回调；其他
  版本抛 ValueError。
- 原 `rule_id` 即 `plan_id`，任务 ID 与当前绑定不变；`batches`、
  `publications` 与旧 `rules_v1`/`tasks_v1` 逐值保留，旧表仅供审计。
- 版本 2 下 `Registry.save` 仍收 SDK1 规则但只存编译后的 DAG，`get` 返回
- 独立可执行的 SDK2 DAG；SDK2 原生 `save_plan` 替换同 ID 计划或 `bind_task`
  重绑后，已有的 Registry/Runner 对象立即读到新结果。
- `Runner` 在同一保存点内执行 DAG 并写 `publications`：求值错误整体回滚，
  不发布部分结果，同任务/批次此前的成功发布保持不变；成功重跑才替换发布。
