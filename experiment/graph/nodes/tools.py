import subprocess

import aiohttp
from langchain_core.tools import tool

WEBHOOK_URL = "https://flow.sokt.io/func/scrioFy1twfh"


@tool
async def send_webhook(final_answer: str) -> str:
    """Send the final completed answer to the webhook. You MUST call this tool once after ALL tasks are fully executed and you have the complete final answer ready. Pass the full consolidated response as final_answer."""
    payload = {"final_answer": final_answer}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(WEBHOOK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                status = resp.status
                body = await resp.text()
                return f"Webhook delivered (HTTP {status}): {body}"
    except Exception as e:
        return f"Webhook failed: {e}"


@tool
def read_file(path: str) -> str:
    """Read the contents of a file at the given path."""
    try:
        with open(path, "r") as f:
            return f.read()
    except Exception as e:
        return f"Error reading file: {e}"


@tool
def write_file(path: str, content: str) -> str:
    """Write content to a file at the given path."""
    try:
        with open(path, "w") as f:
            f.write(content)
        return f"Successfully wrote to {path}"
    except Exception as e:
        return f"Error writing file: {e}"


@tool
def run_shell(command: str) -> str:
    """Run a shell command and return its output. Use for listing files, running scripts, etc."""
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=30
        )
        output = result.stdout or result.stderr
        return output.strip() or "Command ran with no output."
    except subprocess.TimeoutExpired:
        return "Command timed out after 30 seconds."
    except Exception as e:
        return f"Error running command: {e}"


@tool
def list_files(directory: str = ".") -> str:
    """List files and directories at the given path."""
    try:
        result = subprocess.run(
            f"ls -la {directory}", shell=True, capture_output=True, text=True
        )
        return result.stdout.strip()
    except Exception as e:
        return f"Error listing files: {e}"


TOOLS = [send_webhook, read_file, write_file, run_shell, list_files]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}
