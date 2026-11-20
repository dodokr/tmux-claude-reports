#!/usr/bin/env bash
# Open the picker on an isolated tmux server with made-up reports, written by the real hook
# from synthetic transcripts. For trying keybindings or taking screenshots without real data.
#
#   scripts/demo.sh                 attach to the demo (q to quit, then the server is killed)
#   scripts/demo.sh --capture       print the picker as text and exit
#                                   (CAPTURE_ANSI=1 keeps colors, for scripts/ansi2svg.py)
#   scripts/demo.sh --page OUT.png  screenshot one report page in headless Chrome

set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
dir="$(mktemp -d)"
conf="$dir/tmux.conf"
sock=claude-reports-demo
T() { tmux -L "$sock" -f "$conf" "$@"; }
server=""
trap '[[ -n $server ]] && kill "$server" 2>/dev/null; T kill-server 2>/dev/null || true; rm -rf -- "$dir"' EXIT

printf 'set -g base-index 1\nset -g pane-base-index 1\n' >"$conf"
mkdir "$dir/state" "$dir/tasks" "$dir/transcripts"
export CLAUDE_REPORTS_DIR="$dir/state" CLAUDE_TASKS_DIR="$dir/tasks"
T new-session -d -s _ -x 140 -y 20 sleep 3600
export TMUX="$(T display -p '#{socket_path}'),0,0"

# One synthetic transcript per session: a few turns, each with an edit and a final reply.
python3 - "$dir/transcripts" <<'EOF'
import json, sys
from datetime import datetime, timedelta, timezone
out = sys.argv[1]
t0 = datetime.now(timezone.utc) - timedelta(minutes=25)
sessions = {
    "api": ("Add rate limiting to /login", "~/code/api", [
        ("Add rate limiting to the /login endpoint, 5 attempts per minute per IP.",
         "src/middleware/ratelimit.ts",
         "Added a sliding-window rate limiter in front of `/login`.\n\n"
         "- **Limit:** 5 attempts per minute per IP, then `429` with a `Retry-After` header.\n"
         "- **Storage:** Redis, so it holds across the 3 API replicas.\n"
         "- **Tests:** 4 new cases in `ratelimit.test.ts`, all passing.\n\n"
         "Successful logins don't count toward the limit."),
        ("Does it also cover the password reset endpoint?", "src/routes/auth.ts",
         "It does now. `/password-reset` uses the same limiter with its own key, so a burst of "
         "reset requests doesn't lock someone out of logging in.\n\n```ts\n"
         "router.post('/password-reset', rateLimit({ key: 'reset', max: 5 }), resetHandler);\n```"),
    ]),
    "webshop": ("Fix flaky checkout e2e test", "~/code/webshop", [
        ("The checkout e2e test fails about 1 in 5 runs on CI. Find out why and fix it.",
         "e2e/checkout.spec.ts",
         "The test clicked **Pay** before the shipping options finished loading, so the order "
         "sometimes went through without a shipping method.\n\n"
         "I replaced the fixed `wait(500)` with a wait for the shipping request to finish. "
         "50 runs in a row passed locally."),
    ]),
    "dotfiles": ("Migrate nvim config to lazy.nvim", "~/dotfiles", [
        ("Move my neovim plugins from packer to lazy.nvim.", "nvim/lua/plugins/init.lua",
         "Moved all 23 plugins to lazy.nvim specs, one file per plugin under `lua/plugins/`. "
         "Startup went from 180 ms to 42 ms because most plugins now load on their command or "
         "filetype.\n\nRemoved `packer_compiled.lua`; run `:Lazy sync` once."),
    ]),
    "blog": ("Draft post on tmux workflows", "~/notes/blog", [
        ("Draft a blog post about my tmux workflow with popups.", "posts/tmux-popups.md",
         "Drafted `posts/tmux-popups.md` (about 1,200 words): why popups, three examples "
         "(session switcher, Claude board, reports), and the config for each."),
    ]),
    "thesis": ("Summarize related work chapter", "~/school/thesis", [
        ("Summarize chapter 2 in half a page.", "notes/ch2-summary.md",
         "Wrote a 280-word summary to `notes/ch2-summary.md`. It groups the 14 cited papers "
         "into three approaches and ends with the gap the thesis addresses."),
    ]),
}
for name, (title, cwd, turns) in sessions.items():
    cwd = cwd.replace("~", "/home/you")
    recs = [{"type": "ai-title", "aiTitle": title}]
    t = 0
    for prompt, path, reply in turns:
        base = {"cwd": cwd, "gitBranch": "main", "entrypoint": "cli"}
        stamp = lambda m: (t0 + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        recs.append({**base, "type": "user", "timestamp": stamp(t), "origin": {"kind": "human"},
                     "message": {"role": "user", "content": prompt}})
        recs.append({**base, "type": "assistant", "timestamp": stamp(t + 1), "message": {
            "role": "assistant", "content": [{"type": "tool_use", "name": "Edit",
                                              "input": {"file_path": f"{cwd}/{path}"}}]}})
        recs.append({**base, "type": "user", "timestamp": stamp(t + 2),
                     "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}})
        recs.append({**base, "type": "assistant", "timestamp": stamp(t + 4), "message": {
            "role": "assistant", "content": [{"type": "text", "text": reply}]}})
        t += 6
    with open(f"{out}/{name}.jsonl", "w") as f:
        f.write("".join(json.dumps(r) + "\n" for r in recs))
EOF

n=0
add() { # name age-seconds unread(1/0) open(1/0)
    local pane sid pid start
    n=$((n + 1))
    sid="$(printf '%08d-0000-4000-8000-%012d' "$n" "$n")"
    pane=$(T new-session -d -P -F '#{pane_id}' -s "$1" -x 140 -y 20 sleep 3600)
    printf '{"session_id":"%s","transcript_path":"%s","hook_event_name":"Stop"}' \
        "$sid" "$dir/transcripts/$1.jsonl" | TMUX_PANE="$pane" "$root/claude-reports" hook
    python3 - "$dir/state/$sid.json" "$2" "$3" <<'EOF'
import json, sys, time
p, age, unread = sys.argv[1], int(sys.argv[2]), sys.argv[3] == "1"
m = json.load(open(p)); m["ended"] = time.time() - age; m["unread"] = unread
json.dump(m, open(p, "w"))
EOF
    if [[ $4 == 1 ]]; then # open: record it the way claude-tasks does
        pid=$(T display -p -t "$pane" '#{pane_pid}')
        start=$(cut -d')' -f2- "/proc/$pid/stat" | awk '{print $20}')
        printf 'done\t0\t/x\t%s\t%s %s\n' "$sid" "$pid" "$start" >"$dir/tasks/${pane#%}"
    else
        T kill-session -t "$1"
    fi
}

add api 40 1 1
add webshop 300 1 0
add dotfiles 3900 0 1
add blog 7200 0 0
add thesis 90000 0 0

if [[ "${1:-}" == --page ]]; then
    out="$(realpath -m "${2:?--page needs an output file}")"
    port=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
    CLAUDE_REPORTS_PORT=$port "$root/claude-reports" serve & server=$!
    for _ in $(seq 50); do curl -s "http://127.0.0.1:$port/ping" >/dev/null && break; sleep 0.1; done
    token=$(cat "$dir/state/token")
    google-chrome-stable --headless=new --disable-gpu --user-data-dir="$dir/chrome" \
        --window-size=1000,900 --force-prefers-color-scheme=dark \
        --blink-settings=preferredColorScheme=0 --virtual-time-budget=3000 \
        --screenshot="$out" "http://127.0.0.1:$port/r/00000001-0000-4000-8000-000000000001?t=$token" 2>/dev/null
    exit
fi

T new-session -d -s picker -x 180 -y 16 "CLAUDE_REPORTS_DIR=$dir/state CLAUDE_TASKS_DIR=$dir/tasks $root/claude-reports"

if [[ "${1:-}" == --capture ]]; then
    sleep 1.5
    T capture-pane -p ${CAPTURE_ANSI:+-e} -t picker
else
    TMUX='' T attach -t picker
fi
