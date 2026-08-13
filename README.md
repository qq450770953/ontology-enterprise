# Ontology Enterprise

企业级类型化知识图谱运行时（Enterprise Ontology Runtime）。在企业业务对象之上提供 **State / Method / Action / Policy** 治理能力，替代简单 JSONL 存储，基于 SQLite 提供事务与并发安全。

English: A typed knowledge-graph runtime with governance — entities, relations, state machines, deterministic methods, governed actions, RBAC policies, audit and lineage, backed by SQLite.

## 特性（六大能力）

| 能力 | 说明 |
|---|---|
| **Object 对象** | 类型化实体（schema：required / enum / type 校验），别名消歧（namespace），版本与生效时间，软删除 |
| **Link 关系** | 业务关系，支持 from/to 类型白名单、基数约束（many_to_one）、环检测（acyclic） |
| **State 状态机** | 按类型定义状态机与合法流转，非法流转直接拒绝（如 `open -> done` 非法） |
| **Method 确定性方法** | 注册并执行确定性只读业务逻辑（白名单沙箱，禁止 import / os / eval / exec） |
| **Action 受治理动作** | 前置条件评估、角色要求、幂等键重放、受控副作用、风险登记 |
| **Policy / Audit / Lineage** | RBAC 权限强制校验、全操作审计日志、数据血缘追溯 |

## 快速开始

```bash
# 1. 初始化：创建默认类型(Person/Project/Task/Event/Document/Metric) + 引导策略(admin/manager/operator/viewer)
python3 scripts/ontology_enterprise.py --root ./ontology init

# 2. 创建实体（管理员）
python3 scripts/ontology_enterprise.py --root ./ontology --actor alice --role admin \
  object create --type Person --props '{"name":"Alice"}'

# 3. 定义状态机并流转
python3 scripts/ontology_enterprise.py --root ./ontology state define \
  --type Task --states open,in_progress,blocked,done --initial open \
  --allow 'open>in_progress,open>blocked,in_progress>done,blocked>open,blocked>done'
python3 scripts/ontology_enterprise.py --root ./ontology --actor alice --role operator \
  state transition --id <task_id> --to in_progress
```

> `--root / --actor / --role` 必须放在子命令之前；`--id` 使用 `object create` 返回的真实 ID。

## 典型场景：供应链计划审批

```bash
# operator 创建计划并提交（draft -> submitted）
python3 scripts/ontology_enterprise.py --root ./ontology --actor li --role operator \
  object create --type ProductionPlan --props '{"plan_no":"PL-001","status":"draft","qty":100}'
python3 scripts/ontology_enterprise.py --root ./ontology --actor li --role operator \
  state transition --id <plan_id> --to submitted

# 注册审批动作：仅 manager 可执行，前置条件 status=submitted，幂等，副作用置 approved
python3 scripts/ontology_enterprise.py --root ./ontology action register \
  --name approve_plan \
  --preconditions '{"conditions":[{"op":"eq","field":"status","value":"submitted"}]}' \
  --required-role manager --risk high --idempotent \
  --side-effect '{"field":"status","value":"approved"}'

# operator 尝试审批 → 拒绝（requires role manager）
# manager 审批成功 + 幂等重放
python3 scripts/ontology_enterprise.py --root ./ontology --actor zhang --role manager \
  action run --name approve_plan --id <plan_id> --idempotency-key "k-PL-001"

# 审计追溯
python3 scripts/ontology_enterprise.py --root ./ontology audit query --actor zhang
```

## 权限模型（默认引导策略）

| 角色 | 能力 |
|---|---|
| admin | 全量（含 Policy / Method / Action / Type 管理） |
| manager | 读 / 写 + Action execute + Method |
| operator | 读 / 写 + Method + Action execute |
| viewer | 只读 |

所有变更操作（create / update / delete / relate / transition / method / action / policy）都通过 `require_policy` 强制执行 RBAC；未授权操作抛 `policy denied`。

## 安全设计

- **存储**：SQLite（默认 `memory/ontology/ontology.db`），事务 + 并发安全；root 默认限制在工作区内（路径穿越拒绝）。
- **Method 沙箱**：AST 语法检查 + 黑名单（`__dunder__` / import / os / sys / subprocess / eval / exec / compile），执行时仅暴露白名单内置函数；支持纯表达式与 `def run(ctx): ...` 两种写法。
- **Action 副作用**：仅在注册时声明的 `side_effect` 字段范围内修改实体，且重新过类型校验；`_state` 与业务 `status` 字段保持同步。
- **审计不可绕过**：所有变更写入 `audit_log`（ts / actor / role / op / target / detail / result）。
- **凭据**：Credential 类对象只存引用（`secret_ref`），禁止直接存储密码 / token。

## 命令总览

```
ontology_enterprise.py [--root DIR] [--actor USER] [--role ROLE] <command>
  init                      初始化默认类型与引导策略
  object   create|get|update|delete|query|alias-add|resolve
  type     define
  link     relate|related
  state    define|show|transition
  method   register|run
  action   register|run
  policy   add|check
  audit    query
  lineage  add|trace
```

## 测试

```bash
python3 -m pytest tests/ -v
```

32 个测试覆盖：对象 CRUD 与约束、关系基数与环检测、状态机合法 / 非法流转、Method 沙箱与安全拦截、Action 权限 / 前置条件 / 幂等 / 副作用、Policy 放行与拒绝、审计查询、血缘追踪、别名消歧、版本与生效时间。

## 目录结构

```
ontology-enterprise/
├── SKILL.md                      技能入口（WorkBuddy / OpenClaw 兼容）
├── scripts/
│   └── ontology_enterprise.py    核心引擎（CLI + SQLite 存储 + 六能力）
├── references/
│   ├── architecture.md           架构设计（对应企业级 Ontology Runtime 参考架构）
│   ├── schema.md                 类型 / 关系 / 状态机 / 策略参考
│   └── workflows.md              供应链 / 指标血缘 / 别名消歧 / 多 Skill 共享示例
└── tests/
    └── test_enterprise.py        验证套件
```

## 参考

- 设计对齐企业级 Ontology Runtime 参考架构（Object / Link / State / Method / Action / Policy-Audit 六能力分层）。
- 可与 LLM Wiki / RAG 组合：Wiki 沉淀稳定认识，本工具提供机器可用的结构化语义层与受治理动作。

## 设计来源

本仓库是架构设计 **[wiki-ontology-agent-architecture](https://github.com/qq450770953/wiki-ontology-agent-architecture)** 的**可运行参考实现**（设计文档 → 实现能力映射详见该仓库 docs/architecture.md §11.1）。核心对应：

| 架构设计概念 | 本实现能力 |
|---|---|
| 语义层：Ontology 实体 / 别名 / 关系 / 约束 | `type define` + `object` + `link`（基数、环检测）+ `object alias-add/resolve` |
| 治理层：口径版本 / 约束校验 / 状态机 | `state` 状态机 + `method` 确定性沙箱 + schema 校验 |
| 执行层：受治理动作（前置条件 / 权限 / 幂等） | `action register/run` + `policy`（RBAC） |
| 知识复利：固化写回 / 版本失效 | SQLite 追加式状态 + 实体 `supersedes_id` / status 失效机制 |
| 治理层：审计 / 血缘 | `audit` + `lineage` |

该设计方案的六层架构（入口路由 / 知识层 / 语义层 / 执行层 / 接入层 / 治理层）与"双路径 + 知识复利"闭环，是本仓库能力设计的依据；本仓库聚焦其中"语义层 + 执行层 + 治理层"的可运行底座。

## License

MIT
