---
title: Add timeout and kill to Ralph loop stop
priority: 1
assignee: Nicolás Pujia
created: '2026-03-21'
acceptance_criteria:
- stop() returns within ~40 seconds even if Claude process is hanging
- Hanging processes are killed after 30 second timeout
- Loop task is cancelled after 10 second timeout if still running
- Normal stop (process exits on its own) works as before
---

In app/ralph_loop.py, the stop() method (line 307-325) waits indefinitely for the Claude process to finish. If Claude hangs, stop() blocks forever.

WHAT TO CHANGE in app/ralph_loop.py:

1. In the stop() method, replace the indefinite wait with a timeout:
   if self.process and self.process.returncode is None:
       try:
           await asyncio.wait_for(self.process.wait(), timeout=30)
       except asyncio.TimeoutError:
           logger.warning('Claude process did not exit in 30s, killing it')
           self.process.kill()
           await self.process.wait()

2. Similarly for the task wait:
   if self._task and not self._task.done():
       try:
           await asyncio.wait_for(self._task, timeout=10)
       except asyncio.TimeoutError:
           logger.warning('Ralph loop task did not finish in 10s, cancelling')
           self._task.cancel()
           try:
               await self._task
           except asyncio.CancelledError:
               pass
