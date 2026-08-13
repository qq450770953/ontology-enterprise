---
name: ontology-enterprise
description: Enterprise-grade typed knowledge graph runtime for agent memory, business object modeling and governed actions. Use when creating/querying entities (Person, Project, Task, Request, Metric, Document), linking related objects, enforcing state machines, validating constraints, running deterministic business logic (Method), executing governed business actions (Action with preconditions/permissions/idempotency/audit), applying RBAC policies, tracing data lineage, resolving aliases, or when multiple skills need shared structured state with governance. Triggers: "记住/记录实体", "what do I know about X", "link X to Y", "show dependencies", "state transition", "权限校验", "审批动作", "血缘追踪", entity CRUD with versioning/effective dates, cross-skill governed data access.
agent_created: true
---

# Ontology Enterprise

企业级类型化知识图谱运行时：在企业对象之上提供 State/Method/Action/Policy 治理能力，替代简单 JSONL 存储，使用 SQLite 提供事务与并发安全。

## 核心概念

一切皆为带 **类型 (type)**、**属性 (properties)**、**关系 (relations)**、**状态 (state)** 的实体。每次变更先通过类型约束、状态机、策略与前置条件校验，再提交并写入审计日志。

```text
Entity: { id, type, properties, state, version, effective_from, effective_to, created, updated }
Relation: { from_id, relation_type, to_id, properties }
```

## 何时使用

| 触发场景 | 动作 |
|---|---|
| "记住/记录一个客户/任务/项目" | `object create` |
| "我知道关于 X 的什么？" | `object query/get` |
| "把 X 关联到 Y" | `link relate` |
| "任务从 open 流转到 in_progress" | `state transition` |
| "计算毛利率"（确定性业务逻辑） | `method run` |
| "批准这个申请"（受治理动作） | `action run` |
| "检查用户是否有写权限" | `policy check` |
| "这个报表数据来自哪里" | `lineage trace` |
| "华东区指的是哪个实体" | `object resolve` |

## 快速开始

```bash
# 1. 初始化：创建默认类型(Person/Project/Task/Event/Document/Metric) + 引导策略(admin/viewer/operator/manager)
python3 scripts/ontology_enterprise.py --root ./ontology init

# 2. 创建实体（管理员）
python3 scripts/ontology_enterprise.py --root ./ontology --actor alice --role admin \
  object create --type Person --props '{"name":"Alice"}'

# 3. 定义状态机并流转
python3 scripts/ontology_enterprise.py --root ./ontology state define \
  --type Task --states open,in_progress,blocked,done --initial open \
  --allow 'open>in_progress,open>blocked,in_progress>done,blocked>open,blocked>done'
python3 scripts/ontology_enterprise.py --root ./ontology --actor alice --role operator \
  state transition --id task_xxx --to in_progress
```

> 注意：`--root/--actor/--role` 必须放在子命令之前；`--id` 使用 create 返回的真实 ID（脚本示例中的 p_001 为占位）。

## 六大能力

### 1. Object 对象

- `type define --name X --definition '{...}'`：注册类型，含 schema（required/properties/enum）与 relations（from_types/to_types/cardinality/acyclic）。
- `object create/get/query/update/delete`：实体 CRUD；delete 默认软删除（archived），`--hard` 物理删除。
- `object alias-add / resolve`：别名消歧（namespace 隔离，如 "华东区"@sales）。
- 版本与生效时间：实体自带 `version`、`effective_from`、`effective_to` 字段；查询按属性过滤。

### 2. Link 关系

- `link relate --from X --rel r --to Y`：建立关系；违反 from_types/to_types、cardinality（many_to_one）、acyclic（环检测）时拒绝。
- `link related --id X [--rel r] [--direction outgoing|incoming|both]`：查询关系。

### 3. State 状态机

- `state define --type T --states s1,s2 --initial s1 --allow 's1>s2,s2>s3'`：定义类型状态机。
- `state show --id X`：查看当前状态。
- `state transition --id X --to s2`：合法流转提交；非法流转抛 `illegal state transition`。

### 4. Method 确定性方法

- `method register --name calc_margin --code '...'`：注册确定性只读逻辑（纯表达式或 `def run(ctx): ...`）。
- `method run --name calc_margin --ctx '{"revenue":100,"cost":60}'`：执行。
- 安全：注册时 AST 语法检查 + 禁止 `__dunder__`/import/os/sys/subprocess/eval/exec/compile；执行时仅暴露白名单内置函数。**Method 不应替代业务系统**。

### 5. Action 受治理动作

- `action register --name approve_request --preconditions '{"conditions":[{"op":"eq","field":"status","value":"pending"}]}' --required-role manager --risk medium --idempotent --side-effect '{"field":"status","value":"approved"}'`：注册动作（前置条件/角色/风险/幂等/副作用）。
- `action run --name approve_request --id req_001 [--idempotency-key k1]`：执行。
- 执行顺序：角色校验 → 策略校验 → 幂等重放检查 → 前置条件评估 → 受控副作用 → 审计落库。
- 幂等：同一 `--idempotency-key` 重复执行返回首次结果（`idempotent_replay: true`），不重复副作用。

### 6. Policy 权限 / Audit 审计 / Lineage 血缘

- `policy add --role viewer --resource Task --action read --effect allow`：RBAC 规则（`*` 通配资源）。
- `policy check --role viewer --resource Task --action write`：预检（所有写操作执行时也会强制校验）。
- `audit query --actor X [--target-id Y]`：审计日志（谁/何时/做了什么/结果）。
- `lineage add --child X --parent Y [--rel derived_from]` + `lineage trace --id X`：数据血缘追溯。

## 权限模型（默认引导策略）

| 角色 | 能力 |
|---|---|
| admin | 全量（含 Policy/Method/Action/Type 管理） |
| manager | 读/写 + Action execute + Method |
| operator | 读/写 + Method + Action execute |
| viewer | 只读 |

所有变更操作（create/update/delete/relate/transition/method/action/policy）都通过 `require_policy` 强制执行 RBAC；未授权操作抛 `policy denied`。

## 安全边界

- 存储：SQLite 于 `--root`（默认 `memory/ontology/ontology.db`），事务 + 并发安全；root 默认限制在工作区内。
- Method 沙箱：白名单内置函数、AST 校验、禁止导入与 IO。
- Action 副作用：仅在注册时声明的 `side_effect` 字段范围内修改实体，且重新过类型校验。
- 审计不可绕过：所有变更写 `audit_log`。
- 凭据：Credential 类对象只存引用（`secret_ref`），禁止直接存密码/token。

## 参考文档

- `references/schema.md`：类型定义、关系约束、状态机完整参考。
- `references/architecture.md`：六大能力与存储设计（对应企业级 Ontology Runtime 参考架构）。
- `references/workflows.md`：典型场景（供应链/审批/多 Skill 共享）端到端示例。

## 测试

```bash
python3 -m pytest tests/ -v
```

覆盖：对象 CRUD/约束、关系基数与环检测、状态机合法/非法流转、Method 沙箱与安全拦截、Action 权限/前置条件/幂等/副作用、Policy 放行与拒绝、审计查询、血缘追踪、别名消歧、版本与生效时间。
