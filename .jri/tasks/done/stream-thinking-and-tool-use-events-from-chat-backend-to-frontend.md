---
title: Stream thinking and tool-use events from chat backend to frontend
priority: 0
assignee: Nicolás Pujia
created: '2026-03-21'
acceptance_criteria:
- When Ralphy thinks, the frontend shows a collapsible 'Thinking...' section with
  the thinking text in monospace
- When Ralphy uses a tool (e.g., bd create), the tool name appears as a step in the
  thinking/working section
- The text response still renders below the thinking section as before
- No events are silently dropped—all content block types are forwarded to the frontend
- The thinking indicator ('Ralphy is thinking...') shows while thinking is in progress
---

The backend chat streaming in app/routers/chat.py (_stream_claude function) only handles 'text' content blocks and 'text_delta' deltas from Claude CLI's stream-json output. It silently drops thinking blocks and tool_use blocks. The frontend (templates/project.html) already has scaffolding to display these (the rebuildMsg function with thinkingSteps array and collapsible details element) but never receives the events.

WHAT TO CHANGE in app/routers/chat.py, function _stream_claude:

1. In the 'assistant' message type handler (line 177-183), add handling for thinking and tool_use content blocks:
   - For blocks with type 'thinking': emit SSE event {"type": "thinking", "content": block["thinking"]}
   - For blocks with type 'tool_use': emit SSE event {"type": "tool_use", "name": block["name"], "input": block.get("input", {})}

2. In the 'content_block_delta' handler (line 185-189), add handling for thinking_delta:
   - For delta type 'thinking_delta': emit SSE event {"type": "thinking", "content": delta["thinking"]}

3. Also handle the 'content_block_start' message type from stream-json:
   - If the content_block has type 'tool_use': emit SSE event {"type": "tool_use", "name": content_block["name"]}
   - If the content_block has type 'thinking': emit SSE event {"type": "thinking_start"}

WHAT TO CHANGE in templates/project.html (the frontend JS):

4. In the pump() function's line processing (around line 982-1008), update event handling:
   - For evt.type === 'thinking': append evt.content to a thinkingText string (not thinkingSteps array), rebuild message with thinking collapsible
   - For evt.type === 'thinking_start': reset thinkingText, show thinking indicator
   - For evt.type === 'tool_use': push to thinkingSteps with the tool name, rebuild message

5. Update the rebuildMsg() function to render thinking text as a single collapsible block:
   - If thinkingText is non-empty, show a <details> with summary 'Thinking...' and the thinking text in a monospace div
   - If thinkingSteps has entries, show each tool use step below the thinking
