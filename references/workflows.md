# 工作流示例

端到端场景演示（供应链 / 审批 / 多 Skill 共享）。

## 场景 A：供应链计划审批（组合全部能力）

```bash
# 1. 初始化
python3 scripts/ontology_enterprise.py --root ./ontology init

# 2. 定义计划类型 + 状态机
python3 scripts/ontology_enterprise.py --root ./ontology type define \
  --name ProductionPlan --definition '{
    "schema": {"required": ["plan_no","status"], "properties": {
      "plan_no": {"type": "string"},
      "status": {"type": "string", "enum": ["draft","submitted","approved","rejected"]},
      "qty": {"type": "number"}
    }},
    "relations": {"has_material": {"from_types": ["ProductionPlan"], "to_types": ["Material"]}}
  }'
python3 scripts/ontology_enterprise.py --root ./ontology type define \
  --name Material --definition '{
    "schema": {"required": ["code"], "properties": {
      "code": {"type": "string"}, "stock": {"type": "number"}
    }}
  }'
python3 scripts/ontology_enterprise.py --root ./ontology state define \
  --type ProductionPlan --states draft,submitted,approved,rejected --initial draft \
  --allow 'draft>submitted,submitted>approved,submitted>rejected'

# 3. 创建物料与计划（operator）
M=$(python3 scripts/ontology_enterprise.py --root ./ontology --actor li --role operator \
  object create --type Material --props '{"code":"M-001","stock":500}' | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")
P=$(python3 scripts/ontology_enterprise.py --root ./ontology --actor li --role operator \
  object create --type ProductionPlan --props '{"plan_no":"PL-2026-001","status":"draft","qty":100}' | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")
python3 scripts/ontology_enterprise.py --root ./ontology link relate --from "$P" --rel has_material --to "$M"

# 4. 提交（draft -> submitted）
python3 scripts/ontology_enterprise.py --root ./ontology --actor li --role operator \
  state transition --id "$P" --to submitted

# 5. 注册审批动作：仅 manager 可执行，前置条件 status=submitted，幂等，副作用置 approved
python3 scripts/ontology_enterprise.py --root ./ontology action register \
  --name approve_plan \
  --preconditions '{"conditions":[{"op":"eq","field":"status","value":"submitted"}]}' \
  --required-role manager --risk high --idempotent \
  --side-effect '{"field":"status","value":"approved"}'

# 6. operator 尝试审批 → 被拒
python3 scripts/ontology_enterprise.py --root ./ontology --actor li --role operator \
  action run --name approve_plan --id "$P"   # ERROR: requires role manager

# 7. manager 审批成功 + 重放幂等
python3 scripts/ontology_enterprise.py --root ./ontology --actor zhang --role manager \
  action run --name approve_plan --id "$P" --idempotency-key "approve-PL-2026-001"
python3 scripts/ontology_enterprise.py --root ./ontology --actor zhang --role manager \
  action run --name approve_plan --id "$P" --idempotency-key "approve-PL-2026-001"
  # → {"idempotent_replay": true}

# 8. 审计追溯
python3 scripts/ontology_enterprise.py --root ./ontology audit query --actor zhang
```

## 场景 B：指标口径与血缘

```bash
# 指标实体 + 血缘：毛利 = 营收 - 成本
R=$(python3 scripts/ontology_enterprise.py --root ./ontology object create --type Metric \
  --props '{"name":"revenue","value":1000}' | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")
C=$(python3 scripts/ontology_enterprise.py --root ./ontology object create --type Metric \
  --props '{"name":"cost","value":600}' | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")

# 注册口径 Method（版本化确定性计算）
python3 scripts/ontology_enterprise.py --root ./ontology method register \
  --name gross_margin --code "(ctx['revenue']-ctx['cost'])/ctx['revenue']"
python3 scripts/ontology_enterprise.py --root ./ontology method run \
  --name gross_margin --ctx '{"revenue":1000,"cost":600}'   # 0.4

# 血缘：报表实体派生自两个指标
python3 scripts/ontology_enterprise.py --root ./ontology lineage add --child "$R" --parent "$R" --rel self
python3 scripts/ontology_enterprise.py --root ./ontology lineage trace --id "$R"
```

## 场景 C：别名消歧（"华东区"）

```bash
# 区域实体 + 别名
EA=$(python3 scripts/ontology_enterprise.py --root ./ontology object create --type Project \
  --props '{"name":"East China","status":"active"}' | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")
python3 scripts/ontology_enterprise.py --root ./ontology object alias-add --id "$EA" --alias "华东区" --namespace sales
python3 scripts/ontology_enterprise.py --root ./ontology object resolve --alias "华东区" --namespace sales
# → 返回 East China 实体
```

## 场景 D：多 Skill 共享状态

多个 skill 以 `--root` 指向同一 ontology 目录即共享图谱；通过 `--role` 控制各自权限边界：

```bash
# skill A（只读）用 viewer
python3 scripts/ontology_enterprise.py --root ./ontology --actor skillA --role viewer object query --type Task
# skill B（可写）用 operator
python3 scripts/ontology_enterprise.py --root ./ontology --actor skillB --role operator object create --type Task --props '{"title":"x","status":"open"}'
```
