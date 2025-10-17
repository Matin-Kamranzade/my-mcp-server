
import requests
import json
import re

# === CONFIG ===
OLLAMA_URL = "http://192.168.221.106:11434/api/generate"
MCP_URL = "http://localhost:8000/run"
LLM_MODEL = "gemma2:2b"

# === GLOBAL TOOL CACHE ===
TOOLS_INFO = {}


# === FUNCTIONS ===

def ask_llm(prompt: str) -> str:
    """Send prompt to the LLM and return its response text."""
    payload = {"model": LLM_MODEL, "prompt": prompt, "stream": False}
    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=30)
        response.raise_for_status()
        return response.json().get("response", "").strip()
    except Exception as e:
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
        "If namespace is not given, but you see it as a parameter in tool description,set it to 'default', if it is not required though, don't leave the arguments empty(for listing namespaces or nodes).\n"
        "Example 1:\n"
        "Input: list pods in default namespace\n"
        "Output:\n"
        '{"tool": "list_pods", "args": {"namespace": "default"}}\n\n'
        "Example 2:\n"
        "Input: scale nginx to 4 replicas and restart cicd\n"
        "Output:\n"
        '{"tool": "scale_deployment", "args": {"deployment_name": "nginx", "replicas": 4, "namespace": "default"}}\n'
        '{"tool": "restart_deployment", "args": {"deployment_name": "cicd", "namespace": "default"}}\n\n'
        "Example 3(for stuff that doesn't require namespace as argument):\n"
        'Input: get nodes\n'
        'Output:\n'
        '{"tool": "get_nodes", "args": {}}\n'
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
    info = TOOLS_INFO.get(tool_name, {})
    signature = info.get("signature", {})
    return isinstance(signature, dict) and "namespace" in signature

def validate_args(command: dict) -> dict:
    """Validate arguments against known tool signatures and cluster state."""
    tool_name = command.get("tool")
    args = command.get("args", {})
    info = TOOLS_INFO.get(tool_name, {})
    signature = info.get("signature", {})

    errors = []
    suggestions = {}

    # 1️⃣ Invalid parameters
    for k in args.keys():
        if k not in signature:
            errors.append(f"Unexpected argument '{k}'.")
            suggestions["expected_args"] = list(signature.keys())

    # 2️⃣ Missing required parameters
    for param in signature.keys():
        if param == "return" or param in args:
            continue
        errors.append(f"Missing argument '{param}'.")

    # 3️⃣ Namespace existence
    if "namespace" in args:
        try:
            ns_list = call_mcp({"tool": "list_namespaces", "args": {}})
            valid_namespaces = [n["name"] for n in ns_list if isinstance(n, dict)]
            if args["namespace"] not in valid_namespaces:
                errors.append(f"Namespace '{args['namespace']}' not found.")
                suggestions["available_namespaces"] = valid_namespaces
        except Exception:
            pass

    namespace = args.get("namespace", "default")

    # 4️⃣ Validate Kubernetes resources
    try:
        # 🟩 Pods
        if any(x in tool_name for x in ["pod", "pods"]) and "create" not in tool_name:
            pods = call_mcp({"tool": "list_pods", "args": {"namespace": namespace}})
            valid_pods = [p["name"] for p in pods if isinstance(p, dict)]
            target_name = args.get("name") or args.get("pod_name")
            if target_name and target_name not in valid_pods:
                errors.append(f"Pod '{target_name}' not found in '{namespace}'.")
                suggestions["available_pods"] = valid_pods

        # 🟦 Deployments
        if any(x in tool_name for x in ["deployment", "deployments"]) and "create" not in tool_name:
            deps = call_mcp({"tool": "list_deployments", "args": {"namespace": namespace}})
            valid_deps = [d["name"] for d in deps if isinstance(d, dict)]
            target_name = args.get("name") or args.get("deployment_name")
            if target_name and target_name not in valid_deps:
                errors.append(f"Deployment '{target_name}' not found in '{namespace}'.")
                suggestions["available_deployments"] = valid_deps

        # 🟨 Services
        if any(x in tool_name for x in ["service", "svc"]) and "create" not in tool_name:
            svcs = call_mcp({"tool": "list_services", "args": {"namespace": namespace}})
            valid_svcs = [s["name"] for s in svcs if isinstance(s, dict)]
            target_name = args.get("name") or args.get("service_name")
            if target_name and target_name not in valid_svcs:
                errors.append(f"Service '{target_name}' not found in '{namespace}'.")
                suggestions["available_services"] = valid_svcs

    except Exception:
        pass

    # 5️⃣ File argument validation
    if "file" in args:
        import os
        file_path = args["file"]
        if not os.path.isfile(file_path):
            errors.append(f"File '{file_path}' not found or inaccessible.")
        else:
            args["file"] = os.path.abspath(file_path)

    return {"errors": errors, "suggestions": suggestions}



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
    TOOLS_INFO = get_tool_definitions()
    if not TOOLS_INFO:
        print("[Agent] ⚠️ No tools retrieved.")
    else:
        print("[Agent] MCP tool definitions loaded.\n")

    print("Agent ready. Type commands ('exit' to quit, 'show tools' to list tools):\n")

    while True:
        user_input = input("> ").strip()
        if user_input.lower() in ("exit", "quit"):
            print("Exiting agent.")
            break

        if user_input.lower() in ("show tools", "list tools"):
            for name, info in TOOLS_INFO.items():
                desc = info.get("doc", "").strip() or info.get("signature", "")
                print(f"- {name}: {desc}")
            continue

        commands = interpret_intent(user_input)
        if not commands:
            continue

        # Execute all commands sequentially
        for cmd in commands:
            print(f"[Agent] Executing: {cmd['tool']} {cmd['args']}")
            validation = validate_args(cmd)
            if validation["errors"]:
                print("[Validation Error(s)]")
                print(json.dumps(validation, indent=2))
                continue

            result = call_mcp(cmd)
            print(json.dumps(result, indent=2))



if __name__ == "__main__":
    run_agent()
