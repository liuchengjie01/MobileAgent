"""Export compact DPO trajectories while preserving their directory layout."""

import argparse
import json
import re
import sys
from pathlib import Path


STEP_FIELDS = ("step", "step_id", "step_index", "step_num", "step_number")
CALL_FIELDS = (
    ("messages", "response"),
    ("action_messages", "action_response"),
    ("planning_messages", "planning_response"),
    ("reflect_messages", "reflect_response"),
)
RESPONSE_FIELDS = tuple(response for _, response in CALL_FIELDS)


def valid_prompt(messages) -> bool:
    """Match the trainer's prompt check, rejecting assistant-only histories."""
    return (
        isinstance(messages, list)
        and bool(messages)
        and all(
            isinstance(message, dict)
            and message.get("role")
            and "content" in message
            for message in messages
        )
        and any(message["role"] != "assistant" for message in messages)
    )


def compact_trajectory(trajectory: list) -> list:
    """Keep step IDs, actions, attack flags, responses and initial prompts.

    Single-call logs use messages/response; multi-call logs have separate
    action, planning and reflect fields. Each call keeps its own first valid
    prompt, skipping explicit empty responses (calls that did not run).
    Legacy logs without response fields still keep their first valid prompt.
    """
    result = []
    prompts_kept = set()
    for step in trajectory:
        if not isinstance(step, dict):
            # The trainer ignores non-dictionary entries; preserve list order.
            result.append(step)
            continue
        clean = {
            key: step[key]
            for key in (*STEP_FIELDS, "action", "is_attack", *RESPONSE_FIELDS)
            if key in step
        }
        for messages_key, response_key in CALL_FIELDS:
            if messages_key in prompts_kept:
                continue
            if response_key in step:
                response = step[response_key]
                if not isinstance(response, str) or not response.strip():
                    continue
            messages = step.get(messages_key)
            if valid_prompt(messages):
                clean[messages_key] = messages
                prompts_kept.add(messages_key)
        result.append(clean)
    return result

_NUM_JSON_RE = re.compile(r"^\d+\.json$")
_DEFENSE_PREFIX = "log_gui-owl-7b_defense_"

def _iter_target_logs(base_dir: Path):
    """Yield the trajectory log files we want.

    Supported layouts (relative to base_dir):
      - */*/*/*/log_gui-owl-7b_defense_*.json   (form 1)
      - */*/*/[digits].json                     (form 2)
    """
    seen = set()

    # Form 1: 4 directory levels, filename prefixed with log_gui-owl-7b_defense_
    for p in base_dir.glob("*/*/*/*/log_*_defense_-1.json"):
        if p.is_file():
            rp = p.resolve()
            if "_no_attack" in str(rp):
                continue
            if rp not in seen:
                seen.add(rp)
                yield p

    # Form 2: 3 directory levels, filename is just digits + .json
    for p in base_dir.glob("*/*/*/*.json"):
        if p.is_file() and _NUM_JSON_RE.match(p.name):
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                yield p
def copy_json_logs(input_path: Path, output_path: Path) -> int:
    """Export trajectory lists from supported log directory layouts.

    Existing destination files are overwritten. Non-trajectory JSON objects
    (such as task_result.json) are skipped. Source files are never modified.
    """
    input_path = Path(input_path).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()

    if not input_path.is_dir():
        raise ValueError(f"Input directory does not exist: {input_path}")
    if output_path == input_path or input_path in output_path.parents:
        raise ValueError("Output directory must not be inside the input directory.")
    if output_path.exists() and not output_path.is_dir():
        raise ValueError(f"Output path is not a directory: {output_path}")

    output_path.mkdir(parents=True, exist_ok=True)
    copied = 0
    skipped = 0

    for source in _iter_target_logs(input_path):
        if not source.is_file():
            continue
        try:
            with source.open("r", encoding="utf-8-sig") as file:
                trajectory = json.load(file)
        except (ValueError, OSError) as exc:
            raise ValueError(f"Failed to read {source}: {exc}") from exc
        if not isinstance(trajectory, list):
            skipped += 1
            continue
        compact = compact_trajectory(trajectory)
        destination = output_path / source.relative_to(input_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding="utf-8") as file:
            json.dump(compact, file, ensure_ascii=False, separators=(",", ":"), indent=2)
        copied += 1

    if skipped:
        print(f"Skipped {skipped} non-trajectory JSON files.", file=sys.stderr)
    return copied


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export compact JSON trajectories from model/task/timestamp "
        "or model/category/task/timestamp directories. Keep step IDs, action, "
        "is_attack, all response fields and the first valid messages list for "
        "each call (single-call or action/planning/reflect). "
        "Existing files are overwritten."
    )
    parser.add_argument("--input-path", "--input_path", required=True, type=Path,
                        help="Input root directory")
    parser.add_argument("--output-path", "--output_path", required=True, type=Path,
                        help="Output root directory")
    args = parser.parse_args()
    try:
        copied = copy_json_logs(args.input_path, args.output_path)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    print(f"Exported {copied} compact trajectories to {args.output_path.expanduser().resolve()}")


if __name__ == "__main__":
    main()
