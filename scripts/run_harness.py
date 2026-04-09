#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def main() -> int:
    args = parse_args()
    skill_dir = Path(__file__).resolve().parent.parent
    cases_path = skill_dir / "references" / "harness-cases.json"
    cases_config = json.loads(cases_path.read_text(encoding="utf-8"))

    selected_label = args.case
    cases = [
        case
        for case in cases_config["cases"]
        if not selected_label or case["label"] == selected_label
    ]
    if not cases:
        print("找不到指定的 harness case。", file=sys.stderr)
        return 1

    output_root = Path(
        cases_config["outputRoot"].replace("${HOME}", str(Path.home()))
    ).expanduser()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = output_root / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    script_path = skill_dir / "scripts" / "x_article_reader.py"

    for case in cases:
        case_dir = output_dir / case["label"]
        case_dir.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, str(script_path), *case["args"]]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        )

        (case_dir / "stdout.txt").write_text(completed.stdout, encoding="utf-8")
        (case_dir / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
        (case_dir / "command.json").write_text(
            json.dumps(command, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        result = evaluate_case(case, completed)
        (case_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        results.append(result)

    overall_status = "pass" if all(item["status"] == "pass" for item in results) else "fail"
    report = {
        "runId": run_id,
        "createdAt": datetime.now().isoformat(),
        "outputDir": str(output_dir),
        "overallStatus": overall_status,
        "results": results,
    }

    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "report.md").write_text(render_report(report), encoding="utf-8")

    prune_old_runs(output_root, int(cases_config.get("keepRuns", 10)))

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_console(report))

    return 0 if overall_status == "pass" else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the x-article-reader live harness.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--case", help="Run only one case label")
    return parser.parse_args()


def evaluate_case(case: dict[str, Any], completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    errors = []
    parsed = None

    if completed.returncode != 0:
        errors.append(f"腳本退出碼不是 0，而是 {completed.returncode}")
    else:
        try:
            parsed = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            errors.append(f"輸出不是有效 JSON：{exc}")

    if parsed is not None:
        expect = case["expect"]
        article = parsed.get("article") or {}
        extraction = article.get("extraction") or {}
        title = (article.get("title") or "").strip()
        preview = article.get("previewText") or ""
        body = article.get("body") or ""
        warnings = parsed.get("warnings") or []

        expected_title = expect.get("articleTitle")
        if expected_title and expected_title not in title:
            errors.append(f"文章標題不符，實際是：{title}")

        min_preview = expect.get("previewMinChars")
        if min_preview and len(preview) < min_preview:
            errors.append(f"摘要太短，只有 {len(preview)} 字元")

        min_body = expect.get("bodyMinChars")
        if min_body and len(body) < min_body:
            errors.append(f"正文太短，只有 {len(body)} 字元")

        if expect.get("bodyAbsent") and body:
            errors.append("本案例預期沒有正文，但實際抓到了正文")

        expected_method = expect.get("extractionMethod")
        if expected_method and extraction.get("method") != expected_method:
            errors.append(
                f"提取方法不符，預期 {expected_method}，實際 {extraction.get('method')}"
            )

        warning_contains = expect.get("warningContains")
        if warning_contains and not any(warning_contains in item for item in warnings):
            errors.append(f"找不到預期警告內容：{warning_contains}")

    return {
        "label": case["label"],
        "status": "pass" if not errors else "fail",
        "returncode": completed.returncode,
        "errors": errors,
    }


def render_report(report: dict[str, Any]) -> str:
    lines = [
        "# x-article-reader Harness Report",
        "",
        f"- Run ID: {report['runId']}",
        f"- 建立時間: {report['createdAt']}",
        f"- 整體狀態: {report['overallStatus']}",
        "",
        "## Cases",
    ]
    for result in report["results"]:
        lines.append(f"- {result['label']}: {result['status']}")
        for error in result["errors"]:
            lines.append(f"  - {error}")
    return "\n".join(lines) + "\n"


def render_console(report: dict[str, Any]) -> str:
    lines = [
        f"run: {report['runId']}",
        f"status: {report['overallStatus']}",
    ]
    for result in report["results"]:
        lines.append(f"- {result['label']}: {result['status']}")
        for error in result["errors"]:
            lines.append(f"  error: {error}")
    return "\n".join(lines)


def prune_old_runs(output_root: Path, keep_runs: int) -> None:
    if keep_runs <= 0 or not output_root.exists():
        return
    runs = sorted(
        [item for item in output_root.iterdir() if item.is_dir()],
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for old_run in runs[keep_runs:]:
        shutil.rmtree(old_run, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
