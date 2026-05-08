import json
import os


def run_ralph_result(payload: dict[str, object]) -> str:
    result = payload.get("result")
    if result not in {"completed", "incompleted", "needs_human"}:
        raise ValueError("invalid result")
    if result == "incompleted" and not payload.get("learnings"):
        raise ValueError("incompleted requires non-empty learnings")
    if result == "needs_human" and (
        not payload.get("blocker") or payload.get("human_task") is None
    ):
        raise ValueError("needs_human requires blocker and human_task")

    output_path = os.environ.get("JRI_RESULT_PATH")
    if not output_path:
        return "JRI_RESULT_PATH not set"

    result_payload = {"result": result}
    for key in ("summary", "learnings", "blocker", "human_task"):
        value = payload.get(key)
        if value:
            result_payload[key] = value

    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(result_payload, handle, indent=2)
        handle.write("\n")
    return f"Result recorded: {result}"
