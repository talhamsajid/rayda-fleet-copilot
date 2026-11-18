from __future__ import annotations

from functools import lru_cache
from pathlib import Path

SKILLS_DIR = Path(__file__).parent / "skills"


def _parse(path: Path) -> dict:
    _, front, body = path.read_text().split("---", 2)
    meta = dict(line.split(":", 1) for line in front.strip().splitlines())
    return {k.strip(): v.strip() for k, v in meta.items()} | {"body": body.strip()}


@lru_cache(maxsize=1)
def all_skills() -> dict[str, dict]:
    skills = (_parse(p) for p in sorted(SKILLS_DIR.glob("*/SKILL.md")))
    return {s["name"]: s for s in skills}


def index() -> str:
    return "\n".join(f"- {s['name']}: {s['description']} (for {s['applies_to']})" for s in all_skills().values())
