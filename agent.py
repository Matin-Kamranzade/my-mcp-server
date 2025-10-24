import requests
import json
import re
import time

# === CONFIG ===
OLLAMA_URL = "http://192.168.221.106:11434/api/generate"
MCP_URL = "http://localhost:8000/run"
LLM_MODEL = "gemma2:2b"

# === GLOBAL TOOL CACHE ===
TOOLS_INFO = {}


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
            response = requests.post(OLLAMA_URL, json=payload, timeout=30)
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
    """
    Robustly extract multiple JSON objects from arbitrary text.
    Uses brace-level tracking (not regex) to handle multiline or nested structures.
    """
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
                    data = json.loads(candidate)
                    objs.append(data)
                except json.JSONDecodeError:
                    pass
    return objs


def interpret_intent(user_input: str) -> list[dict]:
    """
    Use the LLM to convert natural language into one or more JSON commands.
    Handles compound commands like 'scale X and restart Y'.
    Ensures proper namespace assignment and valid MCP tool names.
    """

    # Describe tools for LLM
    tool_descriptions = "\n".join(
        f"- {name}: {info.get('doc', '').strip() or info.get('signature', '')}"
        for name, info in TOOLS_INFO.items()
    )

    system_prompt = (
        "You are a command translator for a Kubernetes management agent.\n"
        "Convert user input into one or more JSON commands for the MCP server.\n"
        "Output must be raw JSON only — no markdown, no text, no code fences.\n"
        "Each command must be a valid JSON object with 'tool' and 'args'.\n"
        "Available tools and their arguments:\n"
        f"{tool_descriptions}\n\n"
        'Never go out of the scope of parameters presented in the tool descriptions. The arguments must match the tool signatures exactly.\n'
        "If namespace is not given, but you see it as a parameter in tool description, set it to 'default'.(be careful with namespace functions, as the only argument they recieve is namespace itself,one example is given)\n"
        "If namespace is not required, do not include it in args.\n"
        "Examples:\n"
        '{"tool": "list_pods", "args": {"namespace": "default"}}\n'
        '{"tool": "delete_namespace", "args": {"namespace": "ns_name"}}\n'
        '{"tool": "scale_deployment", "args": {"deployment_name": "nginx", "replicas": 4, "namespace": "default"}}\n'
        '{"tool": "restart_deployment", "args": {"deployment_name": "cicd", "namespace": "default"}}\n'
        '{"tool": "get_nodes", "args": {}}\n'

        'Finally, if one argument in a tool is given multiple values, such as listing pods in multiple namespaces, you should generate separate commands for each value.\n'
    )

    full_prompt = f"{system_prompt}\nUser: {user_input}\nCommand:"
    llm_output = ask_llm(full_prompt).strip()

    # Extract JSON objects safely
    extracted = extract_json_objects(llm_output)

    commands = []
    for data in extracted:
        if not isinstance(data, dict) or "tool" not in data:
            continue

        # Ensure args dict exists
        if "args" not in data or not isinstance(data["args"], dict):
            data["args"] = {}

        # Ensure namespace exists
        if tool_requires_namespace(data["tool"]):
            if "namespace" not in data["args"] or not data["args"]["namespace"]:
                data["args"]["namespace"] = "default"
        else:
            data["args"].pop("namespace", None)

        # Only include known tools
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
    """Send parsed JSON command to MCP server and return the result."""
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
    """Main REPL loop for user interaction."""
    global TOOLS_INFO

    print("Agent initializing...")
    ollama_warmup()

    TOOLS_INFO = get_tool_definitions()
    if not TOOLS_INFO:
        print("[Agent]  No tools retrieved.")
    else:
        print(f"[Agent]  Loaded {len(TOOLS_INFO)} tools from MCP.\n")

    print("Agent ready. Type commands ('exit' to quit, 'show tools' to list tools):\n")

    while True:
        user_input = input("> ").strip()
        if user_input.lower() in ("exit", "quit"):
            print("Exiting agent.")
            break

        if user_input.lower() in ("show tools", "list tools"):
            for name, info in TOOLS_INFO.items():
        # info is already the signature dict
                args_desc = ", ".join(f"{k}: {v}" for k, v in info.items())
                print(f"- {name}: {args_desc}")
            continue

        commands = interpret_intent(user_input)
        if not commands:
            continue

        for cmd in commands:
            print(f"[Agent] Executing: {cmd['tool']} {cmd['args']}")
            result = call_mcp(cmd)
            print(json.dumps(result, indent=2))


if __name__ == "__main__":
    run_agent()
