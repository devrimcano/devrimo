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

Exit status is non-zero when a scenario fails, so a staging pipeline can gate on
it. The memory scenario cleans up after itself through DELETE /api/v1/memories.
"""

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
        if "update" not in (result.get("tools") or []):
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

    failures = 0
    for scenario in scenarios:
        try:
            result = ask(headers, scenario["prompt"], scenario["session"])
            problems = scenario["check"](result)
        except Exception as exc:  # one broken scenario must not hide the rest
            result = {"seconds": None, "tools": [], "answer": ""}
            problems = [f"{type(exc).__name__}: {exc}"]
        status = "PASS" if not problems else "FAIL"
        if problems:
            failures += 1
        print(
            f"{status}  {scenario['name']:<18} {result.get('seconds')}s  "
            f"ttft={result.get('ttft')}  tools={result.get('tools')}",
            flush=True,
        )
        for problem in problems:
            print(f"      - {problem}", flush=True)
        if problems and result.get("answer"):
            print(f"      answer: {result['answer'][:220]}", flush=True)

    httpx.delete(f"{API}/api/v1/memories", headers=headers, timeout=30)
    print(f"\n{len(scenarios) - failures}/{len(scenarios)} scenarios passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
