"""Versioned declarative scene skills; no skill code is executed."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import tomllib

from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from jsonschema import Draft202012Validator

BUILTINS = Path(__file__).parent / "builtin_skills"


@dataclass(frozen=True)
class Skill:
    id: str
    version: str
    description: str
    parent: str | None
    enabled: bool
    prompt: str
    schema: dict
    template: str
    digest: str


def asset(directory: Path, name: str) -> Path:
    path = (directory / name).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError("Skill assets must remain inside their skill directory")
    return path


def merge_schema(parent: dict, child: dict) -> dict:
    result = json.loads(json.dumps(parent))
    for key, value in child.items():
        if key in {"properties", "$defs"}:
            target = result.setdefault(key, {})
            for name, definition in value.items():
                if name in target and target[name] != definition:
                    raise ValueError(f"Conflicting inherited schema field: {name}")
                target[name] = definition
        elif key == "required":
            result[key] = sorted(set(result.get(key, [])) | set(value))
        else:
            result[key] = value
    return result


class SkillRegistry:
    def __init__(self, custom_root: Path | None = None):
        self.entries = {}
        self.loaded = {}
        for root in (BUILTINS, custom_root):
            if root is None or not root.exists():
                continue
            seen = set()
            for manifest in sorted(root.rglob("skill.toml")):
                if not manifest.resolve().is_relative_to(root.resolve()):
                    raise ValueError("Skill manifest escapes its configured root")
                data = tomllib.loads(manifest.read_text(encoding="utf-8"))
                if data.get("protocol_version") != 1:
                    raise ValueError("Unsupported Skill protocol version")
                identifier = data["id"]
                if not re.fullmatch(r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*", identifier):
                    raise ValueError("Invalid skill ID")
                if identifier in seen:
                    raise ValueError(f"Duplicate skill ID: {identifier}")
                seen.add(identifier)
                self.entries[identifier] = (manifest.parent, data)
        for identifier in self.entries:
            self.get(identifier)

    def get(self, identifier: str, resolving: tuple = ()) -> Skill:
        if identifier in resolving:
            raise ValueError("Cyclic Skill inheritance")
        if identifier in self.loaded:
            return self.loaded[identifier]
        if identifier not in self.entries:
            raise ValueError(f"Unknown skill ID: {identifier}")
        directory, data = self.entries[identifier]
        parent_id = data.get("parent")
        parent = self.get(parent_id, (*resolving, identifier)) if parent_id else None
        prompt_path, schema_path, template_path = (asset(directory, name) for name in ("prompt.md", "schema.json", "template.md.j2"))
        local_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else ""
        local_schema = json.loads(schema_path.read_text(encoding="utf-8")) if schema_path.is_file() else {}
        schema = merge_schema(parent.schema if parent else {}, local_schema)
        Draft202012Validator.check_schema(schema)
        if schema.get("type") != "object":
            raise ValueError("Skill output schema must describe a JSON object")
        prompt = ((parent.prompt + "\n\n") if parent else "") + local_prompt
        template = template_path.read_text(encoding="utf-8") if template_path.is_file() else parent.template if parent else ""
        if not prompt or not template:
            raise ValueError("Skill requires a prompt and a Markdown template")
        digest = hashlib.sha256(json.dumps([data, prompt, schema, template], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        skill = Skill(identifier, str(data["version"]), data["description"], parent_id, data.get("enabled", True), prompt, schema, template, digest)
        self.loaded[identifier] = skill
        return skill

    def routes(self) -> list[dict]:
        return [{"id": skill.id, "parent": skill.parent, "description": skill.description}
                for skill in self.loaded.values() if skill.enabled]


def render_summary(skill: Skill, result: dict, metadata: dict) -> str:
    roles = {"candidate": "候选人", "interviewer": "面试官", "doctor": "医生", "patient": "患者",
             "courier": "快递员", "recipient": "收件人", "other": "其他", "unknown": "未知"}
    environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
    environment.globals.update(refs=lambda ids: "、".join(f"[{item}](sources.md#{item})" for item in ids), role_name=lambda role: roles.get(role, role))
    return environment.from_string(skill.template).render(result=result, metadata=metadata).strip() + "\n"
