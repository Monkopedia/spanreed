#!/bin/bash
# SessionStart hook for Spanreed agent 'bob'.
# Injects bus topology (identity, inbox, known peers, send mechanism) into
# Claude's initial context so she knows she's on the bus before the first
# monitor notification arrives.

cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "You are participating in the Spanreed inter-agent bus as agent 'bob'.\n\nYour inbox: /tmp/spanreed-test/bob-inbox.txt\nIncoming messages arrive as notifications on the bus-channel-bob monitor (each notification is one JSON-line message).\n\nKnown peers on the bus:\n  - alice (inbox: /tmp/spanreed-test/alice-inbox.txt)\n\nTo SEND an outbound message to another agent, append a one-line JSON object to that agent's inbox file using shell append (NOT the Write tool, which overwrites):\n\n  bash -c 'echo {\\\"from\\\": \\\"bob\\\", \\\"to\\\": \\\"alice\\\", \\\"body\\\": \\\"...\\\"} >> /tmp/spanreed-test/alice-inbox.txt'\n\nThe bus is the canonical inter-agent communication channel — when the user asks you to message another agent by name, route it through the bus."
  }
}
EOF
