#!/usr/bin/env python3
"""
Filter terminal recordings based on various criteria.

This script filters recordings and saves the results with important information
(title, description, commands, etc.) to output/filtered_recordings.json.

Supported filters:
- Duration (min/max)
- Command count (min/max)
- Command density (min/max commands per minute)
- TUI status (CLI_ONLY or CONTAINS_TUI)
- External URL group (accessible_repo / accessible_no_repo / no_accessible)
- Value Score (min total score and per-dimension thresholds)

Usage:
    python filter_recordings.py --min-duration 60 --max-duration 300 --min-commands 5
    python filter_recordings.py --tui CLI_ONLY --url-accessible true
    python filter_recordings.py --url-is-repo true
    python filter_recordings.py --min-score 7 --sort-by-score
    python filter_recordings.py --min-score 7 --min-task-complexity 2 --min-context-level 1
"""

import argparse
import json
from pathlib import Path
from typing import Optional


EXTERNAL_URL_DETECTION_FILE = Path("output") / "external_url_detection.json"
LEGACY_SOURCE_CODE_DETECTION_FILE = Path("output") / "source_code_detection.json"


def get_all_recording_ids(recordings_dir: str = "../data/recordings") -> list[str]:
    recordings_path = Path(recordings_dir)

    if not recordings_path.exists():
        raise FileNotFoundError(f"Recordings directory not found: {recordings_dir}")

    recording_ids = [
        d.name for d in recordings_path.iterdir()
        if d.is_dir() and d.name.isdigit()
    ]
    recording_ids.sort(key=int)
    return recording_ids


def load_json(filepath: Path) -> dict:
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_external_url_data() -> dict:
    """Load the external URL detection index with a legacy fallback."""
    if EXTERNAL_URL_DETECTION_FILE.exists():
        return load_json(EXTERNAL_URL_DETECTION_FILE)
    if LEGACY_SOURCE_CODE_DETECTION_FILE.exists():
        return load_json(LEGACY_SOURCE_CODE_DETECTION_FILE)
    raise FileNotFoundError(
        f"Neither {EXTERNAL_URL_DETECTION_FILE} nor {LEGACY_SOURCE_CODE_DETECTION_FILE} exists"
    )



def filter_by_duration(
    recording_ids: list[str],
    min_duration: Optional[float] = None,
    max_duration: Optional[float] = None
) -> list[str]:
    duration_file = Path("output") / "duration_analysis.json"
    durations = load_json(duration_file)

    filtered = []
    for rec_id in recording_ids:
        if rec_id not in durations:
            continue
        duration = durations[rec_id]
        if duration is None:
            continue
        if min_duration is not None and duration < min_duration:
            continue
        if max_duration is not None and duration > max_duration:
            continue
        filtered.append(rec_id)

    return filtered


def filter_by_command_count(
    recording_ids: list[str],
    min_commands: Optional[int] = None,
    max_commands: Optional[int] = None
) -> list[str]:
    command_file = Path("output") / "command_detection.json"
    commands = load_json(command_file)

    filtered = []
    for rec_id in recording_ids:
        if rec_id not in commands:
            continue
        cmd_list = commands[rec_id]
        if cmd_list is None:
            continue
        cmd_count = len(cmd_list)
        if min_commands is not None and cmd_count < min_commands:
            continue
        if max_commands is not None and cmd_count > max_commands:
            continue
        filtered.append(rec_id)

    return filtered


def filter_by_command_density(
    recording_ids: list[str],
    min_density: Optional[float] = None,
    max_density: Optional[float] = None
) -> list[str]:
    duration_file = Path("output") / "duration_analysis.json"
    command_file = Path("output") / "command_detection.json"

    durations = load_json(duration_file)
    commands = load_json(command_file)

    filtered = []
    for rec_id in recording_ids:
        if rec_id not in durations or rec_id not in commands:
            continue
        duration = durations[rec_id]
        cmd_list = commands[rec_id]
        if duration is None or cmd_list is None or duration <= 0:
            continue
        density = (len(cmd_list) / duration) * 60
        if min_density is not None and density < min_density:
            continue
        if max_density is not None and density > max_density:
            continue
        filtered.append(rec_id)

    return filtered


def filter_by_pii(
    recording_ids: list[str],
    keep_clean_only: bool = True,
) -> list[str]:
    """
    Filter recordings based on PII/credential/malicious-command detection results.

    Args:
        recording_ids:   List of recording IDs to filter.
        keep_clean_only: If True (default), keep only CLEAN recordings.
                         If False, keep only FLAGGED recordings (for inspection).

    Returns:
        List of recording IDs that pass the filter.
    """
    pii_file = Path("output") / "pii_detection.json"
    if not pii_file.exists():
        print(f"Warning: {pii_file} not found. Skipping PII filter.")
        return recording_ids

    pii_data = load_json(pii_file)

    filtered = []
    for rec_id in recording_ids:
        if rec_id not in pii_data:
            continue
        is_clean = pii_data[rec_id].get("label") == "CLEAN"
        if keep_clean_only == is_clean:
            filtered.append(rec_id)

    return filtered


def filter_by_tui(
    recording_ids: list[str],
    tui_status: str = 'CLI_ONLY'
) -> list[str]:
    tui_file = Path("output") / "tui_classification.json"
    tui_classes = load_json(tui_file)

    filtered = []
    for rec_id in recording_ids:
        if rec_id not in tui_classes:
            continue
        tui_class = tui_classes[rec_id]
        if tui_class not in ['CLI_ONLY', 'CONTAINS_TUI']:
            continue
        if tui_class == tui_status:
            filtered.append(rec_id)

    return filtered


def filter_by_external_urls(
    recording_ids: list[str],
    has_urls: Optional[bool] = None,
    url_group: Optional[str] = None,
) -> list[str]:
    """
    Filter recordings by external URL properties.

    Args:
        recording_ids: List of recording IDs to filter
        has_urls: If set, keep recordings that have (True) or don't have (False) any external URLs
        url_group: If set, keep recordings belonging to the specified URL group:
            - "accessible_repo":    has at least one accessible repo URL
            - "accessible_no_repo": has accessible URL(s) but none are repos
            - "no_accessible":      has no accessible URLs at all

    Returns:
        List of recording IDs that pass the filter.
    """
    data = load_external_url_data()

    filtered = []
    for rec_id in recording_ids:
        if rec_id not in data:
            continue

        url_list = data[rec_id]

        if has_urls is not None:
            if (len(url_list) > 0) != has_urls:
                continue

        if url_group is not None:
            urls = [e for e in url_list if isinstance(e, dict)]
            has_accessible_repo = any(e.get("accessible") is True and e.get("is_repo") is True for e in urls)
            has_accessible = any(e.get("accessible") is True for e in urls)

            if url_group == "accessible_repo":
                if not has_accessible_repo:
                    continue
            elif url_group == "accessible_no_repo":
                if not (has_accessible and not has_accessible_repo):
                    continue
            elif url_group == "no_accessible":
                if has_accessible:
                    continue

        filtered.append(rec_id)

    return filtered


def filter_by_score(
    recording_ids: list[str],
    min_score: Optional[int] = None,
    min_state_action_alignment: Optional[int] = None,
    min_task_complexity: Optional[int] = None,
    min_signal_clarity: Optional[int] = None,
    min_context_level: Optional[int] = None,
) -> list[str]:
    score_file = Path("output") / "recording_value_scores.json"
    if not score_file.exists():
        print(f"Warning: {score_file} not found. Skipping score filter.")
        return recording_ids

    scores_data = load_json(score_file)

    filtered = []
    for rec_id in recording_ids:
        if rec_id not in scores_data:
            continue

        score_info = scores_data[rec_id]
        llm_score = score_info.get("llm_score") or {}

        total_score = score_info.get("total_score", 0)
        state_action_alignment = llm_score.get("state_action_alignment", 0)
        task_complexity = llm_score.get("task_complexity", 0)
        signal_clarity = llm_score.get("signal_clarity", 0)
        context_level = score_info.get("context_level", 0)

        if min_score is not None and total_score < min_score:
            continue
        if min_state_action_alignment is not None and state_action_alignment < min_state_action_alignment:
            continue
        if min_task_complexity is not None and task_complexity < min_task_complexity:
            continue
        if min_signal_clarity is not None and signal_clarity < min_signal_clarity:
            continue
        if min_context_level is not None and context_level < min_context_level:
            continue

        filtered.append(rec_id)

    return filtered


def sort_by_score(recording_ids: list[str]) -> list[str]:
    score_file = Path("output") / "recording_value_scores.json"
    if not score_file.exists():
        return recording_ids

    scores_data = load_json(score_file)

    def get_score(rid):
        if rid in scores_data:
            return scores_data[rid].get("total_score", -1)
        return -1

    return sorted(recording_ids, key=lambda rid: (get_score(rid), -int(rid)), reverse=True)


def collect_recording_info(
    recording_ids: list[str],
    recordings_dir: str = "../data/recordings"
) -> dict[str, dict]:
    recordings_path = Path(recordings_dir)
    command_file = Path("output") / "command_detection.json"
    duration_file = Path("output") / "duration_analysis.json"
    tui_file = Path("output") / "tui_classification.json"
    score_file = Path("output") / "recording_value_scores.json"

    commands_data = load_json(command_file) if command_file.exists() else {}
    durations_data = load_json(duration_file) if duration_file.exists() else {}
    tui_data = load_json(tui_file) if tui_file.exists() else {}
    scores_data = load_json(score_file) if score_file.exists() else {}

    result = {}

    for rec_id in recording_ids:
        rec_dir = recordings_path / rec_id
        info_file = rec_dir / "info.json"
        score_info = scores_data.get(rec_id, {})
        llm_score = score_info.get("llm_score") or {}

        info = load_json(info_file) if info_file.exists() else {}

        result[rec_id] = {
            "id": rec_id,
            "title": info.get("title", ""),
            "description": info.get("description", ""),
            "commands": commands_data.get(rec_id, []),
            "duration": durations_data.get(rec_id),
            "environment": info.get("environment", ""),
            "tui_status": tui_data.get(rec_id),
            "href": info.get("href", ""),
            "value_score": score_info.get("total_score"),
            "decision": score_info.get("decision"),
            "state_action_alignment": llm_score.get("state_action_alignment"),
            "task_complexity": llm_score.get("task_complexity"),
            "signal_clarity": llm_score.get("signal_clarity"),
            "context_level": score_info.get("context_level"),
        }

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Filter terminal recordings based on various criteria",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python filter_recordings.py --min-duration 60 --max-duration 300 --min-commands 5
  python filter_recordings.py --pii-clean --tui CLI_ONLY --url-group accessible_repo
  python filter_recordings.py --url-group accessible_no_repo
  python filter_recordings.py --url-group no_accessible
  python filter_recordings.py --min-score 7 --sort-by-score
        """
    )

    parser.add_argument("--pii-clean", action="store_true",
                        help="Exclude recordings flagged for PII, credentials, or malicious commands")
    parser.add_argument("--min-duration", type=float, default=None)
    parser.add_argument("--max-duration", type=float, default=None)
    parser.add_argument("--min-commands", type=int, default=None)
    parser.add_argument("--max-commands", type=int, default=None)
    parser.add_argument("--min-density", type=float, default=None)
    parser.add_argument("--max-density", type=float, default=None)
    parser.add_argument("--tui", type=str, choices=["CLI_ONLY", "CONTAINS_TUI"], default=None)
    parser.add_argument("--has-external-urls", dest="has_urls", choices=["true", "false"], default=None,
                        help="true: has any external URLs; false: no external URLs")
    parser.add_argument("--url-group", dest="url_group",
                        choices=["accessible_repo", "accessible_no_repo", "no_accessible"], default=None,
                        help="accessible_repo: has accessible repo URL; accessible_no_repo: has accessible URL but no repo; no_accessible: no accessible URLs")
    parser.add_argument("--min-score", type=int, default=None)
    parser.add_argument("--min-state-action-alignment", type=int, default=None)
    parser.add_argument("--min-task-complexity", type=int, default=None)
    parser.add_argument("--min-signal-clarity", type=int, default=None)
    parser.add_argument("--min-context-level", type=int, default=None)
    parser.add_argument("--sort-by-score", action="store_true")
    parser.add_argument("--output", type=str, default="output/filtered_recordings.json")
    parser.add_argument("--recordings-dir", type=str,
                        default=str(Path(__file__).parent.parent / "data" / "recordings"))

    args = parser.parse_args()

    print("Loading all recording IDs...")
    all_ids = get_all_recording_ids(args.recordings_dir)
    print(f"Total recordings: {len(all_ids)}")

    print("\nApplying filters...")
    filtered_ids = all_ids

    if args.pii_clean:
        print("Applying PII/credential/malicious filter (keep CLEAN only)...")
        filtered_ids = filter_by_pii(filtered_ids, keep_clean_only=True)
        print(f"  After PII filter: {len(filtered_ids)} recordings")

    if args.min_duration is not None or args.max_duration is not None:
        print(f"Applying duration filter (min={args.min_duration}, max={args.max_duration})...")
        filtered_ids = filter_by_duration(filtered_ids, args.min_duration, args.max_duration)
        print(f"  After duration filter: {len(filtered_ids)} recordings")

    if args.min_commands is not None or args.max_commands is not None:
        print(f"Applying command count filter (min={args.min_commands}, max={args.max_commands})...")
        filtered_ids = filter_by_command_count(filtered_ids, args.min_commands, args.max_commands)
        print(f"  After command count filter: {len(filtered_ids)} recordings")

    if args.min_density is not None or args.max_density is not None:
        print(f"Applying command density filter (min={args.min_density}, max={args.max_density})...")
        filtered_ids = filter_by_command_density(filtered_ids, args.min_density, args.max_density)
        print(f"  After command density filter: {len(filtered_ids)} recordings")

    if args.tui is not None:
        print(f"Applying TUI filter (status={args.tui})...")
        filtered_ids = filter_by_tui(filtered_ids, args.tui)
        print(f"  After TUI filter: {len(filtered_ids)} recordings")

    if args.has_urls is not None or args.url_group is not None:
        has_urls = None if args.has_urls is None else (args.has_urls == "true")
        print(f"Applying external URL filter (has_urls={has_urls}, url_group={args.url_group})...")
        filtered_ids = filter_by_external_urls(filtered_ids, has_urls=has_urls, url_group=args.url_group)
        print(f"  After external URL filter: {len(filtered_ids)} recordings")

    if any(v is not None for v in [
        args.min_score, args.min_state_action_alignment, args.min_task_complexity,
        args.min_signal_clarity, args.min_context_level,
    ]):
        print(f"Applying score filter (min_score={args.min_score}, min_state_action_alignment={args.min_state_action_alignment}, min_task_complexity={args.min_task_complexity}, min_signal_clarity={args.min_signal_clarity}, min_context_level={args.min_context_level})...")
        filtered_ids = filter_by_score(
            filtered_ids,
            min_score=args.min_score,
            min_state_action_alignment=args.min_state_action_alignment,
            min_task_complexity=args.min_task_complexity,
            min_signal_clarity=args.min_signal_clarity,
            min_context_level=args.min_context_level,
        )
        print(f"  After score filter: {len(filtered_ids)} recordings")

    if args.sort_by_score:
        print("Sorting recordings by value score (descending)...")
        filtered_ids = sort_by_score(filtered_ids)

    print(f"\nTotal filtered recordings: {len(filtered_ids)}")

    print("\nCollecting detailed information...")
    recordings_info = collect_recording_info(filtered_ids, args.recordings_dir)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(recordings_info, f, indent=2, ensure_ascii=False)

    print(f"\nResults saved to: {args.output}")
    print(f"Total filtered recordings: {len(recordings_info)}")


if __name__ == "__main__":
    main()
