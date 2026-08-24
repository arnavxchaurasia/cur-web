#!/usr/bin/env python3
"""
Usage:
  tail -f <session>.jsonl | python3 scripts/logtail.py
  python3 scripts/logtail.py <session>.jsonl
"""
import sys, json, textwrap, re

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
CYAN   = "\033[36m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
BLUE   = "\033[34m"
RED    = "\033[31m"

def wrap(text, width=100, indent="  "):
    lines = text.splitlines()
    out = []
    for line in lines:
        if len(line) <= width:
            out.append(indent + line)
        else:
            out.extend(textwrap.wrap(line, width, initial_indent=indent, subsequent_indent=indent))
    return "\n".join(out)

def fmt_tool_use(name, inp):
    parts = [f"{BOLD}{CYAN}⚙  Tool: {name}{RESET}"]
    if name == "Bash":
        cmd = inp.get("command", "")
        desc = inp.get("description", "")
        if desc:
            parts.append(f"{DIM}   {desc}{RESET}")
        parts.append(f"{DIM}{wrap(cmd.strip(), indent='   $ ')}{RESET}")
    else:
        s = json.dumps(inp, indent=2)
        parts.append(wrap(s, indent="   "))
    return "\n".join(parts)

def fmt_tool_result(content):
    text = ""
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text += block.get("text", "")
    elif isinstance(content, str):
        text = content
    lines = text.strip().splitlines()
    if not lines:
        return f"{DIM}   (empty){RESET}"
    preview = lines[:8]
    suffix = f"\n{DIM}   … {len(lines)-8} more lines{RESET}" if len(lines) > 8 else ""
    return f"{GREEN}" + wrap("\n".join(preview), indent="   ") + f"{RESET}" + suffix

def process(line):
    line = line.strip()
    if not line:
        return
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return

    role = entry.get("type", entry.get("role", ""))
    msg  = entry.get("message", {})
    content = msg.get("content", [])
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]

    for block in content:
        btype = block.get("type", "")
        if btype == "thinking":
            t = block.get("thinking", "").strip()
            if t:
                print(f"\n{DIM}{BLUE}💭 Thinking:{RESET}")
                print(f"{DIM}{wrap(t)}{RESET}")
        elif btype == "text" and role == "assistant":
            t = block.get("text", "").strip()
            if t:
                print(f"\n{BOLD}🤖 Claude:{RESET}")
                print(wrap(t))
        elif btype == "tool_use":
            print(f"\n{fmt_tool_use(block['name'], block.get('input', {}))}")
        elif btype == "tool_result":
            print(f"\n{DIM}   ↳ Result:{RESET}")
            print(fmt_tool_result(block.get("content", "")))

def main():
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            for line in f:
                process(line)
    else:
        for line in sys.stdin:
            process(line)
            sys.stdout.flush()

if __name__ == "__main__":
    main()
