"""Enterprise Ontology Runtime - verification suite.

Covers all six capabilities plus enhancements:
Object/Link/State/Method/Action/Policy-Audit + alias/version/lineage.
"""
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ontology_enterprise.py"
sys.path.insert(0, str(SCRIPT.parent))

from ontology_enterprise import OntologyDB, OntologyError  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    d = OntologyDB(tmp_path / "onto")
    # seed admin Policy write via raw SQL (chicken-and-egg: policy_add needs it)
    d.conn.execute(
        "INSERT OR REPLACE INTO policies(role,resource,action,effect) VALUES(?,?,?,?)",
        ("admin", "Policy", "write", "allow"),
    )
    # policies FIRST so type definition passes RBAC
    for res in ["Person", "Task", "Request", "Alias", "Method", "Action", "StateMachine", "Type", "Policy", "*"]:
        for act in ["read", "write", "delete", "execute"]:
            d.policy_add("admin", res, act, "allow", "t", "admin")
    d.policy_add("viewer", "*", "read", "allow", "t", "admin")
    d.policy_add("operator", "*", "read", "allow", "t", "admin")
    d.policy_add("operator", "*", "write", "allow", "t", "admin")
    d.policy_add("operator", "Method", "read", "allow", "t", "admin")
    d.policy_add("operator", "Method", "write", "allow", "t", "admin")
    d.policy_add("operator", "Action", "execute", "allow", "t", "admin")
    d.policy_add("manager", "*", "read", "allow", "t", "admin")
    d.policy_add("manager", "*", "write", "allow", "t", "admin")
    d.policy_add("manager", "Action", "execute", "allow", "t", "admin")
    d.define_type("Person", {
        "schema": {"required": ["name"], "properties": {
            "name": {"type": "string"},
            "email": {"type": "string"},
            "department": {"type": "string"},
        }}
    }, "t", "admin")
    d.define_type("Task", {
        "schema": {"required": ["title", "status"], "properties": {
            "title": {"type": "string"},
            "status": {"type": "string", "enum": ["open", "in_progress", "blocked", "done"]},
            "priority": {"type": "string", "enum": ["low", "medium", "high"]},
        }},
        "relations": {
            "has_owner": {"from_types": ["Task"], "to_types": ["Person"], "cardinality": "many_to_one"},
            "blocks": {"from_types": ["Task"], "to_types": ["Task"], "acyclic": True},
        },
    }, "t", "admin")
    d.define_type("Request", {
        "schema": {"required": ["title", "status"], "properties": {
            "title": {"type": "string"},
            "status": {"type": "string", "enum": ["pending", "approved", "rejected"]},
        }}
    }, "t", "admin")
    yield d
    d.close()


# ---------------------------------------------------------------------------
# 1. Object
# ---------------------------------------------------------------------------

class TestObject:
    def test_create_and_get(self, db):
        ent = db.create_entity("Person", {"name": "Alice"}, "a", "admin")
        got = db.get_entity(ent["id"])
        assert got["type"] == "Person"
        assert got["properties"]["name"] == "Alice"
        assert got["status"] == "active"

    def test_required_field_enforced(self, db):
        with pytest.raises(OntologyError, match="missing required property"):
            db.create_entity("Person", {"email": "a@b.com"}, "a", "admin")

    def test_enum_enforced(self, db):
        with pytest.raises(OntologyError, match="must be one of"):
            db.create_entity("Task", {"title": "t", "status": "nonsense"}, "a", "admin")

    def test_update_merge(self, db):
        ent = db.create_entity("Person", {"name": "Alice"}, "a", "admin")
        upd = db.update_entity(ent["id"], {"email": "a@b.com"}, "a", "admin")
        assert upd["properties"]["email"] == "a@b.com"
        assert upd["properties"]["name"] == "Alice"

    def test_delete_soft(self, db):
        ent = db.create_entity("Person", {"name": "Alice"}, "a", "admin")
        db.delete_entity(ent["id"], "a", "admin")
        assert db.get_entity(ent["id"])["status"] == "archived"

    def test_query_filter(self, db):
        db.create_entity("Task", {"title": "t1", "status": "open"}, "a", "admin")
        db.create_entity("Task", {"title": "t2", "status": "done"}, "a", "admin")
        res = db.query_entities("Task", {"status": "open"}, "a", "admin")
        assert len(res) == 1 and res[0]["properties"]["title"] == "t1"

    def test_alias_resolve(self, db):
        ent = db.create_entity("Project_placeholder" if False else "Person", {"name": "Alice"}, "a", "admin")
        db.alias_add(ent["id"], "华东区", "sales", "a", "admin")
        resolved = db.resolve_alias("华东区", "sales")
        assert resolved["id"] == ent["id"]

    def test_viewer_cannot_write(self, db):
        with pytest.raises(OntologyError, match="policy denied"):
            db.create_entity("Person", {"name": "Bob"}, "v", "viewer")


# ---------------------------------------------------------------------------
# 2. Link
# ---------------------------------------------------------------------------

class TestLink:
    def test_relate_and_related(self, db):
        p = db.create_entity("Person", {"name": "Alice"}, "a", "admin")
        t = db.create_entity("Task", {"title": "t1", "status": "open"}, "a", "admin")
        db.relate(t["id"], "has_owner", p["id"], actor="a", role="admin")
        out_rel = db.related(t["id"], "has_owner", "outgoing")
        assert out_rel[0]["to_id"] == p["id"]

    def test_cardinality_many_to_one(self, db):
        p1 = db.create_entity("Person", {"name": "A"}, "a", "admin")
        p2 = db.create_entity("Person", {"name": "B"}, "a", "admin")
        t = db.create_entity("Task", {"title": "t1", "status": "open"}, "a", "admin")
        db.relate(t["id"], "has_owner", p1["id"], actor="a", role="admin")
        with pytest.raises(OntologyError, match="cardinality"):
            db.relate(t["id"], "has_owner", p2["id"], actor="a", role="admin")

    def test_acyclic_relation_rejected(self, db):
        t1 = db.create_entity("Task", {"title": "a", "status": "open"}, "a", "admin")
        t2 = db.create_entity("Task", {"title": "b", "status": "open"}, "a", "admin")
        t3 = db.create_entity("Task", {"title": "c", "status": "open"}, "a", "admin")
        db.relate(t1["id"], "blocks", t2["id"], actor="a", role="admin")
        db.relate(t2["id"], "blocks", t3["id"], actor="a", role="admin")
        with pytest.raises(OntologyError, match="cycle"):
            db.relate(t3["id"], "blocks", t1["id"], actor="a", role="admin")


# ---------------------------------------------------------------------------
# 3. State
# ---------------------------------------------------------------------------

class TestState:
    def setup_state_machine(self, db):
        db.define_state_machine("Task", ["open", "in_progress", "blocked", "done"],
                                "open",
                                [{"from": "open", "to": "in_progress"},
                                 {"from": "open", "to": "blocked"},
                                 {"from": "in_progress", "to": "done"},
                                 {"from": "blocked", "to": "open"},
                                 {"from": "blocked", "to": "done"}],
                                "a", "admin")

    def test_initial_state(self, db):
        self.setup_state_machine(db)
        t = db.create_entity("Task", {"title": "t", "status": "open"}, "a", "admin")
        assert db.entity_state(t["id"]) == "open"

    def test_legal_transition(self, db):
        self.setup_state_machine(db)
        t = db.create_entity("Task", {"title": "t", "status": "open"}, "a", "admin")
        r = db.transition(t["id"], "in_progress", "a", "admin")
        assert r["to"] == "in_progress"
        assert db.entity_state(t["id"]) == "in_progress"

    def test_illegal_transition_rejected(self, db):
        self.setup_state_machine(db)
        t = db.create_entity("Task", {"title": "t", "status": "open"}, "a", "admin")
        with pytest.raises(OntologyError, match="illegal state transition"):
            db.transition(t["id"], "done", "a", "admin")  # open->done not allowed


# ---------------------------------------------------------------------------
# 4. Method
# ---------------------------------------------------------------------------

class TestMethod:
    def test_register_and_run_expression(self, db):
        db.method_register("margin", "(ctx['revenue']-ctx['cost'])/ctx['revenue']", actor="a", role="admin")
        r = db.method_run("margin", {"revenue": 100, "cost": 60}, "a", "admin")
        assert r["result"] == 0.4

    def test_register_and_run_function(self, db):
        db.method_register("growth", "def run(ctx):\n    base = ctx['base']\n    return round((ctx['now']-base)/base, 4)",
                           actor="a", role="admin")
        r = db.method_run("growth", {"base": 100, "now": 150}, "a", "admin")
        assert r["result"] == 0.5

    def test_os_access_blocked(self, db):
        with pytest.raises(OntologyError, match="forbidden"):
            db.method_register("evil", "__import__('os').system('whoami')", actor="a", role="admin")

    def test_syntax_error_blocked(self, db):
        with pytest.raises(OntologyError, match="syntax"):
            db.method_register("bad", "def run(ctx: return 1", actor="a", role="admin")

    def test_dunder_blocked(self, db):
        with pytest.raises(OntologyError, match="forbidden"):
            db.method_register("evil2", "().__class__.__bases__", actor="a", role="admin")

    def test_operator_can_run(self, db):
        db.method_register("double", "ctx['x']*2", actor="a", role="admin")
        r = db.method_run("double", {"x": 21}, "op", "operator")
        assert r["result"] == 42


# ---------------------------------------------------------------------------
# 5. Action
# ---------------------------------------------------------------------------

class TestAction:
    def _setup(self, db):
        req = db.create_entity("Request", {"title": "hire", "status": "pending"}, "a", "admin")
        db.action_register("approve", {"conditions": [{"op": "eq", "field": "status", "value": "pending"}]},
                           required_role="manager", risk="medium", idempotent=True,
                           side_effect={"field": "status", "value": "approved"},
                           actor="a", role="admin")
        return req

    def test_role_required(self, db):
        req = self._setup(db)
        with pytest.raises(OntologyError, match="requires role manager"):
            db.action_run("approve", req["id"], "v", "viewer")

    def test_run_with_side_effect(self, db):
        req = self._setup(db)
        r = db.action_run("approve", req["id"], "m", "manager")
        assert r["result"]["applied"] == {"status": "approved"}
        assert db.get_entity(req["id"])["properties"]["status"] == "approved"
        # _state and business status stay in sync
        assert db.get_entity(req["id"])["properties"]["_state"] == "approved"

    def test_state_transition_syncs_business_field(self, db):
        db.define_state_machine("Request", ["pending", "approved", "rejected"],
                                "pending",
                                [{"from": "pending", "to": "approved"},
                                 {"from": "pending", "to": "rejected"}],
                                "a", "admin")
        req = db.create_entity("Request", {"title": "r", "status": "pending"}, "a", "admin")
        db.transition(req["id"], "approved", "a", "admin")
        props = db.get_entity(req["id"])["properties"]
        assert props["_state"] == "approved"
        assert props["status"] == "approved"

    def test_precondition_fails_after_apply(self, db):
        req = self._setup(db)
        db.action_run("approve", req["id"], "m", "manager")
        with pytest.raises(OntologyError, match="precondition failed"):
            db.action_run("approve", req["id"], "m", "manager")

    def test_idempotent_replay(self, db):
        req = self._setup(db)
        r1 = db.action_run("approve", req["id"], "m", "manager", idempotency_key="k1")
        assert r1.get("idempotent_replay") is not True
        r2 = db.action_run("approve", req["id"], "m", "manager", idempotency_key="k1")
        assert r2["idempotent_replay"] is True

    def test_unknown_action(self, db):
        req = self._setup(db)
        with pytest.raises(OntologyError, match="not registered"):
            db.action_run("nope", req["id"], "m", "manager")


# ---------------------------------------------------------------------------
# 6. Policy / Audit / Lineage
# ---------------------------------------------------------------------------

class TestGovernance:
    def test_policy_allow_and_deny(self, db):
        assert db.policy_check("viewer", "Task", "read")["allowed"] is True
        assert db.policy_check("viewer", "Task", "write")["allowed"] is False

    def test_policy_enforced_on_read(self, db):
        db.create_entity("Person", {"name": "A"}, "a", "admin")
        # viewer has read on '*' -> query allowed
        assert len(db.query_entities("Person", None, "v", "viewer")) == 1
        # viewer cannot write -> create rejected
        with pytest.raises(OntologyError, match="policy denied"):
            db.create_entity("Person", {"name": "B"}, "v", "viewer")

    def test_audit_recorded(self, db):
        ent = db.create_entity("Person", {"name": "A"}, "alice", "admin")
        rows = db.audit_query(actor_filter="alice")
        assert any(r["op"] == "entity_create" and r["target_id"] == ent["id"] for r in rows)

    def test_action_audit(self, db):
        req = db.create_entity("Request", {"title": "r", "status": "pending"}, "a", "admin")
        db.action_register("go", {"conditions": []}, required_role=None, actor="a", role="admin")
        db.action_run("go", req["id"], "bob", "admin")
        rows = db.audit_query(actor_filter="bob")
        assert any(r["op"] == "action:go" for r in rows)

    def test_lineage_trace(self, db):
        p = db.create_entity("Person", {"name": "A"}, "a", "admin")
        t = db.create_entity("Task", {"title": "t", "status": "open"}, "a", "admin")
        db.lineage_add(t["id"], p["id"], "assigned_to", "a")
        chain = db.lineage_trace(t["id"])
        assert any(c["id"] == p["id"] for c in chain)


# ---------------------------------------------------------------------------
# Version & effective dates
# ---------------------------------------------------------------------------

class TestVersioning:
    def test_entity_has_version_and_effective_dates(self, db):
        ent = db.create_entity("Person", {"name": "A"}, "a", "admin",
                               effective_from="2026-01-01", effective_to="2026-12-31")
        assert ent["version"] == 1
        assert ent["effective_from"] == "2026-01-01"
        assert ent["effective_to"] == "2026-12-31"
