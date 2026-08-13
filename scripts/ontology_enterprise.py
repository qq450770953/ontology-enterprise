#!/usr/bin/env python3
"""
Enterprise Ontology Runtime - typed knowledge graph with governance.

Six capabilities (per enterprise Ontology Runtime reference architecture):
  1. Object   - typed entities with properties, aliases, versions, effective dates
  2. Link     - business relations with cardinality / acyclicity constraints
  3. State    - per-type state machines with legal transition validation
  4. Method   - deterministic read-only business logic (registered, whitelisted)
  5. Action   - governed business actions (preconditions, permissions, idempotency, audit)
  6. Policy   - RBAC permission model + audit log + data lineage

Storage: SQLite (transactional, concurrent-safe). Default: memory/ontology/ontology.db

Usage:
    python ontology_enterprise.py object create --type Person --props '{"name":"Alice"}' --actor alice
    python ontology_enterprise.py object get --id p_001
    python ontology_enterprise.py object query --type Task --where '{"status":"open"}'
    python ontology_enterprise.py object update --id p_001 --props '{"email":"a@b.com"}'
    python ontology_enterprise.py object delete --id p_001
    python ontology_enterprise.py object alias-add --id p_001 --alias "Alice Chen" --namespace cn
    python ontology_enterprise.py object resolve --alias "Alice Chen" --namespace cn
    python ontology_enterprise.py link relate --from proj_001 --rel has_task --to task_001
    python ontology_enterprise.py link related --id proj_001 --rel has_task
    python ontology_enterprise.py state define --type Task --states open,in_progress,blocked,done --initial open --allow open>in_progress,open>blocked,in_progress>done,blocked>open,blocked>done
    python ontology_enterprise.py state show --id task_001
    python ontology_enterprise.py state transition --id task_001 --to in_progress
    python ontology_enterprise.py method register --name calc_margin --code "def run(ctx):\n    return (ctx['revenue']-ctx['cost'])/ctx['revenue']"
    python ontology_enterprise.py method run --name calc_margin --ctx '{"revenue":100,"cost":60}'
    python ontology_enterprise.py action register --name approve_request --preconditions '{"status":"pending"}' --required-role manager --risk medium --idempotent
    python ontology_enterprise.py action run --name approve_request --id req_001 --actor bob --role manager
    python ontology_enterprise.py policy add --role viewer --resource Task --action read --effect allow
    python ontology_enterprise.py policy check --role viewer --resource Task --action write
    python ontology_enterprise.py audit query --actor bob
    python ontology_enterprise.py lineage trace --id req_001
"""

import argparse
import ast
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROOT = "memory/ontology"
DEFAULT_DB = "ontology.db"
DEFAULT_SCHEMA = "schema.yaml"

SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS types (
    name            TEXT PRIMARY KEY,
    definition      TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entities (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    properties      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    version         INTEGER NOT NULL DEFAULT 1,
    supersedes_id   TEXT,
    effective_from  TEXT,
    effective_to    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY(type) REFERENCES types(name)
);
CREATE TABLE IF NOT EXISTS relations (
    id              TEXT PRIMARY KEY,
    from_id         TEXT NOT NULL,
    rel_type        TEXT NOT NULL,
    to_id           TEXT NOT NULL,
    properties      TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE(from_id, rel_type, to_id)
);
CREATE TABLE IF NOT EXISTS aliases (
    entity_id       TEXT NOT NULL,
    alias           TEXT NOT NULL,
    namespace       TEXT NOT NULL DEFAULT 'default',
    PRIMARY KEY(alias, namespace)
);
CREATE TABLE IF NOT EXISTS state_machines (
    type_name       TEXT PRIMARY KEY,
    states          TEXT NOT NULL,
    initial         TEXT NOT NULL,
    transitions     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS methods (
    name            TEXT PRIMARY KEY,
    code            TEXT NOT NULL,
    description     TEXT,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    name            TEXT PRIMARY KEY,
    preconditions   TEXT,
    required_role   TEXT,
    risk            TEXT NOT NULL DEFAULT 'low',
    idempotent      INTEGER NOT NULL DEFAULT 0,
    side_effect     TEXT,
    description     TEXT,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policies (
    role            TEXT NOT NULL,
    resource        TEXT NOT NULL,
    action          TEXT NOT NULL,
    effect          TEXT NOT NULL DEFAULT 'allow',
    PRIMARY KEY(role, resource, action)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    actor           TEXT NOT NULL,
    role            TEXT,
    op              TEXT NOT NULL,
    target_type     TEXT,
    target_id       TEXT,
    detail          TEXT,
    result          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lineage (
    child_id        TEXT NOT NULL,
    parent_id       TEXT NOT NULL,
    rel_type        TEXT NOT NULL DEFAULT 'derived_from',
    created_at      TEXT NOT NULL,
    PRIMARY KEY(child_id, parent_id, rel_type)
);
"""


class OntologyError(Exception):
    """Business rule violation or governance rejection."""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Storage layer
# ---------------------------------------------------------------------------

class OntologyDB:
    """SQLite-backed store with schema init, path confinement and transactions."""

    def __init__(self, root: Path | None = None):
        cwd = Path.cwd().resolve()
        self.root = (root or (cwd / DEFAULT_ROOT)).resolve()
        # confine: must be inside cwd unless explicitly given absolute path
        try:
            self.root.relative_to(cwd)
        except ValueError:
            if not root:
                raise OntologyError(f"refusing to use root outside workspace: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / DEFAULT_DB
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA_TABLE)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def tx(self):
        return self.conn

    # --- helpers ---

    def audit(self, actor, op, target_type=None, target_id=None, detail=None, result="ok", role=None):
        self.conn.execute(
            "INSERT INTO audit_log(ts,actor,role,op,target_type,target_id,detail,result) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (utcnow(), actor, role, op, target_type, target_id,
             json.dumps(detail, ensure_ascii=False) if detail is not None else None, result),
        )

    def require_policy(self, actor, role, resource, action):
        """RBAC check: role must have allow on (resource, action); wildcard '*' supported."""
        if not role:
            raise OntologyError(f"no role provided for actor '{actor}' - policy check denied")
        rows = self.conn.execute(
            "SELECT effect FROM policies WHERE role=? AND action=? AND (resource=? OR resource='*')",
            (role, action, resource),
        ).fetchall()
        if not rows:
            raise OntologyError(
                f"policy denied: role '{role}' has no rule for action '{action}' on '{resource}'"
            )
        if all(r["effect"] == "deny" for r in rows):
            raise OntologyError(
                f"policy denied: role '{role}' explicitly denied for '{action}' on '{resource}'"
            )
        return True

    # --- Object capability ---

    def define_type(self, name, definition, actor="system", role="admin"):
        self.require_policy(actor, role, "Type", "write")
        now = utcnow()
        self.conn.execute(
            "INSERT INTO types(name,definition,version,updated_at) VALUES(?,?,1,?) "
            "ON CONFLICT(name) DO UPDATE SET definition=excluded.definition, "
            "version=types.version+1, updated_at=excluded.updated_at",
            (name, json.dumps(definition, ensure_ascii=False), now),
        )
        self.audit(actor, "type_define", "Type", name, {"definition": definition}, role=role)
        return name

    def get_type(self, name):
        row = self.conn.execute("SELECT * FROM types WHERE name=?", (name,)).fetchone()
        if not row:
            raise OntologyError(f"unknown type: {name}")
        d = json.loads(row["definition"])
        d["_version"] = row["version"]
        return d

    def validate_props(self, type_name, props):
        """Type-level constraint validation: required, enum, types."""
        td = self.get_type(type_name)
        schema = td.get("schema", {})
        required = schema.get("required", [])
        for f in required:
            if f not in props or props[f] in (None, ""):
                raise OntologyError(f"missing required property '{f}' on {type_name}")
        for field, spec in (schema.get("properties") or {}).items():
            if field not in props:
                continue
            val = props[field]
            if isinstance(spec, dict):
                if "enum" in spec and val not in spec["enum"]:
                    raise OntologyError(
                        f"property '{field}' must be one of {spec['enum']}, got {val!r}"
                    )
                if spec.get("type") == "string" and not isinstance(val, str):
                    raise OntologyError(f"property '{field}' must be string")
                if spec.get("type") == "number" and not isinstance(val, (int, float)):
                    raise OntologyError(f"property '{field}' must be number")
                if spec.get("type") == "boolean" and not isinstance(val, bool):
                    raise OntologyError(f"property '{field}' must be boolean")
        # type-level validator hook (Method-like, deterministic)
        validator = td.get("validate")
        if validator:
            self._run_embedded(validator, {"type": type_name, "props": props}, "type-validate")

    def create_entity(self, type_name, props, actor="system", role="admin",
                      effective_from=None, effective_to=None, source=None):
        self.require_policy(actor, role, type_name, "write")
        self.validate_props(type_name, props)
        eid = gen_id(type_name.lower()[:3])
        now = utcnow()
        self.conn.execute(
            "INSERT INTO entities(id,type,properties,status,version,effective_from,effective_to,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (eid, type_name, json.dumps(props, ensure_ascii=False), "active", 1,
             effective_from, effective_to, now, now),
        )
        self.audit(actor, "entity_create", type_name, eid, {"props": props}, role=role)
        if source:
            self.lineage_add(eid, source, "sourced_from", actor)
        return self.get_entity(eid)

    def get_entity(self, eid, include_archived=True):
        row = self.conn.execute("SELECT * FROM entities WHERE id=?", (eid,)).fetchone()
        if not row:
            raise OntologyError(f"entity not found: {eid}")
        if not include_archived and row["status"] == "archived":
            raise OntologyError(f"entity archived: {eid}")
        ent = dict(row)
        ent["properties"] = json.loads(ent["properties"])
        return ent

    def update_entity(self, eid, props, actor="system", role="admin", merge=True):
        ent = self.get_entity(eid)
        self.require_policy(actor, role, ent["type"], "write")
        new_props = {**ent["properties"], **props} if merge else props
        self.validate_props(ent["type"], new_props)
        now = utcnow()
        self.conn.execute(
            "UPDATE entities SET properties=?, updated_at=? WHERE id=?",
            (json.dumps(new_props, ensure_ascii=False), now, eid),
        )
        self.audit(actor, "entity_update", ent["type"], eid, {"props": props}, role=role)
        return self.get_entity(eid)

    def delete_entity(self, eid, actor="system", role="admin", hard=False):
        ent = self.get_entity(eid)
        self.require_policy(actor, role, ent["type"], "delete")
        if hard:
            self.conn.execute("DELETE FROM entities WHERE id=?", (eid,))
            self.conn.execute("DELETE FROM relations WHERE from_id=? OR to_id=?", (eid, eid))
            self.audit(actor, "entity_delete_hard", ent["type"], eid, result="ok", role=role)
            return {"deleted": eid}
        self.conn.execute("UPDATE entities SET status='archived', updated_at=? WHERE id=?", (utcnow(), eid))
        self.audit(actor, "entity_archive", ent["type"], eid, result="ok", role=role)
        return {"archived": eid}

    def query_entities(self, type_name, where=None, actor="system", role="admin"):
        self.require_policy(actor, role, type_name, "read")
        rows = self.conn.execute(
            "SELECT * FROM entities WHERE type=? AND status!='archived'", (type_name,)
        ).fetchall()
        out = []
        for r in rows:
            ent = dict(r)
            ent["properties"] = json.loads(ent["properties"])
            if where:
                matched = True
                for k, v in where.items():
                    if ent["properties"].get(k) != v:
                        matched = False
                        break
                if not matched:
                    continue
            out.append(ent)
        return out

    # --- Link capability ---

    def relate(self, from_id, rel_type, to_id, props=None, actor="system", role="admin"):
        src = self.get_entity(from_id)
        dst = self.get_entity(to_id)
        self.require_policy(actor, role, src["type"], "write")
        self.require_policy(actor, role, dst["type"], "write")
        self._validate_relation(src, rel_type, dst)
        rid = gen_id("rel")
        now = utcnow()
        self.conn.execute(
            "INSERT OR IGNORE INTO relations(id,from_id,rel_type,to_id,properties,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (rid, from_id, rel_type, to_id, json.dumps(props or {}, ensure_ascii=False), now),
        )
        self.audit(actor, "link_relate", f"{src['type']}->{dst['type']}", f"{from_id}-{rel_type}-{to_id}",
                   {"props": props or {}}, role=role)
        return {"from": from_id, "rel": rel_type, "to": to_id}

    def _validate_relation(self, src, rel_type, dst):
        td = self.get_type(src["type"])
        rels = td.get("relations", {})
        spec = rels.get(rel_type)
        if spec is None:
            # fall back to global relation spec from type def
            spec = self.get_type(src["type"]).get("relation_defaults", {}).get(rel_type)
        if spec is None:
            return  # unconstrained relation allowed by default
        if "from_types" in spec and src["type"] not in spec["from_types"]:
            raise OntologyError(f"relation {rel_type} not allowed from {src['type']}")
        if "to_types" in spec and dst["type"] not in spec["to_types"]:
            raise OntologyError(f"relation {rel_type} not allowed to {dst['type']}")
        if spec.get("acyclic"):
            if self._path_exists(dst["id"], src["id"]):
                raise OntologyError(f"relation {rel_type} would create a cycle")
        if spec.get("cardinality") == "many_to_one":
            existing = self.conn.execute(
                "SELECT 1 FROM relations WHERE from_id=? AND rel_type=? AND to_id!=?",
                (src["id"], rel_type, dst["id"]),
            ).fetchone()
            if existing:
                raise OntologyError(f"cardinality violated: {rel_type} is many_to_one for {src['id']}")

    def _path_exists(self, start, target, visited=None):
        if visited is None:
            visited = set()
        if start in visited:
            return False
        visited.add(start)
        rows = self.conn.execute(
            "SELECT to_id FROM relations WHERE from_id=?", (start,)
        ).fetchall()
        for r in rows:
            if r["to_id"] == target:
                return True
            if self._path_exists(r["to_id"], target, visited):
                return True
        return False

    def related(self, eid, rel_type=None, direction="outgoing"):
        self.get_entity(eid)
        out = []
        if direction in ("outgoing", "both"):
            q = "SELECT r.*, e.type AS to_type FROM relations r JOIN entities e ON e.id=r.to_id WHERE r.from_id=?"
            args = [eid]
            if rel_type:
                q += " AND r.rel_type=?"
                args.append(rel_type)
            for r in self.conn.execute(q, args):
                out.append({"direction": "out", "rel": r["rel_type"], "to_id": r["to_id"], "to_type": r["to_type"]})
        if direction in ("incoming", "both"):
            q = "SELECT r.*, e.type AS from_type FROM relations r JOIN entities e ON e.id=r.from_id WHERE r.to_id=?"
            args = [eid]
            if rel_type:
                q += " AND r.rel_type=?"
                args.append(rel_type)
            for r in self.conn.execute(q, args):
                out.append({"direction": "in", "rel": r["rel_type"], "from_id": r["from_id"], "from_type": r["from_type"]})
        return out

    # --- Alias capability ---

    def alias_add(self, eid, alias, namespace="default", actor="system", role="admin"):
        self.get_entity(eid)
        self.require_policy(actor, role, "Alias", "write")
        self.conn.execute(
            "INSERT OR REPLACE INTO aliases(entity_id,alias,namespace) VALUES(?,?,?)",
            (eid, alias, namespace),
        )
        self.audit(actor, "alias_add", None, eid, {"alias": alias, "namespace": namespace}, role=role)
        return {"alias": alias, "namespace": namespace, "entity_id": eid}

    def resolve_alias(self, alias, namespace="default"):
        row = self.conn.execute(
            "SELECT entity_id FROM aliases WHERE alias=? AND namespace=?",
            (alias, namespace),
        ).fetchone()
        if not row:
            raise OntologyError(f"alias not found: {alias}@{namespace}")
        return self.get_entity(row["entity_id"])

    # --- State capability ---

    def define_state_machine(self, type_name, states, initial, transitions, actor="system", role="admin"):
        self.require_policy(actor, role, "StateMachine", "write")
        self.conn.execute(
            "INSERT INTO state_machines(type_name,states,initial,transitions) VALUES(?,?,?,?) "
            "ON CONFLICT(type_name) DO UPDATE SET states=excluded.states, initial=excluded.initial, "
            "transitions=excluded.transitions",
            (type_name, json.dumps(states), initial, json.dumps(transitions)),
        )
        self.audit(actor, "state_define", type_name, None, {"states": states, "initial": initial}, role=role)
        return {"type": type_name, "states": states, "initial": initial}

    def _state_info(self, type_name):
        row = self.conn.execute("SELECT * FROM state_machines WHERE type_name=?", (type_name,)).fetchone()
        if not row:
            raise OntologyError(f"no state machine defined for type {type_name}")
        return {
            "states": json.loads(row["states"]),
            "initial": row["initial"],
            "transitions": json.loads(row["transitions"]),
        }

    def _type_has_prop(self, type_name, prop):
        td = self.get_type(type_name)
        return prop in (td.get("schema", {}).get("properties") or {})

    def entity_state(self, eid):
        ent = self.get_entity(eid)
        state = ent["properties"].get("_state") or self._state_info(ent["type"])["initial"]
        return state

    def transition(self, eid, to_state, actor="system", role="admin"):
        ent = self.get_entity(eid)
        self.require_policy(actor, role, ent["type"], "write")
        sm = self._state_info(ent["type"])
        cur = self.entity_state(eid)
        if cur == to_state:
            return {"id": eid, "state": cur, "changed": False}
        legal = {(t["from"], t["to"]) for t in sm["transitions"]}
        if (cur, to_state) not in legal:
            raise OntologyError(
                f"illegal state transition: {cur} -> {to_state} for {ent['type']} "
                f"(allowed: {sorted(legal)})"
            )
        # sync both _state and the type's status field (if present) so Action
        # preconditions that read the business field see the same state
        new_props = {**ent["properties"], "_state": to_state}
        if "status" in ent["properties"] or self._type_has_prop(ent["type"], "status"):
            new_props["status"] = to_state
        now = utcnow()
        self.conn.execute(
            "UPDATE entities SET properties=?, updated_at=? WHERE id=?",
            (json.dumps(new_props, ensure_ascii=False), now, eid),
        )
        self.audit(actor, "state_transition", ent["type"], eid, {"from": cur, "to": to_state}, role=role)
        return {"id": eid, "from": cur, "to": to_state, "changed": True}

    # --- Method capability ---

    def _run_embedded(self, code, ctx, name):
        """Run a whitelisted deterministic function. Only safe builtins exposed.
        Supports two forms: a bare expression, or a 'def run(ctx): ...' body."""
        safe_globals = {
            "__builtins__": {
                "abs": abs, "min": min, "max": max, "sum": sum, "len": len,
                "round": round, "sorted": sorted, "str": str, "int": int,
                "float": float, "bool": bool, "list": list, "dict": dict,
                "set": set, "isinstance": isinstance, "any": any, "all": all,
                "range": range, "zip": zip, "enumerate": enumerate, "True": True,
                "False": False, "None": None,
            }
        }
        # reject obvious escapes
        banned = re.findall(r"(__\w+__|import\s|open\s*\(|os\.|sys\.|subprocess|eval\s*\(|exec\s*\(|compile\s*\()", code)
        if banned:
            raise OntologyError(f"method {name}: forbidden constructs {banned}")
        code = code.strip()
        try:
            if code.startswith("def "):
                ns = {}
                exec(code, safe_globals, ns)
                fn = ns.get("run")
                if not callable(fn):
                    raise OntologyError(f"method {name}: no 'run(ctx)' function defined")
                return fn(ctx)
            return eval(code, safe_globals, {"ctx": ctx})
        except OntologyError:
            raise
        except Exception as e:
            raise OntologyError(f"method {name} failed: {e}")

    def method_register(self, name, code, description=None, actor="system", role="admin"):
        self.require_policy(actor, role, "Method", "write")
        self._check_method_syntax(code, name)  # syntax + safety check at registration time
        self.conn.execute(
            "INSERT INTO methods(name,code,description,created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET code=excluded.code, description=excluded.description",
            (name, code, description, utcnow()),
        )
        self.audit(actor, "method_register", "Method", name, role=role)
        return {"name": name}

    def _check_method_syntax(self, code, name):
        """Syntax + escape validation without executing."""
        banned = re.findall(r"(__\w+__|import\s|open\s*\(|os\.|sys\.|subprocess|eval\s*\(|exec\s*\(|compile\s*\()", code)
        if banned:
            raise OntologyError(f"method {name}: forbidden constructs {banned}")
        try:
            ast.parse(code)
        except SyntaxError as e:
            raise OntologyError(f"method {name}: syntax error: {e}")
        return True

    def method_run(self, name, ctx, actor="system", role="admin"):
        self.require_policy(actor, role, "Method", "read")
        row = self.conn.execute("SELECT * FROM methods WHERE name=?", (name,)).fetchone()
        if not row:
            raise OntologyError(f"method not found: {name}")
        result = self._run_embedded(row["code"], ctx, name)
        self.audit(actor, "method_run", "Method", name, {"ctx": ctx, "result": result}, role=role)
        return {"method": name, "result": result}

    # --- Action capability ---

    def action_register(self, name, preconditions, required_role=None, risk="low",
                        idempotent=False, side_effect=None, description=None, actor="system", role="admin"):
        self.require_policy(actor, role, "Action", "write")
        self.conn.execute(
            "INSERT INTO actions(name,preconditions,required_role,risk,idempotent,side_effect,description,created_at) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET preconditions=excluded.preconditions, "
            "required_role=excluded.required_role, risk=excluded.risk, idempotent=excluded.idempotent, "
            "side_effect=excluded.side_effect",
            (name, json.dumps(preconditions or {}), required_role, risk, 1 if idempotent else 0,
             json.dumps(side_effect) if side_effect else None,
             description, utcnow()),
        )
        self.audit(actor, "action_register", "Action", name,
                   {"preconditions": preconditions, "risk": risk}, role=role)
        return {"name": name}

    def action_run(self, name, target_id, actor, role, params=None, idempotency_key=None):
        row = self.conn.execute("SELECT * FROM actions WHERE name=?", (name,)).fetchone()
        if not row:
            raise OntologyError(f"action not registered: {name}")
        action = dict(row)
        action["preconditions"] = json.loads(action["preconditions"] or "{}")
        action["side_effect"] = json.loads(action["side_effect"]) if action["side_effect"] else None
        # permission: action role + policy
        if action["required_role"] and role != action["required_role"]:
            raise OntologyError(
                f"action {name} requires role {action['required_role']}, actor {actor} has {role}"
            )
        self.require_policy(actor, role, "Action", "execute")
        # idempotency: same key returns prior result
        if idempotency_key:
            prior = self.conn.execute(
                "SELECT detail FROM audit_log WHERE op=? AND target_id=? AND result='ok' "
                "ORDER BY id DESC LIMIT 1",
                (f"action:{name}", f"{target_id}:{idempotency_key}"),
            ).fetchone()
            if prior:
                return {"idempotent_replay": True, "detail": json.loads(prior["detail"])}
        ent = self.get_entity(target_id)
        # preconditions evaluate against entity properties
        for cond in action["preconditions"].get("conditions", []):
            ok = self._eval_condition(cond, ent["properties"])
            if not ok:
                raise OntologyError(f"action {name} precondition failed: {cond}")
        # side effect: controlled mutation declared at registration time
        applied = None
        if action["side_effect"]:
            field = action["side_effect"].get("field")
            value = action["side_effect"].get("value")
            if field:
                new_props = {**ent["properties"], field: value}
                # keep _state and the business status field in sync
                if field in ("_state", "status"):
                    new_props["_state"] = value
                    new_props["status"] = value
                self.validate_props(ent["type"], new_props)
                self.conn.execute(
                    "UPDATE entities SET properties=?, updated_at=? WHERE id=?",
                    (json.dumps(new_props, ensure_ascii=False), utcnow(), target_id),
                )
                applied = {field: value}
        result = {
            "action": name,
            "target_id": target_id,
            "applied": applied,
            "params": params or {},
        }
        self.conn.execute(
            "INSERT INTO audit_log(ts,actor,role,op,target_type,target_id,detail,result) VALUES(?,?,?,?,?,?,?,?)",
            (utcnow(), actor, role, f"action:{name}", ent["type"], f"{target_id}:{idempotency_key or ''}",
             json.dumps(result, ensure_ascii=False), "ok"),
        )
        return {"action": name, "target_id": target_id, "result": result}

    def _eval_condition(self, cond, props):
        """Simple condition evaluator supporting eq/ne/gt/gte/lt/lte/has."""
        if isinstance(cond, dict):
            op = cond.get("op")
            field = cond.get("field")
            value = cond.get("value")
            actual = props.get(field)
            if op == "eq":
                return actual == value
            if op == "ne":
                return actual != value
            if op == "gt":
                return actual is not None and actual > value
            if op == "gte":
                return actual is not None and actual >= value
            if op == "lt":
                return actual is not None and actual < value
            if op == "lte":
                return actual is not None and actual <= value
            if op == "has":
                return field in props
            if op == "in":
                return actual in value
            raise OntologyError(f"unknown condition op: {op}")
        if isinstance(cond, str):
            # "field=value" or "field!=value"
            m = re.match(r"^([\w.]+)\s*(==|!=|=)\s*(.+)$", cond)
            if m:
                field, op, raw = m.groups()
                actual = props.get(field)
                try:
                    val = json.loads(raw)
                except json.JSONDecodeError:
                    val = raw.strip("\"'")
                if op in ("==", "="):
                    return actual == val
                return actual != val
            return props.get(cond) is not None
        return bool(cond)

    # --- Policy capability ---

    def policy_add(self, role, resource, action, effect="allow", actor="system", role_admin="admin"):
        self.require_policy(actor, role_admin, "Policy", "write")
        self.conn.execute(
            "INSERT OR REPLACE INTO policies(role,resource,action,effect) VALUES(?,?,?,?)",
            (role, resource, action, effect),
        )
        self.audit(actor, "policy_add", "Policy", f"{role}:{resource}:{action}", {"effect": effect}, role=role_admin)
        return {"role": role, "resource": resource, "action": action, "effect": effect}

    def policy_check(self, role, resource, action):
        try:
            self.require_policy("check", role, resource, action)
            return {"allowed": True}
        except OntologyError as e:
            return {"allowed": False, "reason": str(e)}

    # --- Audit capability ---

    def audit_query(self, actor_filter=None, op_filter=None, target_id=None, limit=50):
        q = "SELECT * FROM audit_log"
        conds, args = [], []
        if actor_filter:
            conds.append("actor=?")
            args.append(actor_filter)
        if op_filter:
            conds.append("op=?")
            args.append(op_filter)
        if target_id:
            conds.append("target_id LIKE ?")
            args.append(f"%{target_id}%")
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        rows = self.conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    # --- Lineage capability ---

    def lineage_add(self, child_id, parent_id, rel_type="derived_from", actor="system"):
        self.get_entity(child_id)
        self.get_entity(parent_id)
        self.conn.execute(
            "INSERT OR IGNORE INTO lineage(child_id,parent_id,rel_type,created_at) VALUES(?,?,?,?)",
            (child_id, parent_id, rel_type, utcnow()),
        )
        self.audit(actor, "lineage_add", None, child_id, {"parent": parent_id, "rel": rel_type})
        return {"child": child_id, "parent": parent_id, "rel": rel_type}

    def lineage_trace(self, eid, visited=None):
        """Return ancestry chain (parents, recursively)."""
        if visited is None:
            visited = set()
        if eid in visited:
            return []
        visited.add(eid)
        parents = self.conn.execute(
            "SELECT parent_id, rel_type FROM lineage WHERE child_id=?", (eid,)
        ).fetchall()
        chain = []
        for p in parents:
            chain.append({"id": p["parent_id"], "rel": p["rel_type"]})
            chain.extend(self.lineage_trace(p["parent_id"], visited))
        return chain


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ontology_enterprise", description="Enterprise Ontology Runtime")
    parser.add_argument("--root", default=None, help=f"ontology root dir (default: {DEFAULT_ROOT})")
    parser.add_argument("--actor", default="system", help="acting user")
    parser.add_argument("--role", default="admin", help="acting role")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("object", help="object CRUD")
    os_ = p.add_subparsers(dest="op")
    o_create = os_.add_parser("create"); o_create.add_argument("--type", required=True)
    o_create.add_argument("--props", required=True); o_create.add_argument("--source")
    o_create.add_argument("--effective-from"); o_create.add_argument("--effective-to")
    o_get = os_.add_parser("get"); o_get.add_argument("--id", required=True)
    o_update = os_.add_parser("update"); o_update.add_argument("--id", required=True); o_update.add_argument("--props", required=True)
    o_delete = os_.add_parser("delete"); o_delete.add_argument("--id", required=True); o_delete.add_argument("--hard", action="store_true")
    o_query = os_.add_parser("query"); o_query.add_argument("--type", required=True); o_query.add_argument("--where")
    o_alias = os_.add_parser("alias-add"); o_alias.add_argument("--id", required=True); o_alias.add_argument("--alias", required=True); o_alias.add_argument("--namespace", default="default")
    o_resolve = os_.add_parser("resolve"); o_resolve.add_argument("--alias", required=True); o_resolve.add_argument("--namespace", default="default")

    p = sub.add_parser("type", help="type definition")
    ts_ = p.add_subparsers(dest="op")
    t_def = ts_.add_parser("define"); t_def.add_argument("--name", required=True); t_def.add_argument("--definition", required=True)

    p = sub.add_parser("link", help="relation management")
    ls_ = p.add_subparsers(dest="op")
    l_rel = ls_.add_parser("relate"); l_rel.add_argument("--from", dest="from_id", required=True)
    l_rel.add_argument("--rel", required=True); l_rel.add_argument("--to", dest="to_id", required=True); l_rel.add_argument("--props")
    l_related = ls_.add_parser("related"); l_related.add_argument("--id", required=True); l_related.add_argument("--rel")
    l_related.add_argument("--direction", default="outgoing", choices=["outgoing", "incoming", "both"])

    p = sub.add_parser("state", help="state machine")
    ss_ = p.add_subparsers(dest="op")
    s_def = ss_.add_parser("define"); s_def.add_argument("--type", required=True); s_def.add_argument("--states", required=True)
    s_def.add_argument("--initial", required=True); s_def.add_argument("--allow", required=True)
    s_show = ss_.add_parser("show"); s_show.add_argument("--id", required=True)
    s_tr = ss_.add_parser("transition"); s_tr.add_argument("--id", required=True); s_tr.add_argument("--to", required=True)

    p = sub.add_parser("method", help="deterministic methods")
    ms_ = p.add_subparsers(dest="op")
    m_reg = ms_.add_parser("register"); m_reg.add_argument("--name", required=True); m_reg.add_argument("--code", required=True); m_reg.add_argument("--description")
    m_run = ms_.add_parser("run"); m_run.add_argument("--name", required=True); m_run.add_argument("--ctx", default="{}")

    p = sub.add_parser("action", help="governed actions")
    as_ = p.add_subparsers(dest="op")
    a_reg = as_.add_parser("register"); a_reg.add_argument("--name", required=True)
    a_reg.add_argument("--preconditions", default="{}"); a_reg.add_argument("--required-role")
    a_reg.add_argument("--risk", default="low", choices=["low", "medium", "high"]); a_reg.add_argument("--idempotent", action="store_true")
    a_reg.add_argument("--side-effect", help='e.g. {"field":"status","value":"approved"}')
    a_run = as_.add_parser("run"); a_run.add_argument("--name", required=True); a_run.add_argument("--id", dest="target_id", required=True)
    a_run.add_argument("--params", default="{}"); a_run.add_argument("--idempotency-key")

    p = sub.add_parser("policy", help="RBAC policies")
    ps_ = p.add_subparsers(dest="op")
    pol_add = ps_.add_parser("add"); pol_add.add_argument("--role", required=True); pol_add.add_argument("--resource", required=True)
    pol_add.add_argument("--action", required=True); pol_add.add_argument("--effect", default="allow")
    pol_check = ps_.add_parser("check"); pol_check.add_argument("--role", required=True); pol_check.add_argument("--resource", required=True)
    pol_check.add_argument("--action", required=True)

    p = sub.add_parser("audit", help="audit log")
    au_ = p.add_subparsers(dest="op")
    au_q = au_.add_parser("query"); au_q.add_argument("--actor"); au_q.add_argument("--target-id"); au_q.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("lineage", help="data lineage")
    lg_ = p.add_subparsers(dest="op")
    lg_add = lg_.add_parser("add"); lg_add.add_argument("--child", required=True); lg_add.add_argument("--parent", required=True); lg_add.add_argument("--rel", default="derived_from")
    lg_trace = lg_.add_parser("trace"); lg_trace.add_argument("--id", required=True)

    p = sub.add_parser("init", help="initialize default types + bootstrap policies")

    args = parser.parse_args(argv)
    db = OntologyDB(Path(args.root) if args.root else None)
    try:
        if args.cmd == "init":
            bootstrap(db)
            out({"ok": "initialized", "db": str(db.db_path)})
        elif args.cmd == "object":
            handle_object(db, args, args.op)
        elif args.cmd == "type":
            if args.op == "define":
                out(db.define_type(args.name, json.loads(args.definition), args.actor, args.role))
        elif args.cmd == "link":
            if args.op == "relate":
                out(db.relate(args.from_id, args.rel, args.to_id,
                              json.loads(args.props) if args.props else None, args.actor, args.role))
            elif args.op == "related":
                out(db.related(args.id, args.rel, args.direction))
        elif args.cmd == "state":
            if args.op == "define":
                states = args.states.split(",")
                transitions = []
                for pair in args.allow.split(","):
                    frm, to = pair.split(">")
                    transitions.append({"from": frm.strip(), "to": to.strip()})
                out(db.define_state_machine(args.type, states, args.initial, transitions, args.actor, args.role))
            elif args.op == "show":
                out({"id": args.id, "state": db.entity_state(args.id)})
            elif args.op == "transition":
                out(db.transition(args.id, args.to, args.actor, args.role))
        elif args.cmd == "method":
            if args.op == "register":
                out(db.method_register(args.name, args.code, args.description, args.actor, args.role))
            elif args.op == "run":
                out(db.method_run(args.name, json.loads(args.ctx), args.actor, args.role))
        elif args.cmd == "action":
            if args.op == "register":
                out(db.action_register(args.name, json.loads(args.preconditions), args.required_role,
                                       args.risk, args.idempotent,
                                       json.loads(args.side_effect) if args.side_effect else None,
                                       actor=args.actor, role=args.role))
            elif args.op == "run":
                out(db.action_run(args.name, args.target_id, args.actor, args.role,
                                  json.loads(args.params), args.idempotency_key))
        elif args.cmd == "policy":
            if args.op == "add":
                out(db.policy_add(args.role, args.resource, args.action, args.effect, args.actor, args.role))
            elif args.op == "check":
                out(db.policy_check(args.role, args.resource, args.action))
        elif args.cmd == "audit":
            if args.op == "query":
                out(db.audit_query(args.actor, None, args.target_id, args.limit))
        elif args.cmd == "lineage":
            if args.op == "add":
                out(db.lineage_add(args.child, args.parent, args.rel, args.actor))
            elif args.op == "trace":
                out(db.lineage_trace(args.id))
        else:
            parser.print_help()
        db.conn.commit()
    except OntologyError as e:
        db.conn.rollback()
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        db.conn.rollback()
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    finally:
        db.close()


def handle_object(db, args, op):
    if op == "create":
        out(db.create_entity(args.type, json.loads(args.props), args.actor, args.role,
                             args.effective_from, args.effective_to, args.source))
    elif op == "get":
        out(db.get_entity(args.id))
    elif op == "update":
        out(db.update_entity(args.id, json.loads(args.props), args.actor, args.role))
    elif op == "delete":
        out(db.delete_entity(args.id, args.actor, args.role, args.hard))
    elif op == "query":
        out(db.query_entities(args.type, json.loads(args.where) if args.where else None, args.actor, args.role))
    elif op == "alias-add":
        out(db.alias_add(args.id, args.alias, args.namespace, args.actor, args.role))
    elif op == "resolve":
        out(db.resolve_alias(args.alias, args.namespace))


def bootstrap(db):
    """Default enterprise types + bootstrap policies so the CLI is usable out of the box."""
    # bootstrap policies first (admin full) so type definition can pass policy checks
    admin_rules = [
        ("admin", "*", "read"), ("admin", "*", "write"), ("admin", "*", "delete"),
        ("admin", "*", "execute"), ("admin", "Policy", "write"), ("admin", "Method", "write"),
        ("admin", "Action", "write"), ("admin", "Action", "execute"), ("admin", "StateMachine", "write"),
        ("admin", "Type", "write"), ("admin", "Alias", "write"),
    ]
    for role, res, act in admin_rules:
        db.conn.execute(
            "INSERT OR REPLACE INTO policies(role,resource,action,effect) VALUES(?,?,?,?)",
            (role, res, act, "allow"),
        )
    viewer_rules = [("viewer", "*", "read")]
    for role, res, act in viewer_rules:
        db.conn.execute(
            "INSERT OR REPLACE INTO policies(role,resource,action,effect) VALUES(?,?,?,?)",
            (role, res, act, "allow"),
        )
    operator_rules = [
        ("operator", "*", "read"), ("operator", "*", "write"),
        ("operator", "Method", "read"), ("operator", "Method", "write"),
        ("operator", "Action", "execute"),
    ]
    for role, res, act in operator_rules:
        db.conn.execute(
            "INSERT OR REPLACE INTO policies(role,resource,action,effect) VALUES(?,?,?,?)",
            (role, res, act, "allow"),
        )
    manager_rules = [
        ("manager", "*", "read"), ("manager", "*", "write"),
        ("manager", "Action", "execute"),
        ("manager", "Method", "read"), ("manager", "Method", "write"),
    ]
    for role, res, act in manager_rules:
        db.conn.execute(
            "INSERT OR REPLACE INTO policies(role,resource,action,effect) VALUES(?,?,?,?)",
            (role, res, act, "allow"),
        )

    types = {
        "Person": {
            "schema": {
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "email": {"type": "string"},
                    "phone": {"type": "string"},
                    "department": {"type": "string"},
                },
            }
        },
        "Project": {
            "schema": {
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "status": {"type": "string", "enum": ["planning", "active", "paused", "completed", "archived"]},
                    "owner": {"type": "string"},
                },
            }
        },
        "Task": {
            "schema": {
                "required": ["title", "status"],
                "properties": {
                    "title": {"type": "string"},
                    "status": {"type": "string", "enum": ["open", "in_progress", "blocked", "done", "cancelled"]},
                    "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"]},
                    "assignee": {"type": "string"},
                    "due": {"type": "string"},
                },
            },
            "relations": {
                "has_owner": {"from_types": ["Project", "Task"], "to_types": ["Person"], "cardinality": "many_to_one"},
                "blocks": {"from_types": ["Task"], "to_types": ["Task"], "acyclic": True},
            },
        },
        "Event": {
            "schema": {
                "required": ["title", "start"],
                "properties": {
                    "title": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "attendees": {"type": "string"},
                },
            }
        },
        "Document": {
            "schema": {
                "required": ["title"],
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "path": {"type": "string"},
                    "summary": {"type": "string"},
                },
            }
        },
        "Metric": {
            "schema": {
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": "number"},
                    "dimension": {"type": "string"},
                    "version": {"type": "number"},
                    "effective_from": {"type": "string"},
                },
            }
        },
    }
    for name, definition in types.items():
        db.define_type(name, definition, "bootstrap", "admin")


if __name__ == "__main__":
    main()
