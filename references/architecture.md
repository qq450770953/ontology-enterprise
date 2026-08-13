# 架构参考

六大能力设计，对应企业级 Ontology Runtime 参考架构（Object/Link/State/Method/Action/Policy-Audit）。

## 总体设计

```text
上层 Agent / 应用
  ↓ 统一对象与动作（不面对底层 API）
Ontology Enterprise Runtime
  ├─ Object   类型化实体 + 别名 + 版本 + 生效时间
  ├─ Link     业务关系 + 基数 + 环检测
  ├─ State    类型状态机 + 合法流转校验
  ├─ Method   确定性只读业务逻辑（白名单沙箱）
  ├─ Action   受治理动作（前置条件/角色/幂等/副作用）
  └─ Policy / Audit / Lineage   RBAC + 审计 + 血缘
  ↓
SQLite（memory/ontology/ontology.db）— 事务 + 并发安全
```

## 为什么从 JSONL 升级到 SQLite

| 维度 | 原 ontology (JSONL) | ontology-enterprise (SQLite) |
|---|---|---|
| 事务 | 无 | 每命令 commit，失败 rollback |
| 并发 | 追加写冲突 | WAL + 事务隔离 |
| 查询 | 全量扫描 | SQL 索引过滤 |
| 治理 | 无 | 策略/审计/血缘表 |

## 治理执行链

每次变更操作的统一路径：

```text
请求 (actor, role)
  → require_policy(role, resource, action)   RBAC 预检
  → 业务校验（类型约束 / 状态机 / 前置条件）
  → 执行（含受控副作用）
  → audit_log 落库（不可绕过）
  → commit / rollback
```

## Method 沙箱

- 注册时：正则黑名单（`__dunder__`/import/os/sys/subprocess/eval/exec/compile）+ `ast.parse` 语法检查。
- 执行时：`eval(code, {"__builtins__": {白名单}}, {"ctx": ctx})`。
- 白名单内置：abs/min/max/sum/len/round/sorted/str/int/float/bool/list/dict/set/isinstance/any/all/range/zip/enumerate。
- 两种写法：纯表达式 `(ctx['a']-ctx['b'])/ctx['a']` 或 `def run(ctx): ...`。

## Action 治理模型

| 属性 | 说明 |
|---|---|
| preconditions | 对目标实体属性的条件列表（op: eq/ne/gt/gte/lt/lte/has/in，field/value） |
| required_role | 必须匹配的执行角色 |
| risk | low/medium/high（登记用，供上层审批决策） |
| idempotent | 支持幂等键重放 |
| side_effect | 受控副作用：`{"field": "status", "value": "approved"}`，重新过类型校验 |

执行顺序：角色 → 策略 → 幂等重放 → 前置条件 → 副作用 → 审计。

## 权限模型

- 四角色默认：admin / manager / operator / viewer。
- 规则三元组：`(role, resource, action) -> effect`；`resource` 可为类型名或 `*`。
- 校验策略：存在任一 allow 且无 deny 即放行；无规则拒绝。
- 所有变更命令强制校验，不可用 `policy check` 绕过。

## 审计模型

- 每条记录：ts / actor / role / op / target_type / target_id / detail(JSON) / result。
- `op` 前缀区分：`entity_*`、`link_relate`、`state_transition`、`method_run`、`action:<name>`、`policy_add` 等。
- Action 幂等重放通过 op=`action:<name>` + target_id=`<id>:<key>` 定位首次结果。

## 血缘模型

- `lineage(child_id, parent_id, rel_type)`：派生关系有向边。
- `lineage trace` 递归回溯父链，防止环（visited 集合）。

## 已知边界

- Method 为确定性计算沙箱，不替代业务系统事务。
- Action 副作用仅限注册声明的单字段赋值；复杂副作用（多表/跨系统）需外部集成。
- 权限为单角色（非多角色叠加）；跨部门/租户隔离需扩展 resource 命名空间。
- SQLite 单文件适合中小规模（万级实体）；更大规模迁移 PostgreSQL 需重写 store 层。
