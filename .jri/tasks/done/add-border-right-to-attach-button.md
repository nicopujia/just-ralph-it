---
title: Add border-right to Attach button
priority: 2
assignee: Nicolás Pujia
created: '2026-03-21'
acceptance_criteria:
- The Attach button has a visible right border separating it from the textarea
- The Send button has a visible left border separating it from the textarea
- No double borders appear between elements
---

In templates/project.html, the Attach button (.btn-attach, line 263-275) has border-left: 1px solid var(--border) but no border-right. The DOM order is: Attach button | textarea | Send button. The Attach button needs a border-right to visually separate it from the textarea.

WHAT TO CHANGE in templates/project.html:

1. In the .btn-attach, .btn-send CSS rule (line 263-275), the shared styles have border-left. But Attach is on the LEFT side, so it needs border-right instead. Change the approach:
   - Remove border-left from the shared .btn-attach, .btn-send rule
   - Add to .btn-attach: border-right: 1px solid var(--border);
   - Add to .btn-send: border-left: 1px solid var(--border);
   
   This gives Attach a right border (separating from textarea) and Send a left border (separating from textarea).
