import json
import sys
import pathlib
import importlib.util
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
from mcp.server.fastmcp import FastMCP

# Import utility modules
import validators
import common
import k8s_utils

# Initialize MCP
mcp = FastMCP("k8s-generative")

# ---- Load and register all tools from tools/ directory ----
def import_and_register_tools(dir_path: str, mcp_instance: FastMCP):
    """Import every .py file in tools/ and register its tools with FastMCP."""
    dir_path = pathlib.Path(dir_path)
    tools_dict = {}

    for file in dir_path.glob("*.py"):
        if file.name == "__init__.py":
            continue

        spec = importlib.util.spec_from_file_location(file.stem, file)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[file.stem] = mod
        spec.loader.exec_module(mod)

        if hasattr(mod, "register_tools"):
            module_tools = mod.register_tools(mcp_instance)

            # Some modules return dicts, others just the same MCP instance
            if isinstance(module_tools, dict):
                for name, func in module_tools.items():
                    if name in tools_dict:
                        print(f"Tool already exists: {name}")
                    tools_dict[name] = func

    # Fallback: also collect decorated tools directly
    for mod_name, mod in sys.modules.items():
        if mod_name.startswith("tools."):
            for attr in dir(mod):
                obj = getattr(mod, attr)
                if callable(obj) and getattr(obj, "_is_mcp_tool", False):
                    if obj.__name__ in tools_dict:
                        print(f"Tool already exists: {obj.__name__}")
                    tools_dict[obj.__name__] = obj

    return tools_dict


tools_path = pathlib.Path(__file__).parent / "tools"
tools_dict = import_and_register_tools(tools_path, mcp)

# ---- HTTP server definition ----
class MCPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed_path = urlparse(self.path)
        if parsed_path.path == "/tools":
            tools_info = {}
            for name, func in tools_dict.items():
                sig = getattr(func, "__tool_signature__", {})
                tools_info[name] = sig
            self._send_response(200, {"tools": tools_info})


    def _send_response(self, code=200, data=None):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if data:
            self.wfile.write(json.dumps(data, indent=2, default=str).encode())

    def do_POST(self):
        parsed_path = urlparse(self.path)
        if parsed_path.path != "/run":
            return self._send_response(404, {"error": "Not found"})

        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length))
        except Exception as e:
            return self._send_response(400, {"error": f"Invalid JSON: {e}"})

        tool_name = payload.get("tool")
        args = payload.get("args", {}) or {}

        if not tool_name or tool_name not in tools_dict:
            return self._send_response(400, {"error": f"Tool '{tool_name}' not found"})

        try:
            result = tools_dict[tool_name](**args)
            self._send_response(200, result)
        except Exception as e:
            import traceback
            self._send_response(500, {"error": str(e), "trace": traceback.format_exc()[:500]})



# ---- Entry point ----
if __name__ == "__main__":
    addr = ("0.0.0.0", 8000)
    print(f"MCP HTTP server running on http://{addr[0]}:{addr[1]}/run")
    HTTPServer(addr, MCPRequestHandler).serve_forever()
