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
    # Load DATA_DIR from .env
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
            
    # Find most recently modified job dir
    job_dirs = glob.glob(os.path.join(jobs_dir, "*"))
    if not job_dirs:
        print(f"No jobs found in {jobs_dir}")
        sys.exit(1)
    
    # Filter to actual directories
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
            # Match UUIDs
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

def extract_tool_info(step):
    tool_calls = step.get("tool_calls", []) or []
    if not tool_calls:
        return None
        
    tc = tool_calls[0]
    name = tc.get("name", "")
    args = tc.get("args", {}) or {}
    
    details = ""
    if name == "view_file":
        details = args.get("AbsolutePath", "")
    elif name == "run_command":
        details = args.get("CommandLine", "")
    elif name == "grep_search":
        details = f"Query='{args.get('Query', '')}' Path='{args.get('SearchPath', '')}'"
    elif name == "list_dir":
        details = args.get("DirectoryPath", "")
    else:
        # Serialise args
        details = json.dumps(args)
        
    return name, details

def analyze_tool_calls(brain_dir, conv_id):
    path = os.path.join(brain_dir, conv_id, ".system_generated/logs/transcript_full.jsonl")
    if not os.path.exists(path):
        return None
        
    tool_counts = defaultdict(int)
    first_prompt = "Unknown"
    steps_count = 0
    
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                step = json.loads(line)
            except Exception:
                continue
                
            steps_count += 1
            if steps_count == 1:
                req = step.get("content", "") or ""
                if "Run ONLY Phase" in req:
                    first_prompt = req.split("\n")[0] + " ... " + req.split("\n")[1][:80]
                else:
                    first_prompt = req[:100].replace("\n", " ")
            
            tool_info = extract_tool_info(step)
            if tool_info:
                name, details = tool_info
                key = (name, details)
                tool_counts[key] += 1
                
    return {
        "id": conv_id,
        "prompt": first_prompt,
        "tool_counts": tool_counts,
        "mtime": os.path.getmtime(path)
    }

def main():
    data_dir = load_data_dir()
    jobs_dir = os.path.join(data_dir, "jobs")
    
    # Target job
    target_job_id = sys.argv[1] if len(sys.argv) > 1 else None
    
    job_id, job_dir = get_recent_job(jobs_dir, target_job_id)
    print(f"Target Job: {job_id}")
    print(f"Job Directory: {job_dir}")
    
    # Get time window of this job
    job_dir_mtime = os.path.getmtime(job_dir)
    # The start time is approximate - creation of log/progress files
    start_time = job_dir_mtime - 120 # 2 mins buffer
    end_time = datetime.datetime.now().timestamp()
    
    # Brain directory
    brain_dir = os.path.expanduser("~/.gemini/antigravity-cli/brain")
    
    # 1. Scan from log file
    log_conv_ids = scan_conv_ids_from_log(job_dir)
    
    # 2. Scan by time
    time_conv_ids = find_conv_ids_by_time(brain_dir, start_time, end_time)
    
    # Combined list
    all_conv_ids = log_conv_ids.union(time_conv_ids)
    
    print(f"Found {len(all_conv_ids)} conversations linked to this job run.")
    print("=" * 80)
    
    analyzed_convs = []
    for cid in all_conv_ids:
        res = analyze_tool_calls(brain_dir, cid)
        if res:
            analyzed_convs.append(res)
            
    # Sort by modification time
    analyzed_convs.sort(key=lambda x: x["mtime"])
    
    csv_rows = []
    overall_calls = defaultdict(int)
    
    for c in analyzed_convs:
        dt = datetime.datetime.fromtimestamp(c["mtime"]).strftime('%Y-%m-%d %H:%M:%S')
        print(f"Conversation: {c['id']} ({dt})")
        print(f"  Prompt: {c['prompt']}")
        
        sorted_tools = sorted(c["tool_counts"].items(), key=lambda x: x[1], reverse=True)
        if not sorted_tools:
            print("  No tool/function calls executed in this conversation.")
        else:
            print("  Function Calls:")
            for (name, details), count in sorted_tools:
                # Truncate details for console output
                print(f"    - {name:<15} (called {count:>2}x): {details[:90]}")
                overall_calls[(name, details)] += count
                
                csv_rows.append({
                    "job_id": job_id,
                    "conversation_id": c["id"],
                    "tool_name": name,
                    "details": details,
                    "call_count": count
                })
        print("-" * 80)

    # Write to CSV
    csv_file_path = os.path.join(job_dir, "tool_calls_frequency.csv")
    fields = ["job_id", "conversation_id", "tool_name", "details", "call_count"]
    
    try:
        with open(csv_file_path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fields)
            writer.writeheader()
            writer.writerows(csv_rows)
        print(f"\n[Success] Function call frequency saved to CSV:")
        print(f"  {csv_file_path}")
    except Exception as e:
        print(f"Error writing CSV file: {e}")

    # Print summary of repeated calls
    print("\n" + "=" * 80)
    print("MOST FREQUENT FUNCTION CALLS SUMMARY (POTENTIAL OPTIMIZATIONS)")
    print("=" * 80)
    
    repeated_calls = {k: v for k, v in overall_calls.items() if v > 1}
    if not repeated_calls:
        print("Excellent! No redundant/repeated function calls with exact parameters were detected.")
    else:
        print("The following function calls were executed multiple times with identical arguments:")
        sorted_repeated = sorted(repeated_calls.items(), key=lambda x: x[1], reverse=True)
        for (name, details), count in sorted_repeated[:15]:
            print(f"  {count:>3}x calls to {name:<15} -> {details[:120]}")
            
    print("=" * 80)

if __name__ == "__main__":
    main()
