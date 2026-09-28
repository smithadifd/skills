#!/usr/bin/env python3
"""Resolve kickoff's output directory and optional references."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOPIC_RESOLVER_PATH = HERE.parent / "topic" / "topics_dir.py"
FALLBACK_TOPICS_DIR = "~/.claude/topics"
FALLBACK_MODEL_ROUTING_STANDARD = "~/.claude/standards/model-routing.md"


@dataclass(frozen=True)
class KickoffPaths:
    topics_dir: Path
    topic_template: Path
    model_routing_standard: Path

    def topic_path(self, slug: str) -> Path:
        return self.topics_dir / f"{slug}.md"


def _load_topic_resolver():
    spec = importlib.util.spec_from_file_location(
        "kickoff_topic_topics_dir", TOPIC_RESOLVER_PATH
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load topic resolver at {TOPIC_RESOLVER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def resolve() -> KickoffPaths:
    if not TOPIC_RESOLVER_PATH.is_file():
        topics_dir = Path(FALLBACK_TOPICS_DIR).expanduser()
        return KickoffPaths(
            topics_dir=topics_dir,
            topic_template=topics_dir / "TEMPLATE.md",
            model_routing_standard=Path(
                FALLBACK_MODEL_ROUTING_STANDARD
            ).expanduser(),
        )

    topic_resolver = _load_topic_resolver()
    topics_dir = topic_resolver.resolve()
    normative = topic_resolver.normative_paths(topics_dir)
    return KickoffPaths(
        topics_dir=topics_dir,
        topic_template=normative["template"],
        model_routing_standard=normative["model_routing"],
    )


def optional_read_notices(paths: KickoffPaths) -> list[str]:
    notices = []
    if not TOPIC_RESOLVER_PATH.is_file():
        notices.append(
            "kickoff: topic resolver not found at "
            f"{TOPIC_RESOLVER_PATH}; using ~/.claude/topics"
        )
    if not paths.topic_template.is_file():
        notices.append(
            "kickoff: optional topic template not found at "
            f"{paths.topic_template}; using the built-in hand-off shape in SKILL.md"
        )
    if not paths.model_routing_standard.is_file():
        notices.append(
            "kickoff: optional model-routing standard not found at "
            f"{paths.model_routing_standard}; using the current session model with no "
            "model-specific consult"
        )
    return notices


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug")
    args = parser.parse_args(argv)

    paths = resolve()
    for notice in optional_read_notices(paths):
        print(notice, file=sys.stderr)

    payload = {
        "topics_dir": str(paths.topics_dir),
        "topic_template": str(paths.topic_template),
        "model_routing_standard": str(paths.model_routing_standard),
    }
    if args.slug:
        payload["topic_path"] = str(paths.topic_path(args.slug))
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
