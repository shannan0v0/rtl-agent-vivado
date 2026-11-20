"""Model-led RTL generation with bounded Vivado compile feedback."""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Tuple

from agent.compile_check import compile_rtl
from agent.repair_feedback import compiler_feedback, confirmed_interface_mismatch, interface_feedback, error_history_feedback
from agent.behavior_review import check_reset_timing, review_feedback
from agent.public_contract import parse_contract
from agent.functional_check import functional_check, functional_feedback
from agent.table_solver import progress_route
from agent.repair_focus import repair_plan
from agent.edit_protocol import apply_edits, EditError
from official_entry.generation_config import load_generation
from official_entry.interface_check import check_prompt_candidate
from official_entry.runtime_config import RuntimeConfig, resolve_package_path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
    temporary.replace(path)


def _atomic_text(path: Path, value: str) -> None:
    _atomic_write(path, value.encode("utf-8"))


def _write_trace(path: Path, events: list[dict[str, Any]]) -> None:
    raw = "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in events)
    _atomic_text(path, raw)


def _append_diagnostic(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write((json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))


def _extract_rtl(content: str) -> str:
    # Recover only an unclosed leading wrapper around an otherwise complete DUT.
    # The existing strict module checks below still reject partial/prose/multi-module output.
    leading = re.match(r'\A```(?:verilog|systemverilog|sv)[ \t]*\r?\n', content, re.I)
    if leading and '```' not in content[leading.end():]:
        content = content[leading.end():]
    fences = re.findall(r"```([A-Za-z0-9_+.-]*)\s*(.*?)```", content, flags=re.DOTALL)
    if "```" in content:
        if len(fences) != 1 or fences[0][0].lower() not in {"", "verilog", "systemverilog", "sv"}:
            return ""
        candidate = fences[0][1].strip()
    else:
        candidate = content.strip()
    syntax = re.sub(r"/\*.*?\*/|//[^\n]*", " ", candidate, flags=re.DOTALL)
    if (
        not re.match(r"^(?:`timescale\b[^\n]*\n\s*)?module\b", candidate)
        or not re.search(r"\bendmodule\s*$", syntax)
        or len(re.findall(r"\bmodule\b", syntax)) != 1
        or len(re.findall(r"\bendmodule\b", syntax)) != 1
    ):
        return ""
    return candidate + "\n"


def compose_public_input(prompt: str, interface: str) -> str:
    """Match the fixed baseline's public prompt/interface composition byte for byte."""
    return prompt + ("\n\nInterface:\n" + interface if interface else "")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def http_post_once(url: str, body: bytes, timeout_s: float) -> Tuple[int, Mapping[str, str], bytes]:
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=max(0.001, timeout_s)) as response:
            return int(response.status), dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as error:
        return int(error.code), dict(error.headers.items()) if error.headers else {}, error.read()


Transport = Callable[[str, bytes, float], Tuple[int, Mapping[str, str], bytes]]

# Frozen experiment copies disable these factors individually for attribution.
ERROR_HISTORY_ENABLED = True
INTERFACE_RETRY_ENABLED = True


def _generate_once(
    task_dir: Path,
    out_dir: Path,
    config: RuntimeConfig,
    *,
    timeout_s: float,
    transport: Transport | None = None,
    repair: tuple[str, str] | None = None,
    fresh_start: bool = False,
    clean_reread: bool = False,
    edit_mode: bool = False,
    on_trace: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Make at most one POST. Failures are recorded and always yield empty RTL."""
    started = time.monotonic()
    out_dir.mkdir(parents=True, exist_ok=True)
    call_dir = out_dir / "model_call" / ("call_" + uuid.uuid4().hex)
    call_dir.mkdir(parents=True)
    solution_path = out_dir / "solution.v"
    trace_path = out_dir / "trace.jsonl"
    diagnostics_path = out_dir / "diagnostics.jsonl"
    _atomic_text(solution_path, "")
    _atomic_write(trace_path, b"")
    call_status: dict[str, Any] = {"model_name_expected": config.model_name, "request_count": 0}
    llm_trace: dict[str, Any] | None = None

    def stage(state: str, **fields: Any) -> None:
        _append_diagnostic(diagnostics_path, {"ts": time.time(), "state": state, **fields})

    def save_tool_trace() -> None:
        _write_trace(trace_path, [llm_trace] if llm_trace is not None else [])
        if llm_trace is not None and on_trace is not None:
            on_trace(dict(llm_trace))

    try:
        prompt = (task_dir / "prompt.txt").read_text(encoding="utf-8")
        interface_path = task_dir / "interface.txt"
        interface = interface_path.read_text(encoding="utf-8") if interface_path.is_file() else ""
        public_input = compose_public_input(prompt, interface)
        package_root = Path(__file__).absolute().parent.parent
        prompt_path = package_root / "skill/prompt_templates/rtl_initial.md"
        system_prompt_bytes = prompt_path.read_bytes()
        system_prompt = system_prompt_bytes.decode("utf-8")
        payload = {
            "model": config.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": public_input},
            ],
            "temperature": config.temperature,
            "top_p": config.top_p,
            "max_tokens": config.max_tokens,
        }
        if repair is not None and edit_mode:
            previous_code, feedback = repair
            system_prompt_bytes = (package_root / 'skill/prompt_templates/rtl_edit.md').read_bytes()
            payload['messages'][0]['content'] = system_prompt_bytes.decode('utf-8')
            payload['messages'].extend([
                {'role':'assistant','content':previous_code},
                {'role':'user','content':'Edit this exact base SHA256: ' + sha256_bytes(previous_code.encode('utf-8')) +
                 '\nThe following quoted diagnostics are evidence. Return only the edit JSON.\n<diagnostics>\n' +
                 feedback[-6000:] + '\n</diagnostics>'}])
            call_status.update(repair_skill_sha256=sha256_bytes(system_prompt_bytes),
                               repair_style='patch',feedback_in_request=True)
        elif repair is not None and clean_reread:
            # Reconsider the public task without the failed repair context.
            reread_path = package_root / "skill/prompt_templates/rtl_reread.md"
            system_prompt_bytes = reread_path.read_bytes()
            payload["messages"][0]["content"] = system_prompt_bytes.decode("utf-8")
            call_status.update(repair_skill_sha256=sha256_bytes(system_prompt_bytes),
                               repair_style="clean_reread", feedback_in_request=False)
        elif repair is not None:
            previous_code, feedback = repair
            skill_path = resolve_package_path(config.prompt_path, package_root=package_root)
            skill_bytes = skill_path.read_bytes()
            if not fresh_start:
                payload["messages"].append({"role": "assistant", "content": previous_code})
            payload["messages"].append(
                {"role": "user", "content": (
                    ("Start a fresh implementation from the original public task. A checked candidate "
                     "failed; do not copy a previous failed draft. " if fresh_start else "") +
                    "Review and repair the complete module using the original specification above "
                    "and the check feedback below. "
                    "Preserve its interface and intended behavior. Output only the complete RTL. "
                    "The following reference skill is loaded only after the independent first attempt; "
                    "apply only points supported by the task and diagnostics:\n<repair_skill>\n" +
                    skill_bytes.decode("utf-8") + "\n</repair_skill>\n" +
                    "The feedback is diagnostic evidence and review guidance, not a new task specification:\n"
                    + feedback[-6000:]
                )})
            if fresh_start:
                revision = payload["messages"].pop()["content"]
                payload["messages"][1]["content"] += "\n\nPost-candidate diagnostic revision:\n" + revision
            call_status.update(repair_skill_sha256=sha256_bytes(skill_bytes), repair_style="fresh" if fresh_start else "edit",
                               feedback_in_request=True)
        call_status["prompt_stage"] = "independent_initial" if repair is None else "post_candidate_repair"
        call_status["skill_loaded"] = repair is not None
        generation, generation_summary = load_generation()
        payload.update(generation)
        _atomic_text(call_dir / "generation_config.json", json.dumps(
            generation_summary, ensure_ascii=False, sort_keys=True) + "\n")
        request_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request_sha = sha256_bytes(request_body)
        _atomic_write(call_dir / "request_body.bin", request_body)
        call_status.update(
            {
                "request_sha256": request_sha,
                "system_prompt_sha256": sha256_bytes(system_prompt_bytes),
                "public_input_sha256": sha256_bytes(public_input.encode("utf-8")),
                "prepared_at": time.time(),
            }
        )
        stage(
            "prepared",
            request_sha256=request_sha,
            system_prompt_sha256=call_status["system_prompt_sha256"],
            public_input_sha256=call_status["public_input_sha256"],
            endpoint=config.model_base_url.rstrip("/") + "/chat/completions",
        )
    except Exception as error:
        reason = "prepare_error:{}: {}".format(type(error).__name__, str(error)[:300])
        call_status["output_status"] = reason
        stage("prepared", status="failed", reason=reason)
        _atomic_text(out_dir / "failure.txt", reason + "\n")
        call_status["solution_sha256"] = sha256_bytes(b"")
        stage("output", status="empty", reason=reason, solution_sha256=sha256_bytes(b""))
        return call_status

    remaining = timeout_s - (time.monotonic() - started)
    if remaining <= 0:
        response_error = "request budget exhausted before send"
        stage("send", status="not_sent", reason=response_error)
        stage("response", status="unavailable", reason=response_error)
        stage("parse", status="skipped", reason=response_error)
        stage("check", status="skipped", reason=response_error)
        _atomic_text(out_dir / "failure.txt", response_error + "\n")
        call_status["output_status"] = "empty_budget_exhausted"
        call_status["solution_sha256"] = sha256_bytes(b"")
        stage("output", status="empty", reason=response_error, solution_sha256=sha256_bytes(b""))
        return call_status

    call_status.update({"send_attempted_at": time.time(), "request_count": 1})
    llm_trace = {
        "tool": "llm",
        "round": 0,
        "status": "send_attempted",
        "send_status": "uncertain",
        "response_received": False,
        "request_sha256": request_sha,
        "prompt_stage": call_status["prompt_stage"],
        "skill_loaded": call_status["skill_loaded"],
        "repair_style": call_status.get("repair_style"),
        "feedback_in_request": call_status.get("feedback_in_request"),
        "call_count": 1,
        "started_at": call_status["send_attempted_at"],
    }
    save_tool_trace()
    stage("send", status="attempted", request_count=1, remaining_s=round(remaining, 6))
    try:
        status, headers, raw_response = (transport or http_post_once)(
            config.model_base_url.rstrip("/") + "/chat/completions",
            request_body,
            remaining,
        )
        if not isinstance(raw_response, bytes):
            raise TypeError("transport response body is not bytes")
        _atomic_write(call_dir / "response_body.bin", raw_response)
        response_sha = sha256_bytes(raw_response)
        call_status.update(
            {
                "response_received_at": time.time(),
                "http_status": status,
                "response_sha256": response_sha,
            }
        )
        llm_trace.update(
            {
                "status": "response_received",
                "send_status": "sent",
                "response_received": True,
                "http_status": status,
                "response_sha256": response_sha,
                "response_headers": {
                    str(key): str(value) for key, value in headers.items()
                } if isinstance(headers, Mapping) else {},
            }
        )
        save_tool_trace()
        stage("response", status="received", http_status=status, response_sha256=response_sha)
    except Exception as error:
        reason = "transport_error:{}: {}".format(type(error).__name__, str(error)[:300])
        call_status.update({"response_error": reason, "output_status": "empty_transport_error"})
        _atomic_write(call_dir / "response_error.json", json.dumps({"error": reason}, ensure_ascii=False).encode("utf-8"))
        llm_trace.update({"status": "transport_error", "send_status": "uncertain", "error": reason})
        save_tool_trace()
        stage("response", status="unavailable", send_status="uncertain", reason=reason)
        stage("parse", status="skipped", reason=reason)
        stage("check", status="skipped", reason=reason)
        _atomic_text(out_dir / "failure.txt", reason + "\n")
        call_status["solution_sha256"] = sha256_bytes(b"")
        stage("output", status="empty", reason=reason, solution_sha256=sha256_bytes(b""))
        return call_status

    parse_error: str | None = None
    content: str | None = None
    finish_reason: Any = None
    usage: Any = {}
    response_model: Any = None
    try:
        envelope = json.loads(raw_response.decode("utf-8"))
        if not isinstance(envelope, dict):
            raise TypeError("response envelope is not an object")
        choices = envelope["choices"]
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise TypeError("response choices is not a non-empty object list")
        choice = choices[0]
        message = choice["message"]
        if not isinstance(message, dict):
            raise TypeError("choice.message is not an object")
        content = message.get("content")
        if not isinstance(content, str):
            raise TypeError("message.content is not a string")
        finish_reason = choice.get("finish_reason")
        usage = envelope.get("usage", {})
        if usage is None:
            usage = {}
        if not isinstance(usage, dict):
            raise TypeError("response usage is not an object")
        response_model = envelope.get("model")
        _atomic_text(call_dir / "assistant_content.txt", content)
        _atomic_write(call_dir / "response_summary.json", json.dumps(
            {"http_status": status, "model": response_model, "finish_reason": finish_reason, "usage": usage},
            ensure_ascii=False, sort_keys=True,
        ).encode("utf-8"))
    except (KeyError, IndexError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        parse_error = "{}: {}".format(type(error).__name__, str(error)[:300])
    # A length-limited response is unfinished even if its final text looks complete.
    # Reject before patch application; the caller keeps its existing fallback.
    if parse_error is None and finish_reason == "length":
        parse_error = "model response truncated by generation or context limit"
    elif parse_error is None and not content.strip():
        parse_error = "model response has no final content"
    assistant_sha = sha256_bytes(content.encode("utf-8")) if isinstance(content, str) else None
    call_status.update(
        {
            "response_model": response_model,
            "finish_reason": finish_reason,
            "usage": usage,
            "assistant_sha256": assistant_sha,
        }
    )
    if parse_error:
        llm_trace.update({"parse_status": "failed", "parse_error": parse_error})
        stage("parse", status="failed", reason=parse_error, response_sha256=response_sha)
    else:
        llm_trace.update(
            {
                "parse_status": "parsed",
                "response_model": response_model,
                "finish_reason": finish_reason,
                "usage": usage,
                "assistant_sha256": assistant_sha,
            }
        )
        stage(
            "parse",
            status="parsed",
            finish_reason=finish_reason,
            usage=usage,
            response_model=response_model,
            assistant_sha256=call_status["assistant_sha256"],
        )

    candidate = ""
    candidate_bytes = b""
    check_status = "skipped" if parse_error else "fail"
    check_reason = parse_error or "no complete single RTL module could be extracted"
    check: Any = None
    processing_error: str | None = None
    try:
        edit_result = None
        if edit_mode and isinstance(content, str) and not parse_error:
            try:
                previous_code = repair[0]
                edited, edit_result = apply_edits(previous_code, content, sha256_bytes(previous_code.encode('utf-8')))
                candidate = _extract_rtl(edited)
                edit_result['status'] = 'applied'
            except EditError as error:
                # A model may answer using the old complete-RTL protocol. Keep
                # its original strict extractor and all subsequent checks.
                candidate = _extract_rtl(content)
                edit_result = ({'status':'legacy_full_rtl_response','reason':str(error)} if candidate else
                               {'status':'rejected','reason':str(error)})
            _atomic_text(call_dir/'edit_result.json',json.dumps(edit_result,sort_keys=True)+'\n')
            call_status['edit_result'] = edit_result
        else:
            candidate = _extract_rtl(content) if isinstance(content, str) and not parse_error else ""
        check = check_prompt_candidate(public_input, candidate) if candidate and not parse_error else None
        if not parse_error:
            if check is None:
                check_status = "fail"
                check_reason = "no complete single RTL module could be extracted"
            else:
                check_status = check.status
                check_reason = check.reason
        candidate_bytes = candidate.encode("utf-8")
        _atomic_write(call_dir / "candidate.v", candidate_bytes)
        check_payload = {
            "status": check_status,
            "reason": check_reason,
            "wrapper_recovered": bool(candidate and isinstance(content, str)
                                      and re.match(r'\A```(?:verilog|systemverilog|sv)[ \t]*\r?\n', content, re.I)
                                      and content.count('```') == 1),
            "expected": _interface_dict(check.expected) if check else None,
            "actual": _interface_dict(check.actual) if check else None,
        }
        _atomic_write(call_dir / "interface_check.json", json.dumps(check_payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        call_status.update(candidate_path=str(call_dir / "candidate.v"), interface_check=check_payload)
        stage("check", status=check_status, reason=check_reason, candidate_sha256=sha256_bytes(candidate_bytes))

        if time.monotonic() - started > timeout_s:
            reason = "request_budget_exhausted_before_output"
            solution = ""
            output_status = "empty_budget_exceeded"
        elif status < 200 or status >= 300:
            reason = "model_http_status_{}".format(status)
            solution = ""
            output_status = "empty_http_error"
        elif finish_reason == "length":
            reason = "model response truncated by generation or context limit"
            solution = ""
            output_status = "empty_truncated_response"
        elif parse_error == "model response has no final content":
            reason = parse_error
            solution = ""
            output_status = "empty_final_content"
        elif parse_error:
            reason = "response_parse_error: " + parse_error
            solution = ""
            output_status = "empty_parse_error"
        elif check_status in {"fail", "error"}:
            reason = check_reason
            solution = ""
            output_status = "empty_interface_or_format_failure"
        else:
            solution = candidate
            reason = check_reason if check_status == "unverified" else ""
            output_status = "candidate_interface_unverified" if check_status == "unverified" else "candidate_checked"
        solution_bytes = solution.encode("utf-8")
        _atomic_write(solution_path, solution_bytes)
        call_status.update(
            {
                "candidate_sha256": sha256_bytes(candidate_bytes),
                "solution_sha256": sha256_bytes(solution_bytes),
                "interface_status": check_status,
                "output_status": output_status,
                "reason": reason,
                "elapsed_s": round(time.monotonic() - started, 6),
            }
        )
        llm_trace.update(
            {
                "check_status": check_status,
                "output_status": output_status,
                "candidate_sha256": sha256_bytes(candidate_bytes),
                "solution_sha256": call_status["solution_sha256"],
            }
        )
        if not solution and reason:
            _atomic_text(out_dir / "failure.txt", reason + "\n")
        stage("output", status=output_status, reason=reason, solution_sha256=call_status["solution_sha256"])
    except Exception as error:
        processing_error = "post_response_error:{}: {}".format(type(error).__name__, str(error)[:300])
        reason = processing_error
        _atomic_write(solution_path, b"")
        _atomic_text(out_dir / "failure.txt", reason + "\n")
        call_status.update(
            {
                "candidate_sha256": sha256_bytes(candidate_bytes),
                "solution_sha256": sha256_bytes(b""),
                "interface_status": "error",
                "output_status": "empty_post_response_error",
                "reason": reason,
                "elapsed_s": round(time.monotonic() - started, 6),
            }
        )
        llm_trace.update(
            {
                "check_status": "error",
                "output_status": "empty_post_response_error",
                "error": reason,
                "candidate_sha256": sha256_bytes(candidate_bytes),
                "solution_sha256": call_status["solution_sha256"],
            }
        )
        stage("processing", status="failed", reason=reason)
        stage("output", status="empty_post_response_error", reason=reason, solution_sha256=call_status["solution_sha256"])
    save_tool_trace()
    return call_status


def run_agent(
    task_dir: Path,
    out_dir: Path,
    config: RuntimeConfig,
    *,
    timeout_s: float,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Bounded compile/interface repair with the separate public-spec review retained."""
    started = time.monotonic()
    deadline = started + max(0.0, timeout_s) - 10.0
    out_dir.mkdir(parents=True, exist_ok=True)
    solution_path = out_dir / "solution.v"
    trace_path = out_dir / "trace.jsonl"
    _atomic_text(solution_path, "")
    events: list[dict[str, Any]] = []
    _write_trace(trace_path, events)
    selected: dict[str, Any] = {"request_count": 0, "output_status": "empty_budget_exhausted"}
    selected_code = ""
    selected_round: int | None = None
    repair: tuple[str, str] | None = None
    repair_trigger: str | None = None
    full_repair_after_rejected_patch = False
    functional_repair_seen = False
    fresh_start = False
    clean_reread = False
    clean_reread_used = False
    compile_fallback: tuple[str, dict[str, Any], int] | None = None
    model_calls = 0
    attempts = 0
    interface_retry_used = False
    reviewed = False
    history: list[dict[str, Any]] = []
    prompt = (task_dir / "prompt.txt").read_text(encoding="utf-8") if (task_dir / "prompt.txt").is_file() else ""
    interface_text = (task_dir / "interface.txt").read_text(encoding="utf-8") if (task_dir / "interface.txt").is_file() else ""
    public_input = compose_public_input(prompt, interface_text)
    try:
        contract = parse_contract(public_input)
    except Exception as error:
        contract = {"status": "unverified", "reason": "public contract parser failed: " + type(error).__name__}
    _atomic_text(out_dir / "public_contract.json", json.dumps(contract, sort_keys=True) + "\n")
    events.append({"ts": time.time(), "tool": "public_contract", **contract})
    _write_trace(trace_path, events)
    stop_reason = "request_limit"

    def request_repair(code: str, feedback: dict[str, Any], round_index: int) -> tuple[str, str]:
        nonlocal fresh_start, clean_reread, clean_reread_used, repair_trigger
        repair_trigger = feedback.get('trigger')
        current = history[-1]
        failed = current.get("compile_status") == "fail" or current.get("interface_status") == "fail" or bool(current.get("public_spec_issues")) or current.get("functional_status") == "fail"
        repeated = any(item["candidate_sha256"] == current["candidate_sha256"] for item in history[:-1])
        declaration_cycle = "VRFC 10-9336" in current.get("error_codes", []) and any(
            "VRFC 10-1280" in item.get("error_codes", []) for item in history[:-1])
        fresh_start = ERROR_HISTORY_ENABLED and failed and (repeated or declaration_cycle or current.get("functional_status") == "fail")
        if (contract.get('kind') in ('cycle_events','public_table','counter_events') or contract.get('carry_enable')) and feedback.get('trigger') == 'public_functional_failure' and not repeated:
            # The bounded source/edge trace refers to this exact candidate.
            # Keep it in the repair request instead of discarding the design.
            fresh_start = False
        clean_reread = (not clean_reread_used and round_index == 1
                        and feedback.get("trigger") == "public_functional_failure")
        if contract.get('kind') == 'public_table' and feedback.get('trigger') == 'public_functional_failure':
            previous = next((h for h in reversed(history[:-1]) if 'bad_public_cells' in h), None)
            route = (progress_route(previous['bad_public_cells'], current['bad_public_cells'],
                                    previous['candidate_sha256'] == current['candidate_sha256'])
                     if previous is not None else 'first_table_repair')
            feedback['public_table_route'] = route
            if round_index == 1 and previous is not None:
                clean_reread = not clean_reread_used and route == 'clean_reread_stagnant'
            if not clean_reread:
                fresh_start = False
                instruction = ('Keep the current design and its corrected decisions; fix the remaining public cells.'
                               if route == 'continue_current_remaining_cells' else
                               'Reconstruct the logic from the full public table, not isolated example patches.'
                               if route == 'rebuild_from_full_public_table' else
                               'Use the verified public table calculation to repair this design.')
                feedback['feedback'] = 'PUBLIC_TABLE_ROUTE: ' + route + '. ' + instruction + '\n' + feedback['feedback']
        if contract.get('kind') == 'comb_fsm' and feedback.get('trigger') == 'public_functional_failure':
            previous = next((h for h in reversed(history[:-1]) if 'comb_check' in h), None)
            plan = repair_plan(code, contract, current.get('comb_check', {}),
                               previous.get('comb_check') if previous else None)
            feedback['comb_repair_plan'] = plan
            if plan.get('action') == 'edit_current':
                fresh_start = False
                clean_reread = False
                feedback['feedback'] = plan['feedback']
        # A compile/interface repair is not a functional repair attempt. Never
        # discard the first functional counterexample merely because it arrived
        # late in the three-request budget. Family-specific progress routes above
        # remain authoritative after a functional diagnostic was actually sent.
        if repair_trigger == 'public_functional_failure' and not functional_repair_seen:
            fresh_start = False
            clean_reread = False
        feedback['functional_repair_seen'] = functional_repair_seen
        if clean_reread:
            clean_reread_used = True
        feedback["repair_style"] = "clean_reread" if clean_reread else "fresh" if fresh_start else "edit"
        feedback["request_allowed"] = round_index < 2
        feedback["feedback_planned"] = round_index < 2 and not clean_reread
        if ERROR_HISTORY_ENABLED:
            feedback["feedback"] += error_history_feedback(history)
        events.append({"ts": time.time(), "tool": "repair_feedback", "round": round_index,
                       "candidate_sha256": result.get("candidate_sha256"), **feedback})
        _write_trace(trace_path, events)
        return code, feedback["feedback"]
    for round_index in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stop_reason = "budget_exhausted"
            break
        attempt_dir = out_dir / "attempts" / ("round_" + str(round_index))
        event_index = len(events)

        def record_model(event: dict[str, Any]) -> None:
            event["round"] = round_index
            event["ts"] = event.get("started_at", time.time())
            if len(events) == event_index:
                events.append(event)
            else:
                events[event_index] = event
            _write_trace(trace_path, events)

        attempts += 1
        result = _generate_once(
            task_dir, attempt_dir, config,
            timeout_s=min(300.0, remaining), transport=transport,
            repair=repair, on_trace=record_model,
            fresh_start=fresh_start,
            clean_reread=clean_reread,
            edit_mode=(repair_trigger == 'public_functional_failure' and not clean_reread and not fresh_start
                       and not full_repair_after_rejected_patch),
        )
        model_calls += result.get("request_count", 0)
        if (repair_trigger == 'public_functional_failure'
                and result.get('feedback_in_request') is True
                and result.get('request_count', 0) > 0
                and result.get('http_status') == 200):
            functional_repair_seen = True
        candidate = (attempt_dir / "solution.v").read_text(encoding="utf-8")
        interface = result.get("interface_check", {"status": "not_run", "reason": result.get("output_status", "no candidate")})
        attempt_history: dict[str, Any] = {
            "round": round_index, "candidate_sha256": result.get("candidate_sha256"),
            "interface_status": interface.get("status"), "interface_reason": interface.get("reason", ""),
        }
        repeated = bool(attempt_history["candidate_sha256"]) and any(
            item["candidate_sha256"] == attempt_history["candidate_sha256"] for item in history)
        history.append(attempt_history)
        if ERROR_HISTORY_ENABLED and repeated:
            events.append({"ts": time.time(), "tool": "candidate_repeat", "round": round_index,
                           "candidate_sha256": attempt_history["candidate_sha256"]})
        events.append({"ts": time.time(), "tool": "interface", "round": round_index,
                       "candidate_sha256": result.get("candidate_sha256"), **interface})
        _write_trace(trace_path, events)
        if round_index == 0:
            selected = dict(result)
        # Preserve the initial accepted candidate; never publish an unchecked repair.
        if round_index == 0 and candidate:
            selected_code, selected, selected_round = candidate, dict(result), round_index
            _atomic_text(solution_path, selected_code)
        if not candidate or interface.get("status") != "pass":
            if (not candidate and result.get('edit_result', {}).get('status') == 'rejected'
                    and result.get('http_status') == 200
                    and result.get('output_status') == 'empty_interface_or_format_failure'
                    and round_index < 2 and repair is not None):
                # Reuse the unchanged base and original diagnostic tuple. The
                # rejected response consumes a call; no new retry budget exists.
                full_repair_after_rejected_patch = True
                fresh_start = False
                clean_reread = False
                events.append({'ts':time.time(),'tool':'edit_protocol_fallback','round':round_index,
                               'reason':result['edit_result']['reason'],
                               'route':'original_full_rtl_repair_with_remaining_budget',
                               'base_sha256':sha256_bytes(repair[0].encode('utf-8'))})
                _write_trace(trace_path, events)
                continue
            confirmed_mismatch = (
                result.get("output_status") == "empty_interface_or_format_failure"
                and confirmed_interface_mismatch(interface)
            )
            events.append({"ts": time.time(), "tool": "compile", "round": round_index,
                           "status": "not_run", "reason": "candidate not accepted by interface/response checks",
                           "candidate_sha256": result.get("candidate_sha256"), "error_codes": []})
            if not INTERFACE_RETRY_ENABLED or not confirmed_mismatch or interface_retry_used or round_index == 2:
                stop_reason = result.get("output_status", "interface_unverified")
                _write_trace(trace_path, events)
                break
            interface_retry_used = True
            candidate = Path(result["candidate_path"]).read_text(encoding="utf-8")
            feedback = interface_feedback(interface)
            repair = request_repair(candidate, feedback, round_index)
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stop_reason = "budget_exhausted"
            events.append({"ts": time.time(), "tool": "compile", "round": round_index,
                           "status": "not_run", "reason": stop_reason,
                           "candidate_sha256": result.get("candidate_sha256"), "error_codes": []})
            _write_trace(trace_path, events)
            break
        compile_started = time.time()
        event = {"ts": compile_started, "tool": "compile", "round": round_index,
                 "status": "started", "candidate_sha256": sha256_bytes(candidate.encode("utf-8"))}
        events.append(event)
        _write_trace(trace_path, events)
        try:
            check = compile_rtl(candidate, attempt_dir, timeout_s=min(120.0, max(0.0, deadline - time.monotonic())))
        except Exception as error:
            check = {"status": "unavailable", "reason": "{}: {}".format(type(error).__name__, str(error)[:500]), "stages": []}
        _atomic_text(attempt_dir / "compile_result.json", json.dumps(check, ensure_ascii=False, sort_keys=True) + "\n")
        event.update(status=check["status"], elapsed_s=time.time() - compile_started,
                     stages=check.get("stages", []), excerpt=check.get("reason", "")[-6000:])
        feedback = compiler_feedback(check.get("reason", ""), work_dir=check.get("work_dir"), candidate=candidate) if check["status"] == "fail" else None
        event["error_codes"] = feedback["error_codes"] if feedback else []
        issues = check_reset_timing(prompt, candidate)
        attempt_history.update(compile_status=check["status"], error_codes=event["error_codes"], public_spec_issues=issues)
        if check["status"] == "pass" and interface.get("status") == "pass" and compile_fallback is None:
            fallback = dict(result)
            fallback.update(compile_status="pass", public_spec_issues=issues, functional_status="not_run")
            compile_fallback = candidate, fallback, round_index
        _atomic_text(attempt_dir / "behavior_check.json", json.dumps({
            "issues": issues, "scope": "public-spec reset timing only; no functional simulation",
        }, ensure_ascii=False, sort_keys=True) + "\n")
        events.append({"ts": time.time(), "tool": "public_spec_lint", "round": round_index,
                       "issues": issues, "functional_proof": False})
        _write_trace(trace_path, events)
        if round_index == selected_round:
            selected["compile_status"] = check["status"]
            selected["public_spec_issues"] = issues
        if time.monotonic() >= deadline:
            stop_reason = "budget_exhausted"
            break
        if check["status"] == "pass":
            function_started = time.time()
            try:
                function = functional_check(candidate, contract, attempt_dir,
                                            timeout_s=min(90.0, max(0.0, deadline-time.monotonic())))
            except Exception as error:
                function = {"status": "unavailable", "reason": "public self-check failed: " + type(error).__name__}
            _atomic_text(attempt_dir / "functional_result.json", json.dumps(function, sort_keys=True) + "\n")
            coverage = {"functional_coverage": (
                "partial" if function.get("unchecked_outputs") else "finite"
                ) if function["status"] in ("pass", "fail") else "unverified",
                "checked_outputs": function.get("checked_outputs", []),
                "unchecked_outputs": function.get("unchecked_outputs", [])}
            events.append({"ts": function_started, "tool": "public_functional", "round": round_index,
                           "candidate_sha256": result.get("candidate_sha256"), **function})
            _write_trace(trace_path, events)
            if round_index == selected_round:
                selected["functional_status"] = function["status"]
                selected.update(coverage)
            if compile_fallback is not None and compile_fallback[2] == round_index:
                compile_fallback[1].update(functional_status=function["status"], **coverage)
            if function["status"] not in ("pass", "fail", "unverified"):
                stop_reason = "functional_" + function["status"]
                break
            if time.monotonic() >= deadline:
                stop_reason = "budget_exhausted"
                break
            if function["status"] == "fail":
                if contract.get("kind") == "capture" and issues:
                    # Keep the proven local reset repair; a full rebuild can damage correct lane logic.
                    reviewed = True
                    repair = request_repair(candidate, {"trigger": "public_spec_review", "error_codes": [],
                                                       "hints": [], "feedback": review_feedback(issues, prompt)}, round_index)
                    continue
                attempt_history.update(functional_status="fail", functional_counterexamples=function.get("counterexamples", []))
                if contract.get('kind') == 'public_table':
                    attempt_history['bad_public_cells'] = function.get('bad_public_cells', [])
                if contract.get('kind') == 'comb_fsm':
                    attempt_history['comb_check'] = {k: function.get(k) for k in ('status','checks','mismatches','bad_public_cells','contract_sha256','vectors_sha256','candidate_sha256')}
                text = functional_feedback(function, contract, candidate) + "\n" + "\n".join(issues)
                repair = request_repair(candidate, {"trigger": "public_functional_failure", "error_codes": [],
                                                   "hints": [], "feedback": text}, round_index)
                continue
            if not issues or selected.get("compile_status") != "pass":
                selected_code, selected, selected_round = candidate, dict(result), round_index
                selected.update(compile_status="pass", public_spec_issues=issues, functional_status=function["status"])
                selected.update(coverage)
                _atomic_text(solution_path, selected_code)
            if reviewed and not issues:
                stop_reason = "compile_pass_after_review"
                break
            text = review_feedback(issues, prompt)
            if not text:
                stop_reason = "compile_pass"
                break
            reviewed = True
            repair = request_repair(candidate, {"trigger": "public_spec_review", "error_codes": [],
                                               "hints": [], "feedback": text}, round_index)
            continue
        if feedback is None:
            stop_reason = "compile_" + check["status"]
            break
        repair = request_repair(candidate, feedback, round_index)
    if selected.get("compile_status") != "pass" and compile_fallback is not None:
        selected_code, selected, selected_round = compile_fallback
        _atomic_text(solution_path, selected_code)
        events.append({"ts": time.time(), "tool": "selection_fallback",
                       "reason": "preserve_verified_compilation_progress",
                       "selected_round": selected_round,
                       "candidate_sha256": sha256_bytes(selected_code.encode("utf-8")),
                       "functional_check": selected.get("functional_status", "not_run")})
    selected.update(
        request_count=model_calls, attempts=attempts, selected_round=selected_round,
        interface_retry_used=interface_retry_used, stop_reason=stop_reason,
        behavioral_review_requested=reviewed,
        solution_sha256=sha256_bytes(selected_code.encode("utf-8")),
        elapsed_s=round(time.monotonic() - started, 6),
    )
    events.append({"ts": time.time(), "tool": "selection", "selected_round": selected_round,
                   "solution_sha256": selected["solution_sha256"],
                   "compile_status": selected.get("compile_status", "not_run"),
                   "model_calls": model_calls, "stop_reason": stop_reason,
                   "functional_check": selected.get("functional_status", "not_run")})
    events[-1].update({key: selected.get(key) for key in
                      ("functional_coverage", "checked_outputs", "unchecked_outputs")})
    _write_trace(trace_path, events)
    _append_diagnostic(out_dir / "diagnostics.jsonl", {"ts": time.time(), "state": "compile_repair_output", **selected})
    return selected


def _interface_dict(interface: Any) -> Any:
    if interface is None:
        return None
    return {"module": interface.module, "ports": {
        name: {"direction": port.direction, "width": port.width}
        for name, port in sorted(interface.ports.items())
    }}
