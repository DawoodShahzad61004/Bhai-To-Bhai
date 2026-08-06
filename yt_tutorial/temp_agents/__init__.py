"""A two-agent sandbox: one supervisor, two agents, two files.

The supervisor starts both agents at the same time, waits for both, then reads
what they wrote and says whether they did their job. Each agent runs on either
the Claude Code CLI or the Codex CLI, chosen by TEMP_AGENT_A_BACKEND and
TEMP_AGENT_B_BACKEND in config.py.

Standalone by design: its own prompts, its own directory, its own runners, and
no connection to the graphs in teams/ or the agents in claude_agents/.

    python -m temp_agents.main
"""
