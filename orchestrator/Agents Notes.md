## **Agent 1: Gemini CLI–based requirements_gathering agent:**

### 1. Initialize understanding of an existing project:
```
maestro install  
maestro run skill specs-setup # scans an existing codebase and establishes project specifications.
maestro kg index # builds or refreshes Maestro’s code knowledge graph
```

### 2. Search old documentation and accumulated project knowledge:
```
maestro search "<user query>" --all --json   # main

# other possible ones
maestro search "user authentication" --all --json
maestro search "token rotation" --type spec --json
maestro search "AuthService" --code --json
maestro search "authentication" --type knowhow --json
maestro search "login" --type issue --json
```

### 3. Possible tools to Use:
| Tool              | Provided by | Purpose                                                |
| ----------------- | ----------- | ------------------------------------------------------ |
| `read_file`       | Maestro MCP | Read one project file                                  |
| `read_many_files` | Maestro MCP | Read multiple project files                            |
| `write_file`      | Maestro MCP | Create or overwrite a file                             |
| `edit_file`       | Maestro MCP | Modify an existing file                                |
| `team_msg`        | Maestro MCP | Communicate within Maestro’s team workflow             |
| `store_knowhow`   | Maestro MCP | Store reusable knowledge in Maestro’s knowledge system |

### 3. Gemini delegate interaction
A Gemini worker can be started asynchronously:
```
maestro delegate \
  "Gather requirements for the requested feature. Inspect project knowledge and code. Ask only for unresolved material decisions." \
  --to gemini \
  --role analyze \
  --mode analysis \
  --cd . \
  --async
```

### 4. User In the Loop for QnA:
Maestro returns an execution ID such as `gem-143022-a7f2`. Its lifecycle includes an input_required state. The user’s answer can then be injected into the running Gemini process:
```
maestro delegate status gem-143022-a7f2

maestro delegate message gem-143022-a7f2 \
  "Decision: preserve backward compatibility and use JWT access tokens."
```

### 5. Output
Write the context.md file from the gemini's output

---
## **Agent 2: Codex or Claude Code CLI-based planner agent:**
Maestro Flow can run only its planning stage, without implementing the plan.
However, in the current Maestro architecture, plan is an internal workflow step dispatched through /maestro; it is not a separate /maestro-plan slash command. The coordinator recognizes a planning-only request and creates a single-step plan chain.
A non-interactive Claude Code planner invocation could be:
```claude -p '/maestro Run only the plan step. Read requirements from .workflow/handoffs/requirements_gathering/context.md. Produce the planning artifacts and stop without executing.'```
For Codex, first verify that the plan skill was installed:
```maestro skills --platform codex```
If plan appears, the Codex-side invocation would follow the installed-skill interface, for example:
```codex exec '$plan --dir .workflow/handoffs/requirements_gathering'```
The outputs of this planner agent will be plan.json + TASK-*.json files

---
## **Agent 3: Gemini CLI-based orchestrator agent:**
The outputs of the planner agent (plan.json, TASK-*.json) are the inputs of the Orchestrator agent. This reads the plan's and the tasks' files and runs the gemini cli-based coding subagents' `waves`. When one wave is completed it dispatches the next until all tasks have been completed. 
If there is more than one subagent in a wave, each of these coding subagents work in a separate git worktree. Each of the coding agents will have access to the knowledge graph, knowledge base markdown files and the learnings markdown file from maestro-flow, which they will use to generate the response. Also, if it finds any new findings, the coding agent should report it to the learnings file.

---
## **Agent 4: Gemini CLI-based merger agent:**
After each `wave`, a merger agent merges the git worktrees of each coding agent, and if there is any conflict the merger agent resolves it. Also, if it finds any new findings, the merger agent should report it to the learnings file.

---
## **Agent 5: Gemini CLI-based reviewer agent:**
The output of the Merger Agent should be a Reviewer Agent, which reviews it against the original context and the tasks files. This agent is claude code or codex cli based. If needed, adds new implementation comments that are given back to the same coding subagent (same session id), its worktree's work is reverted/deleted, and the process cycles. Otherwise, the orchestrator continues with the next wave. Also, if it finds any new findings, the coding agent should report it to the learnings file.

---
## **Agent 6: Gemini CLI-based supervisor agent:**
When all waves are completed, the Supervisor Agent reviews the final code with the original context.md and checks if all the requirements have been fullfilled. This agent is claude code or codex cli based. If not satisfactory, it provides comments to planner agent and the process cycles. Also, if it finds any new findings, the coding agent should report it to the learnings file.


**NOTE: Needed Files/Records:**
#### `user_choices.md` File
*Created and appended by the agent*
*Record only explicit choices made by the user:*
- Choices stated in the original query.
- Answers provided during clarification Q&A.
- Corrections to previous choices.
- Explicitly confirmed constraints or preferences.
*Do not store:*
- Agent assumptions.
- Unanswered questions.
- Information inferred from the code.
- Every functional requirement.
- Implementation decisions made by other agents.
*For example:*
* ```The system must support English and Urdu.``` This is an explicit user requirement and may be recorded.
* ```The user probably wants PostgreSQL.``` This is an assumption and must not be recorded.
