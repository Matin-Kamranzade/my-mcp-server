import requests
import json
import re
import time
from collections import deque
# === CONFIG ===
OLLAMA_URL = "http://192.168.221.106:11434/api/generate"
MCP_URL = "http://localhost:8000/run"
LLM_MODEL = "gemma2:2b"

# === GLOBAL TOOL CACHE ===
TOOLS_INFO = {}

CONVERSATION_HISTORY = deque(maxlen=10)  # ✅ store last 10 turns
# === FUNCTIONS ===

def ollama_warmup():
    """
    Ensures Ollama is awake before first use.
    Sometimes Ollama spins down and causes the first request to hang.
    """
    print("[Agent] Warming up Ollama...")
    try:
        payload = {"model": LLM_MODEL, "prompt": "ping", "stream": False}
        requests.post(OLLAMA_URL, json=payload, timeout=10)
        print("[Agent] Ollama is ready.")
    except Exception:
        print("[Agent] Ollama warm-up failed — will retry on first prompt.")


def ask_llm(prompt: str) -> str:
    """Send prompt to the LLM and return its response text, with retry logic."""
    payload = {"model": LLM_MODEL, "prompt": prompt, "stream": False}

    for attempt in range(2):
        try:
            response = requests.post(OLLAMA_URL, json=payload, timeout=60)
            response.raise_for_status()
            return response.json().get("response", "").strip()
        except Exception as e:
            if attempt == 0:
                print(f"[Agent] Ollama not reachable (attempt 1), retrying...")
                time.sleep(3)
                continue
            print(f"[Agent] Error contacting LLM: {e}")
            return ""


def get_tool_definitions() -> dict:
    """Fetch tool definitions from MCP server."""
    try:
        r = requests.get(MCP_URL.replace("/run", "/tools"), timeout=10)
        r.raise_for_status()
        return r.json().get("tools", {})
    except Exception as e:
        print(f"[Agent] Failed to get tool definitions: {e}")
        return {}


def extract_json_objects(text: str) -> list[dict]:
    """Extract multiple JSON objects safely from LLM output."""
    text = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```", "", text).strip()

    objs, brace_level, start = [], 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if brace_level == 0:
                start = i
            brace_level += 1
        elif ch == "}":
            brace_level -= 1
            if brace_level == 0 and start is not None:
                try:
                    candidate = text[start:i + 1]
                    objs.append(json.loads(candidate))
                except json.JSONDecodeError:
                    pass
    return objs


def update_history(user_input: str, llm_output: str, mcp_output: str):
    """Store user input, LLM command output, and actual MCP response."""
    CONVERSATION_HISTORY.append({
        "user": user_input,
        "llm": llm_output,
        "mcp": mcp_output
    })



def interpret_intent(user_input: str) -> list[dict]:
    """Convert natural language into one or more JSON MCP commands."""

    tool_descriptions = "\n".join(
        f"- {name}: {info.get('doc', '').strip() or info.get('signature', '')}"
        for name, info in TOOLS_INFO.items()
    )

    # Include short-term history
    history_text = ""
    if CONVERSATION_HISTORY:
        history_text = "Recent conversation:\n" + "\n".join(
            f"User: {h.get('user', '')}\n"
            f"LLM: {h.get('llm', '')}\n"
            f"Agent: {h.get('mcp', '')}"
            for h in CONVERSATION_HISTORY
        ) + "\n\n"

    system_prompt = (
        "You are a command translator for a Kubernetes management agent.\n"
        "Convert user input into one or more JSON commands for the MCP server.\n"
        "Output must be raw JSON only — no markdown, no text, no code fences.\n"
        "Each command must be a valid JSON object with 'tool' and 'args'.\n"
        "Available tools and their arguments:\n"
        f"{tool_descriptions}\n\n"
        "Never go beyond the parameters defined in tool descriptions.\n"
        "If a tool has 'namespace' as a parameter but the user doesn't specify it, set it to 'default'.\n"
        "If namespace isn't required, omit it.\n"
        "Examples:\n"
        '{"tool": "list_pods", "args": {"namespace": "default"}}\n'
        '{"tool": "delete_namespace", "args": {"namespace": "ns_name"}}\n'
        '{"tool": "scale_deployment", "args": {"deployment_name": "nginx", "replicas": 4, "namespace": "default"}}\n'
        '{"tool": "restart_deployment", "args": {"deployment_name": "cicd", "namespace": "default"}}\n'
        '{"tool": "get_nodes", "args": {}}\n'
        "If multiple values are given for one argument, generate one JSON command per value.\n"
    )

    full_prompt = f"{system_prompt}\n{history_text}User: {user_input}\nCommand:"
    print(full_prompt)
    llm_output = ask_llm(full_prompt).strip()

    extracted = extract_json_objects(llm_output)
    commands = []

    for data in extracted:
        if not isinstance(data, dict) or "tool" not in data:
            continue

        if "args" not in data or not isinstance(data["args"], dict):
            data["args"] = {}

        if tool_requires_namespace(data["tool"]):
            data["args"].setdefault("namespace", "default")
        else:
            data["args"].pop("namespace", None)

        if data["tool"] in TOOLS_INFO:
            commands.append(data)
        else:
            print(f"[Agent] Ignored unknown tool: {data['tool']}")


    if not commands:
        print(f"[Agent] Could not find valid JSON in LLM output:\n{llm_output}")
        return []

    return commands


def tool_requires_namespace(tool_name: str) -> bool:
    """Check if tool needs a namespace argument."""
    signature = TOOLS_INFO.get(tool_name, {})
    return isinstance(signature, dict) and "namespace" in signature


def call_mcp(command: dict) -> dict:
    """Send parsed JSON command to MCP server and return its result."""
    if not command:
        return {"error": "Invalid command."}

    payload = {"tool": command.get("tool"), "args": command.get("args", {})}
    try:
        r = requests.post(MCP_URL, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def run_agent():
    """Main REPL loop."""
    global TOOLS_INFO

    print("Agent initializing...")
    ollama_warmup()

    TOOLS_INFO = get_tool_definitions()
    if not TOOLS_INFO:
        print("[Agent] No tools retrieved.")
    else:
        print(f"[Agent] Loaded {len(TOOLS_INFO)} tools from MCP.\n")

    print("Agent ready. Type commands ('exit' to quit, 'show tools' to list tools):\n")

    while True:
        user_input = input("> ").strip()
        if user_input.lower() in ("exit", "quit"):
            print("Exiting agent.")
            break

        if user_input.lower() in ("show tools", "list tools"):
            for name, info in TOOLS_INFO.items():
                args_desc = ", ".join(f"{k}: {v}" for k, v in info.items())
                print(f"- {name}: {args_desc}")
            continue

        commands = interpret_intent(user_input)
        if not commands:
            continue

        llm_output_str = json.dumps(commands, indent=2)
        mcp_output_str = ""

        for cmd in commands:
            print(f"[Agent] Executing: {cmd['tool']} {cmd['args']}")
            result = call_mcp(cmd)
            result_json = json.dumps(result, indent=2)
            print(result_json)
            mcp_output_str += f"[Agent] Executing: {cmd['tool']} {cmd['args']}\n{result_json}\n"

        # ✅ Record all layers of this turn
        update_history(user_input, llm_output_str, mcp_output_str)  

if __name__ == "__main__":
    run_agent()
