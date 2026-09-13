"""Live agent eval harness for the Scholar chat surface.

Runs scripted student questions against a running API and checks the behaviour
that has regressed before: tool count, tool errors, answer language, internal
jargon, and key facts. It needs a real session token, so it is deliberately not
part of CI; point it at staging or a local backend:

    SUPABASE_URL=... SUPABASE_SECRET_KEY=... DEVRIMO_EVAL_USER_ID=... \
        python backend/scripts/agent_eval.py

Environment:
    DEVRIMO_EVAL_USER_ID   Supabase user id the magic link is issued for
    DEVRIMO_API            API base (default http://127.0.0.1:8000)
    DEVRIMO_SITE           Redirect target for the magic link
    DEVRIMO_EVAL_TOKEN     Skip Supabase and use this bearer token instead
    DEVRIMO_EVAL_ALLOW_MEMORY_WRITE=1
                           Run the memory scenarios at all. Without it they are
                           skipped, and no memory endpoint is ever touched.
    DEVRIMO_EVAL_DISPOSABLE=1
                           Allow the memory scenarios on an account that
                           already has memories. The original memory list is
                           restored with an optimistic revision check.

Memory scenarios are opt-in and restore the exact pre-run list through the
workspace's revisioned update contract. A concurrent memory write makes the
revision check fail and leaves the account untouched rather than deleting an
account-wide memory set. Exit status is non-zero when a scenario or safe
restoration fails, so a staging pipeline can gate on it.
"""

import asyncio
import json
import os
import re
import sys
import time
import uuid
from urllib.parse import parse_qs, urlparse

import httpx

API = os.environ.get("DEVRIMO_API", "http://127.0.0.1:8000").rstrip("/")
SITE = os.environ.get("DEVRIMO_SITE", "https://devrimo.ates.digital")

# Terms that belong to the machinery, not to an answer. Word-bounded so
# "sorgulama" (ordinary Turkish for querying) does not trip them.
JARGON = re.compile(
    r"\b(published release|veritaban|şema|schema|tool|catalog\.|course_count|system prompt)\b",
    re.IGNORECASE,
)
SPANISH = re.compile(r"\b(el|la|los|las|debe|pero|todos|cursos|requisito)\b")

MEMORY_SCENARIOS = {"memory", "memory_follow_up"}
ALLOW_MEMORY_WRITE = os.environ.get("DEVRIMO_EVAL_ALLOW_MEMORY_WRITE") == "1"
DISPOSABLE = os.environ.get("DEVRIMO_EVAL_DISPOSABLE") == "1"


def list_memories(headers: dict) -> list[dict] | None:
    """The account's memories, or None when that could not be verified."""
    try:
        response = httpx.get(f"{API}/api/v1/memories", headers=headers, timeout=30)
    except Exception:
        return None
    if response.status_code != 200:
        return None
    try:
        memories = response.json().get("memories", [])
    except (TypeError, ValueError):
        return None
    return memories if isinstance(memories, list) else None


def _normalized_memories(memories) -> list[dict] | None:
    """Return the stable memory shape, or None for an unverifiable payload."""
    if not isinstance(memories, list):
        return None
    normalized = []
    for item in memories:
        if not isinstance(item, dict) or not item.get("id") or not item.get("content"):
            return None
        normalized.append({"id": str(item["id"]), "content": str(item["content"])})
    return normalized


def _same_memories(left: list[dict], right: list[dict]) -> bool:
    """Compare memory lists without depending on their storage order."""
    return sorted(left, key=lambda item: (item["id"], item["content"])) == sorted(
        right, key=lambda item: (item["id"], item["content"])
    )


async def _workspace_call(headers: dict, name: str, arguments: dict) -> dict:
    """Call one authenticated workspace tool for revisioned memory access."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    # The chat request uses an SSE-only Accept header; the stateless MCP
    # transport negotiates its own JSON/event-stream pair, so forward only the
    # bearer credential here.
    workspace_headers = {"Authorization": headers["Authorization"]}
    async with httpx.AsyncClient(headers=workspace_headers, timeout=30) as client:
        async with streamable_http_client(f"{API}/mcp/", http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
                if result.isError:
                    detail = "; ".join(
                        str(getattr(item, "text", "")).strip()
                        for item in result.content
                        if getattr(item, "text", "")
                    )
                    raise RuntimeError(detail or f"workspace {name} failed")
                payload = result.structuredContent
                if payload is None:
                    for item in result.content:
                        if getattr(item, "type", None) == "text":
                            payload = json.loads(item.text)
                            break
                if not isinstance(payload, dict):
                    raise RuntimeError(f"workspace {name} returned no structured result")
                return payload


def _memory_state_from_payload(payload: dict) -> dict | None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    revision = data.get("revision")
    memories = _normalized_memories(data.get("memories"))
    if not isinstance(revision, int) or revision < 0 or memories is None:
        return None
    return {"revision": revision, "memories": memories}


def memory_snapshot(headers: dict) -> dict | None:
    """Read memories and their revision through the workspace boundary."""
    try:
        payload = asyncio.run(_workspace_call(headers, "read", {"resource": {"kind": "my.memory"}}))
    except Exception:
        return None
    return _memory_state_from_payload(payload)


def restore_memory_snapshot(headers: dict, baseline: dict, current: dict) -> tuple[bool, str]:
    """Restore the baseline only if the observed revision is still current."""
    if _same_memories(current["memories"], baseline["memories"]):
        return True, "already at the pre-run state"
    try:
        payload = asyncio.run(
            _workspace_call(
                headers,
                "update",
                {
                    "resource": {"kind": "my.memory"},
                    "changes": {"memories": baseline["memories"]},
                    "expected_revision": current["revision"],
                    "idempotency_key": f"agent-eval-restore-{uuid.uuid4().hex}",
                },
            )
        )
    except Exception as exc:
        return False, f"optimistic memory restoration failed: {exc}"
    restored = _memory_state_from_payload(payload)
    if restored is None:
        return False, "optimistic memory restoration returned an invalid result"
    verified = memory_snapshot(headers)
    if verified is None:
        return False, "memory restoration could not be verified"
    if verified["revision"] != restored["revision"] or not _same_memories(
        verified["memories"], baseline["memories"]
    ):
        return False, "memory restoration changed concurrently; no safe final state was verified"
    return True, "restored the pre-run state"


def cleanup_memory_snapshot(headers: dict, baseline: dict, expected_revision: int) -> tuple[bool, str]:
    """Restore only the revision observed after the last eval scenario.

    The final read and the update are both optimistic, but the read itself must
    also agree with the revisions observed after each scenario. Otherwise a
    concurrent writer could be mistaken for the eval's own update and be
    overwritten during cleanup.
    """
    current = memory_snapshot(headers)
    if current is None:
        return False, "memory revision could not be verified"
    if current["revision"] != expected_revision:
        return False, "memory changed concurrently; refusing to overwrite the account's current state"
    return restore_memory_snapshot(headers, baseline, current)


def memory_write_allowed(existing: list[dict] | None) -> bool:
    """Whether the opt-in memory scenarios may run with scoped restoration."""
    return ALLOW_MEMORY_WRITE and existing is not None and (not existing or DISPOSABLE)


def acquire_token() -> str:
    token = os.environ.get("DEVRIMO_EVAL_TOKEN")
    if token:
        return token
    base = os.environ["SUPABASE_URL"].rstrip("/")
    secret = os.environ["SUPABASE_SECRET_KEY"]
    user_id = os.environ["DEVRIMO_EVAL_USER_ID"]
    admin = {"apikey": secret, "Authorization": f"Bearer {secret}"}
    email = httpx.get(f"{base}/auth/v1/admin/users/{user_id}", headers=admin, timeout=30).json()["email"]
    hashed = httpx.post(
        f"{base}/auth/v1/admin/generate_link",
        headers=admin,
        json={"type": "magiclink", "email": email, "redirect_to": f"{SITE}/auth/callback"},
        timeout=30,
    ).json()["hashed_token"]
    verify = httpx.get(
        f"{base}/auth/v1/verify",
        headers={"apikey": secret},
        params={"type": "magiclink", "token": hashed, "redirect_to": f"{SITE}/auth/callback"},
        timeout=30,
        follow_redirects=False,
    )
    token = (parse_qs(urlparse(verify.headers.get("location", "")).fragment).get("access_token") or [""])[0]
    if not token:
        raise SystemExit("Could not acquire a session token")
    return token


def ask(headers: dict, prompt: str, session_id: str | None = None) -> dict:
    body = {"messages": [{"role": "user", "content": prompt}], "idempotency_key": str(uuid.uuid4())}
    if session_id:
        body["session_id"] = session_id
    started = time.time()
    ttft = None
    text: list[str] = []
    tools: list[str] = []
    completed_tools: list[str] = []
    errors: list[str] = []
    with httpx.stream("POST", f"{API}/api/v1/chat/completions", json=body, headers=headers, timeout=300) as response:
        if response.status_code == 409:
            return {"busy": True}
        if response.status_code != 200:
            return {"status": response.status_code, "answer": response.read().decode()[:200], "errors": ["transport"]}
        for line in response.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except ValueError:
                continue
            extension = event.get("devrimo") or {}
            delta = (event.get("choices") or [{}])[0].get("delta") or {}
            if delta.get("content"):
                if ttft is None:
                    ttft = round(time.time() - started, 1)
                text.append(delta["content"])
            if extension.get("type") == "tool_call_started":
                tools.append(extension.get("tool") or "")
            if extension.get("type") == "tool_call_completed" and extension.get("success") is True:
                completed_tools.append(extension.get("tool") or "")
            if extension.get("type") == "tool_call_error":
                errors.append(f"{extension.get('tool')}: {str(extension.get('message'))[:120]}")
            if extension.get("type") == "error":
                errors.append(f"run: {str(extension.get('message'))[:120]}")
    answer = "".join(text).strip()
    return {
        "status": 200,
        "seconds": round(time.time() - started, 1),
        "ttft": ttft,
        "tools": tools,
        "completed_tools": completed_tools,
        "errors": errors,
        "answer": answer,
        "jargon": sorted(set(match.group(0) for match in JARGON.finditer(answer))),
    }


def _common(result: dict) -> list[str]:
    """Failures every scenario shares: transport, tool errors, jargon."""
    if result.get("busy"):
        return ["agent busy"]
    failures = []
    if result.get("errors"):
        failures.append(f"tool errors: {result['errors']}")
    if result.get("jargon"):
        failures.append(f"internal jargon: {result['jargon']}")
    return failures


def _completed_update_calls(result: dict) -> int:
    """Count only update calls that emitted a completion event."""
    return (result.get("completed_tools") or []).count("update")


def build_scenarios(memory_session: str) -> list[dict]:
    def greeting(result):
        failures = _common(result)
        if result.get("tools"):
            failures.append(f"expected no tools, saw {result['tools']}")
        return failures

    def prerequisites(result):
        failures = _common(result)
        if len(result.get("tools") or []) > 4:
            failures.append(f"too many tool calls: {result['tools']}")
        for expected in ("MATH 260", "DD"):
            if expected not in result.get("answer", ""):
                failures.append(f"missing {expected!r}")
        return failures

    def credits(result):
        failures = _common(result)
        if "kredi" not in result.get("answer", "").casefold():
            failures.append("no credits in the answer")
        return failures

    def timetable(result):
        failures = _common(result)
        if result.get("tools"):
            failures.append(f"expected the planned week without tools, saw {result['tools']}")
        return failures

    def english(result):
        failures = _common(result)
        if "prerequisit" not in result.get("answer", "").casefold():
            failures.append("not answered in English")
        if SPANISH.search(result.get("answer", "")):
            failures.append("answer drifted into another language")
        return failures

    def out_of_scope(result):
        return _common(result) + (
            ["send_email ran without a recipient"] if "send_email" in (result.get("tools") or []) else []
        )

    def memory(result):
        failures = _common(result)
        if "update" not in (result.get("completed_tools") or []):
            failures.append("the preference was promised but not written")
        return failures

    def memory_follow_up(result):
        failures = _common(result)
        if "today" not in result.get("answer", "").casefold() and "saturday" not in result.get("answer", "").casefold():
            failures.append("memory preference did not carry to the next turn")
        return failures

    return [
        {
            "name": "greeting",
            "prompt": "Merhaba! Kısaca nasılsın, neler yapabilirsin?",
            "session": None,
            "check": greeting,
        },
        {
            "name": "prerequisites",
            "prompt": "EE201 dersinin ön koşulu nedir? Açılan şubeleri de yaz.",
            "session": None,
            "check": prerequisites,
        },
        {
            "name": "credits",
            "prompt": "CENG331 bu dönem açılıyor mu, kaç kredi?",
            "session": None,
            "check": credits,
        },
        {
            "name": "timetable",
            "prompt": "Bu dönem için planladığım dersler neler? Kısaca listele.",
            "session": None,
            "check": timetable,
        },
        {
            "name": "english",
            "prompt": "What are the prerequisites for PHYS 213?",
            "session": None,
            "check": english,
        },
        {
            "name": "out_of_scope",
            "prompt": "Bana kısa bir mail yazıp gönder: 'Merhaba'.",
            "session": None,
            "check": out_of_scope,
        },
        {
            "name": "memory",
            "prompt": "Bundan sonra bana her zaman İngilizce cevap ver, bunu hatırla.",
            "session": memory_session,
            "check": memory,
        },
        {
            "name": "memory_follow_up",
            "prompt": "Bugün günlerden ne?",
            "session": memory_session,
            "check": memory_follow_up,
        },
    ]


def main() -> int:
    token = acquire_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
    memory_session = str(uuid.uuid4())
    scenarios = build_scenarios(memory_session)
    only = sys.argv[1:]
    if only:
        scenarios = [scenario for scenario in scenarios if scenario["name"] in only]

    selected_memory = any(scenario["name"] in MEMORY_SCENARIOS for scenario in scenarios)
    existing = None
    baseline = None
    memory_tracking_error = None
    if selected_memory:
        reason = None
        if not ALLOW_MEMORY_WRITE:
            reason = "DEVRIMO_EVAL_ALLOW_MEMORY_WRITE is not set"
        else:
            existing = list_memories(headers)
            if not memory_write_allowed(existing):
                reason = (
                    "the account's memories could not be read"
                    if existing is None
                    else "the account has memories"
                )
            else:
                baseline = memory_snapshot(headers)
                if baseline is None or not _same_memories(existing, baseline["memories"]):
                    memory_tracking_error = "the account's memory revision could not be verified"
                    reason = memory_tracking_error
        if reason:
            for scenario in [item for item in scenarios if item["name"] in MEMORY_SCENARIOS]:
                print(f"SKIP  {scenario['name']:<18} {reason}", flush=True)
            scenarios = [scenario for scenario in scenarios if scenario["name"] not in MEMORY_SCENARIOS]
    ran_memory = any(scenario["name"] in MEMORY_SCENARIOS for scenario in scenarios)

    scenario_failures = 0
    expected_memory_revision = baseline["revision"] if baseline else None
    for scenario in scenarios:
        if scenario["name"] in MEMORY_SCENARIOS and memory_tracking_error:
            result = {"seconds": None, "tools": [], "answer": ""}
            problems = [memory_tracking_error]
        else:
            try:
                result = ask(headers, scenario["prompt"], scenario["session"])
                problems = scenario["check"](result)
            except Exception as exc:  # one broken scenario must not hide the rest
                result = {"seconds": None, "tools": [], "answer": ""}
                problems = [f"{type(exc).__name__}: {exc}"]
        if scenario["name"] in MEMORY_SCENARIOS and baseline and not memory_tracking_error:
            expected_memory_revision += _completed_update_calls(result)
            observed = memory_snapshot(headers)
            if observed is None:
                memory_tracking_error = "memory revision could not be read after the scenario"
            elif observed["revision"] != expected_memory_revision:
                memory_tracking_error = (
                    "memory changed concurrently; refusing to overwrite the account's current state"
                )
            if memory_tracking_error:
                problems.append(memory_tracking_error)
        status = "PASS" if not problems else "FAIL"
        if problems:
            scenario_failures += 1
        print(
            f"{status}  {scenario['name']:<18} {result.get('seconds')}s  "
            f"ttft={result.get('ttft')}  tools={result.get('tools')}",
            flush=True,
        )
        for problem in problems:
            print(f"      - {problem}", flush=True)
        if problems and result.get("answer"):
            print(f"      answer: {result['answer'][:220]}", flush=True)

    cleanup_failed = False
    if ran_memory and baseline and not memory_tracking_error:
        restored, detail = cleanup_memory_snapshot(headers, baseline, expected_memory_revision)
        if restored:
            print(f"memory cleanup: {detail}", flush=True)
        else:
            cleanup_failed = True
            print(f"FAIL  memory cleanup     {detail}", flush=True)
    elif ran_memory and memory_tracking_error:
        cleanup_failed = True
        print(f"FAIL  memory cleanup     {memory_tracking_error}; no destructive cleanup attempted", flush=True)
    print(f"\n{len(scenarios) - scenario_failures}/{len(scenarios)} scenarios passed")
    return 1 if scenario_failures or cleanup_failed else 0


if __name__ == "__main__":
    sys.exit(main())
