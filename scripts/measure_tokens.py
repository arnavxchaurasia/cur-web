#!/usr/bin/env python3
import os
import sys
import json
import glob
import re
import csv
from collections import defaultdict
import datetime

def load_data_dir():
    data_dir = "/tmp/cur-web-data" # fallback
    if os.path.exists(".env"):
        with open(".env", "r") as f:
            for line in f:
                if line.strip().startswith("DATA_DIR="):
                    data_dir = line.strip().split("=", 1)[1].strip('"').strip("'")
                    break
    return data_dir

def get_recent_job(jobs_dir, job_id=None):
    if job_id:
        p = os.path.join(jobs_dir, job_id)
        if os.path.isdir(p):
            return job_id, p
        else:
            print(f"Error: Job directory {p} not found.")
            sys.exit(1)
            
    job_dirs = glob.glob(os.path.join(jobs_dir, "*"))
    if not job_dirs:
        print(f"No jobs found in {jobs_dir}")
        sys.exit(1)
    
    job_dirs = [d for d in job_dirs if os.path.isdir(d)]
    if not job_dirs:
        print(f"No jobs found in {jobs_dir}")
        sys.exit(1)
        
    job_dirs.sort(key=lambda x: os.path.getmtime(x), reverse=True)
    latest_job_dir = job_dirs[0]
    return os.path.basename(latest_job_dir), latest_job_dir

def scan_conv_ids_from_log(job_dir):
    conv_ids = set()
    internal_log = os.path.join(job_dir, "agy-internal.log")
    if os.path.exists(internal_log):
        with open(internal_log, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            matches = re.findall(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', content, re.IGNORECASE)
            for m in matches:
                conv_ids.add(m.lower())
    return conv_ids

def find_conv_ids_by_time(brain_dir, start_time, end_time):
    conv_ids = set()
    transcripts = glob.glob(os.path.join(brain_dir, "*/.system_generated/logs/transcript_full.jsonl"))
    for path in transcripts:
        mtime = os.path.getmtime(path)
        if start_time <= mtime <= end_time:
            conv_id = path.split("/")[-4]
            conv_ids.add(conv_id.lower())
    return conv_ids

def parse_ts(ts_str):
    if not ts_str:
        return None
    try:
        # Ensure UTC timezone offset is present so .timestamp() resolves timezone correctly
        if ts_str.endswith("Z"):
            ts_str = ts_str.replace("Z", "+00:00")
        elif "+" not in ts_str and "-" not in ts_str[-6:]:
            ts_str += "+00:00"
        return datetime.datetime.fromisoformat(ts_str).timestamp()
    except Exception:
        return None

def get_step_details(step):
    step_type = step.get("type", "UNKNOWN")
    tool_calls = step.get("tool_calls", []) or []
    if tool_calls:
        tc = tool_calls[0]
        name = tc.get("name", "")
        args = tc.get("args", {}) or {}
        if name == "view_file":
            return f"view_file: {args.get('AbsolutePath', '')}"
        elif name == "run_command":
            return f"run_command: {args.get('CommandLine', '')}"
        elif name == "grep_search":
            return f"grep_search: Query='{args.get('Query', '')}' Path='{args.get('SearchPath', '')}'"
        elif name == "list_dir":
            return f"list_dir: {args.get('DirectoryPath', '')}"
        return f"tool_call: {name}"
        
    content = step.get("content", "") or ""
    thinking = step.get("thinking", "") or ""
    
    if step_type == "USER_INPUT":
        req = content.replace("<USER_REQUEST>", "").replace("</USER_REQUEST>", "").strip()
        req_line = req.split("\n")[0] if req else ""
        return f"user_request: {req_line[:120]}"
    elif step_type == "PLANNER_RESPONSE" and thinking:
        think_line = thinking.replace("\n", " ").strip()
        return f"model_thinking: {think_line[:120]}..."
    
    clean_content = content.replace("\n", " ").strip()
    return clean_content[:120]

def analyze_conversation(brain_dir, conv_id):
    path = os.path.join(brain_dir, conv_id, ".system_generated/logs/transcript_full.jsonl")
    if not os.path.exists(path):
        return None
        
    conv_chars = 0
    conv_steps = 0
    conv_types = defaultdict(int)
    first_prompt = "Unknown"
    steps_list = []
    
    active_messages = []
    min_ts = None
    max_ts = None
    
    temp_steps = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                step = json.loads(line)
                temp_steps.append(step)
            except Exception:
                continue

    for step in temp_steps:
        conv_steps += 1
        created_at = step.get("created_at")
        ts = parse_ts(created_at)
        if ts:
            if min_ts is None or ts < min_ts:
                min_ts = ts
            if max_ts is None or ts > max_ts:
                max_ts = ts
                
        content = step.get("content", "") or ""
        thinking = step.get("thinking", "") or ""
        
        # Track character counts including serialized tool calls
        chars = len(content) + len(thinking)
        tool_calls = step.get("tool_calls", []) or []
        if tool_calls:
            chars += len(json.dumps(tool_calls))
            
        step_type = step.get("type", "UNKNOWN")
        
        if step_type == "CHECKPOINT":
            active_messages = [content]
            est_tokens = 0
        elif step_type in ["USER_INPUT", "VIEW_FILE", "RUN_COMMAND", "LIST_DIRECTORY", "GREP_SEARCH", "PLANNER_RESPONSE"]:
            active_messages.append(content + thinking + json.dumps(tool_calls))
            
            if step_type == "PLANNER_RESPONSE":
                # Input context size is the sum of all elements before this planner response
                input_chars = sum(len(msg) for msg in active_messages[:-1])
                output_chars = len(active_messages[-1])
                
                input_tok = input_chars // 4
                output_tok = output_chars // 4
                est_tokens = input_tok + output_tok
                
                conv_types["INPUT_CONTEXT"] += input_chars
                conv_types["OUTPUT_GENERATED"] += output_chars
            else:
                est_tokens = 0
                conv_types[step_type] += chars
        else:
            est_tokens = 0
            conv_types[step_type] += chars

        details = get_step_details(step)
        steps_list.append({
            "step_index": step.get("step_index", conv_steps - 1),
            "step_type": step_type,
            "details": details,
            "characters": chars,
            "estimated_tokens": est_tokens
        })
        
        conv_chars += chars
        
        if conv_steps == 1:
            req = step.get("content", "") or ""
            if "Run ONLY Phase" in req:
                first_prompt = req.split("\n")[0] + " ... " + req.split("\n")[1][:80]
            else:
                first_prompt = req[:100].replace("\n", " ")
                
    return {
        "id": conv_id,
        "chars": conv_chars,
        "steps": conv_steps,
        "types": conv_types,
        "prompt": first_prompt,
        "min_ts": min_ts,
        "max_ts": max_ts,
        "steps_list": steps_list
    }

def main():
    data_dir = load_data_dir()
    jobs_dir = os.path.join(data_dir, "jobs")
    
    target_job_id = sys.argv[1] if len(sys.argv) > 1 else None
    job_id, job_dir = get_recent_job(jobs_dir, target_job_id)
    print(f"Target Job: {job_id}")
    print(f"Job Directory: {job_dir}")
    
    cust_txt = os.path.join(job_dir, "customer_name.txt")
    val_report = os.path.join(job_dir, "validation_report.json")
    
    job_start_time = os.path.getmtime(cust_txt) if os.path.exists(cust_txt) else os.path.getmtime(job_dir) - 600
    job_end_time = os.path.getmtime(val_report) if os.path.exists(val_report) else os.path.getmtime(job_dir)
    
    # 2. Bound search window strictly to job run duration (+/- 120 seconds buffer)
    start_time = job_start_time - 120
    end_time = job_end_time + 120
    
    # Brain directory
    brain_dir = os.path.expanduser("~/.gemini/antigravity-cli/brain")
    
    log_conv_ids = scan_conv_ids_from_log(job_dir)
    time_conv_ids = find_conv_ids_by_time(brain_dir, start_time, end_time)
    
    all_conv_ids = log_conv_ids.union(time_conv_ids)
    print(f"Found {len(all_conv_ids)} conversations linked to this job run.")
    print("=" * 70)
    
    overall_chars = 0
    overall_steps = 0
    overall_types = defaultdict(int)
    
    analyzed_convs = []
    
    for cid in all_conv_ids:
        res = analyze_conversation(brain_dir, cid)
        if res:
            if res["min_ts"] and not (start_time <= res["min_ts"] <= end_time):
                continue
            analyzed_convs.append(res)
            
    # Sort by modification time
    analyzed_convs.sort(key=lambda x: x["min_ts"] if x["min_ts"] else 0)
    
    csv_rows = []
    
    for c in analyzed_convs:
        dt = datetime.datetime.fromtimestamp(c["min_ts"]).strftime('%Y-%m-%d %H:%M:%S') if c["min_ts"] else "Unknown"
        print(f"Conversation: {c['id']} ({dt})")
        print(f"  Prompt: {c['prompt']}")
        print(f"  Total Chars: {c['chars']:,} chars")
        print(f"  Steps: {c['steps']}")
        for k, v in sorted(c["types"].items(), key=lambda x: x[1], reverse=True):
            print(f"    - {k}: {v:,} chars (~{v//4:,} tokens)")
        print("-" * 70)
        
        overall_chars += c["chars"]
        overall_steps += c["steps"]
        for k, v in c["types"].items():
            overall_types[k] += v
            
        for step in c["steps_list"]:
            csv_rows.append({
                "job_id": job_id,
                "conversation_id": c["id"],
                "step_index": step["step_index"],
                "step_type": step["step_type"],
                "details": step["details"],
                "characters": step["characters"],
                "estimated_tokens": step["estimated_tokens"]
            })

    csv_file_path = os.path.join(job_dir, f"token_usage_breakdown_{job_id}.csv")
    csv_legacy_path = os.path.join(job_dir, "token_usage_breakdown.csv")
    
    fields = ["job_id", "conversation_id", "step_index", "step_type", "details", "characters", "estimated_tokens"]
    
    for path_to_write in (csv_file_path, csv_legacy_path):
        try:
            with open(path_to_write, "w", newline="", encoding="utf-8") as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=fields)
                writer.writeheader()
                writer.writerows(csv_rows)
            print(f"[Success] Token usage breakdown saved to: {path_to_write}")
        except Exception as e:
            print(f"Error writing CSV file: {e}")

    duration_sec = job_end_time - job_start_time
    minutes = int(duration_sec // 60)
    seconds = int(duration_sec % 60)
    duration_str = f"{minutes}m {seconds}s ({int(duration_sec)} seconds)"

    total_input = overall_types["INPUT_CONTEXT"] // 4
    total_output = overall_types["OUTPUT_GENERATED"] // 4

    print("\n" + "=" * 70)
    print("JOB ACCURATE TOKEN & TIMING SUMMARY")
    print("=" * 70)
    print(f"Total Conversations : {len(analyzed_convs)}")
    print(f"Total Steps Executed: {overall_steps}")
    print(f"Start Time          : {datetime.datetime.fromtimestamp(job_start_time).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"End Time            : {datetime.datetime.fromtimestamp(job_end_time).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Total Job Runtime   : {duration_str}")
    print("-" * 70)
    print(f"Stateful Input Tokens  : {total_input:,} tokens")
    print(f"Stateful Output Tokens : {total_output:,} tokens")
    print(f"Total Estimated Tokens : {total_input + total_output:,} tokens")
    print("=" * 70)

if __name__ == "__main__":
    main()
