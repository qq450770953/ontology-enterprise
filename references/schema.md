# Schema 参考

类型定义、关系约束与状态机规范。

## 类型定义（type define）

```json
{
  "schema": {
    "required": ["title", "status"],
    "properties": {
      "title": {"type": "string"},
      "status": {"type": "string", "enum": ["open", "in_progress", "blocked", "done"]},
      "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"]},
      "assignee": {"type": "string"},
      "due": {"type": "string"},
      "estimate_hours": {"type": "number"},
      "archived": {"type": "boolean"}
    }
  },
  "relations": {
    "has_owner": {"from_types": ["Project", "Task"], "to_types": ["Person"], "cardinality": "many_to_one"},
    "blocks": {"from_types": ["Task"], "to_types": ["Task"], "acyclic": true}
  }
}
```

### 属性校验规则

| 规则 | 说明 | 示例 |
|---|---|---|
| `required` | 必填字段列表 | `["title", "status"]` |
| `properties.<f>.type` | 类型检查 string/number/boolean | `{"type": "number"}` |
| `properties.<f>.enum` | 枚举白名单 | `{"enum": ["low", "medium", "high"]}` |

### 关系约束

| 规则 | 说明 |
|---|---|
| `from_types` / `to_types` | 源/目标类型白名单，越界拒绝 |
| `cardinality: many_to_one` | 同一 from+rel 只能指向一个 to |
| `acyclic: true` | 禁止成环（DFS 检测，任务依赖典型场景） |

未声明的关系默认允许（宽松模式）。

## 状态机（state define）

```text
--states open,in_progress,blocked,done --initial open
--allow 'open>in_progress,open>blocked,in_progress>done,blocked>open,blocked>done'
```

- 状态存于实体属性 `_state`（首写时取 initial）。
- 仅 `--allow` 中声明的 `from>to` 对合法；其余抛 `illegal state transition`。
- 状态流转也是受治理写操作：需角色有目标类型 write 权限。

## 默认引导类型

| 类型 | 必填 | 备注 |
|---|---|---|
| Person | name | email/phone/department |
| Project | name | status 枚举 planning/active/paused/completed/archived |
| Task | title, status | status 枚举 open/in_progress/blocked/done/cancelled；has_owner/blocks 关系 |
| Event | title, start | end >= start 需通过 Method 校验 |
| Document | title | url/path/summary |
| Metric | name | value/dimension/version/effective_from |

## 引导策略（bootstrap）

| 角色 | 规则 |
|---|---|
| admin | `*:*:*` 全放行 + Type/Policy/Method/Action/StateMachine/Alias 管理 |
| manager | `*` read/write + Action execute + Method read/write |
| operator | `*` read/write + Method read/write + Action execute |
| viewer | `*` read |

## 存储

- 数据库：`--root/ontology.db`（SQLite，事务 + 并发安全）
- 表：types / entities / relations / aliases / state_machines / methods / actions / policies / audit_log / lineage
- root 默认 `memory/ontology/`，限制在工作区内（路径穿越拒绝）
