# 保存规则的依赖与存储迁移

本文件是 CONTRACT.md 的本轮增量契约。原接口、格式、样例及全部原测试继续适用；下列差异仅在 storage_version=2 后生效。sdk1.py、sdk2.py 是已提供的完整离线依赖，保持其文件及原测试不变。

## 事故与范围

升级试运行把 CASE 两支预先计算：原本选择常量7的事实因另一支的多行查找报错；直接关联还扩大了重复事实的结果数。本轮迁移已有保存规则与任务绑定，不扩展规则语言、通用SQL、查询优化、并发迁移或性能目标。允许全量读取、全量转换和使用现成 transaction。

实现 migration.upgrade(db, *, before_commit=None)。db 为 warehouse.connect 返回的连接；维护窗口单工作者，没有并发业务写入。调用成功返回 None。版本1时，在一个现成事务内将所有规则转为SDK2 DAG，写入 sdk2.install 定义的 sdk2_plans、sdk2_tasks；原 rule_id 即 plan_id，任务ID和当前绑定不变，最后将 meta.storage_version 改为2。batches、publications和旧规则/任务表逐值保留，旧表仅作审计。版本2重复升级无副作用且返回 None；其他存储版本报 ValueError。before_commit 在全部迁移写入之后、提交之前调用，异常原样传播并完整回滚；失败后旧库可用，重开可重试。外层事务仍拥有最终提交。

## 切换后的公开接口

保留 Registry.save/get/bind、Runner.run、put_batch、read_publication 的签名与旧行为。Registry.save 仍接受 CONTRACT.md 的SDK1规则，但保存的是转换后的SDK2 DAG；get 返回独立可执行的SDK2 DAG，不再返回旧AST。已有 Registry/Runner 对象须在升级后立即使用目标表，后续替换规则和重绑任务立即生效。版本1接口仍按原行为工作，原回归不得删改。

目标计划输入源名固定为 facts、a、b，与原批次一致。SDK2输出关系恰有 position、value 两列：position 是原 facts 的零基位置，每次出现恰一行，value 是旧规则结果。关系输出次序可不同，Runner按 position 还原原序结果列表。不得把不同位置的相同事实合并。SDK2原生保存符合此出口的计划或替换同ID计划，既有Runner也须读到新结果。

保存的DAG必须由未修改的 sdk2.validate 接受，并能直接由 sdk2.execute 或 sdk2.run_saved 在后续不同批次独立运行。编译与升级不得读取 batches 来专门生成结果，也不得嵌入输入数据。运行路径不得调用 SDK1解释器、Python逐行表达式回调，或读取 rules_v1/tasks_v1；旧AST仅供升级输入和审计。可在 Registry.save 使用旧验证器验证输入。不得给SDK2增加CASE、惰性回调或旧语言解释节点；不要求更改其通用算子实现。

沿用旧语言全部求值语义，包括两层CASE、条件内的查找、可空布尔、精确有理数及空事实批次。NULL键不匹配；标量查找零行返回NULL，多行即使值相同或均为NULL也属cardinality，但只在表达式被求值时生效。CASE只计算条件和选中分支，False/NULL选择否分支。除法两操作数都被求值；NULL/0返回NULL，但NULL操作数不能掩盖另一操作数已触发的查找错误。只有被实际求值的除零或多行查找才能使整批失败；多个实际错误允许任选其中一种错误码。失败不发布部分结果，且保全此前同任务/批次的成功发布。

## 兼容样例

samples.build_legacy(db) 是完整旧库构造器，不是新版实现。原 daily/morning 结果为 [7,7,2,NULL,6]，重复的key=1事实分别占位置0、1；它们未选中的多行查找不能报错。把第一条的flag改为False，结果应为cardinality且无新发布。迁移相同规则后，将事实换为 key=2、amount=4、flag=False，a换为唯一 key=2/value=11，结果必须是 [11/4]。空事实且规则为1/0时结果为 []。原测试和 migration_checks.py 给出可重跑输入及手算/旧解释器对照。

## 交付与运行

交付迁移和接口适配实现，以及简短的表示说明，解释事实身份、标量基数和分支异常如何跨保存计划与任务执行保持一致。保留原正常能力、全部原断言及现有运行清单的结构。

python3.11 -B migration_checks.py legacy 仅核对初始旧库，供基线检查。
python3.11 -B migration_checks.py verify 执行完整原测试，再核对真实文件旧库升级、提交前失败与重开重试、重复升级、旧对象续用、换数据、规则替换及SDK2原生续写。
python3.11 -B migration_checks.py demo 演示真实升级、换批执行及选中坏分支的发布结果。
将 .pointwise-runtime.json 的 verify/demo 分别接到上述 verify/demo 入口；setup仍为空。脚本是公开的基础核验，不替代对契约全部语义的独立测试；可以补充测试，不能削弱原断言。基线的两个旧入口仍可运行。

核验必须经过实际旧库转换，不得只手建目标库。最终展示真实结果及失败后的发布状态，不能仅打印成功。需要文件库的检查在可写的干净副本执行。
