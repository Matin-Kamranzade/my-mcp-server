import requests
import json
import re
import time
import datetime
from collections import deque

# === CONFIG ===
MCP_URL = "http://localhost:8000/run"
OLLAMA_URL = "http://10.150.249.12:8080"
LLM_MODEL = "gemma3:12b"

# === GLOBAL TOOL CACHE ===
TOOLS_INFO = {}

# === SHORT-TERM MEMORY ===
CONVERSATION_HISTORY = deque(maxlen=10)

# === Logging Helpers ===
def log_info(msg: str):
    now = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] [INFO] {msg}")

def log_error(msg: str):
    now = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] [ERROR] {msg}")

# === CORE FUNCTIONS ===

def ollama_warmup():
    """Ensure Ollama is awake before first use."""
    log_info("Warming up Ollama...")
    try:
        payload = {"model": LLM_MODEL, "prompt": "ping", "stream": False}
        requests.post(OLLAMA_URL + "/generate", json=payload, timeout=10)
        log_info("Ollama is ready.")
    except Exception:
        log_error("Ollama warm-up failed — will retry on first prompt.")


def ask_llm(prompt: str) -> str:
    """Send prompt to Ollama model and return plain text."""
    payload = {"model": LLM_MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0.2}}
    url = OLLAMA_URL + "/v1/completions"

    for attempt in range(2):
        try:
            r = requests.post(url, json=payload, timeout=90)
            r.raise_for_status()
            data = r.json()

            # ✅ Handle possible schema variations
            text = ""
            if isinstance(data, dict):
                if "response" in data:
                    text = data["response"].strip()
                elif "content" in data:
                    text = data["content"].strip()
                elif "choices" in data and data["choices"]:
                    text = data["choices"][0].get("text", "").strip()

            log_info(f"LLM Response:\n{text}\n")
            return text
        except Exception as e:
            if attempt == 0:
                log_error("LLM not reachable (attempt 1), retrying...")
                time.sleep(3)
                continue
            log_error(f"Failed to contact LLM: {e}")
            return ""


def get_tool_definitions() -> dict:
    """Fetch tool definitions from MCP server."""
    try:
        r = requests.get(MCP_URL.replace("/run", "/tools"), timeout=10)
        r.raise_for_status()
        return r.json().get("tools", {})
    except Exception as e:
        log_error(f"Failed to get tool definitions: {e}")
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


def tool_requires_namespace(tool_name: str) -> bool:
    """Check if tool needs a namespace argument."""
    signature = TOOLS_INFO.get(tool_name, {})
    return isinstance(signature, dict) and "namespace" in signature


def interpret_intent(user_input: str) -> list[dict]:
    """Convert user input into one or more JSON MCP commands."""

    tool_descriptions = "\n".join(
        f"- {name}: {info.get('doc', '').strip() or info.get('signature', '')}"
        for name, info in TOOLS_INFO.items()
    )

    # Include recent context
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
        "Output must be raw JSON only — no markdown, no code fences.\n"
        "Each command must be a JSON object with 'tool' and 'args'.\n\n"
        "Available tools:\n"
        f"{tool_descriptions}\n\n"
        "Rules:\n"
        "- Only call tools the user explicitly requests.\n"
        "- Do NOT guess or invent parameters.\n"
        "- If 'namespace' is missing and required, default to 'default'.\n"
        "If you are asked for logs of pods in plural, that means you will execute the function that will not require specific name,"
        "just namespace. If you are asked in singular form, give me logs of this pods, you execute the function that requires name of specific pod\n"
        "- Examples:\n"
        '{"tool": "list_pods", "args": {"namespace": "default"}}\n'
        '{"tool": "get_nodes", "args": {}}\n'
        '{"tool": "delete_namespace", "args": {"namespace": "test-ns"}}\n'
        '{"tool": "scale_deployment", "args": {"deployment_name": "nginx", "replicas": 3, "namespace": "default"}}\n'
    )

    full_prompt = f"{system_prompt}\n{history_text}User: {user_input}\nCommand:"
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
            log_info(f"Ignored unknown tool: {data['tool']}")

    if not commands:
        log_error(f"Could not find valid JSON in LLM output:\n{llm_output}")
        return []

    return commands


def call_mcp(command: dict) -> dict:
    """Send JSON command to MCP server."""
    if not command:
        return {"error": "Invalid command."}

    payload = {"tool": command.get("tool"), "args": command.get("args", {})}
    try:
        r = requests.post(MCP_URL, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def pretty_print(data):
    """Format JSON data into table-like readable output."""
    if isinstance(data, dict) and "error" in data:
        return f"❌ Error: {data['error']}"

    if isinstance(data, dict) and "result" in data:
        data = data["result"]

    if isinstance(data, list) and data and isinstance(data[0], dict):
        keys = list(data[0].keys())
        header = " | ".join(keys)
        line = "-+-".join("-" * len(k) for k in keys)

        rows = []
        for item in data:
            row = " | ".join(str(item.get(k, "")) for k in keys)
            rows.append(row)

        return f"{header}\n{line}\n" + "\n".join(rows)

    if isinstance(data, dict):
        return "\n".join(f"{k}: {v}" for k, v in data.items())

    return str(data)


def run_agent():
    """Main REPL loop."""
    global TOOLS_INFO

    log_info("🧠 Universal MCP Agent Started")
    ollama_warmup()

    TOOLS_INFO = get_tool_definitions()
    if not TOOLS_INFO:
        log_error("No tools retrieved.")
    else:
        log_info(f"Loaded {len(TOOLS_INFO)} tools from MCP.")

    while True:
        user_input = input("\nYou: ").strip()
        if user_input.lower() in ("exit", "quit"):
            log_info("Exiting agent.")
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
            log_info(f"Executing: {cmd['tool']} {cmd['args']}")
            result = call_mcp(cmd)
            formatted = pretty_print(result)
            print(formatted)

            mcp_output_str += json.dumps(result, indent=2) + "\n"

        update_history(user_input, llm_output_str, mcp_output_str)


if __name__ == "__main__":
    run_agent()
