---
description: Review long-term memory and choose what to keep or delete
---
This is my long-term memory for this project, already numbered (built in the background, so it is instant):

!`hypnos cleanup`

$ARGUMENTS

Rules (be fast: no other tool calls, no analysis):
1. Print the list above EXACTLY as it is, in a code block. Do not reword, regroup or comment on it. If it is empty or an error, call hypnos_memory_cleanup_view once and print that instead.
2. Then one line: Reply with what to delete, e.g. "1-5, 9", "T2", "all duplicates", "show T3" (older facts), or "purge". Nothing is deleted until you say so.
3. When I answer, call hypnos_memory_cleanup_apply once with my words as the selection and `space` set to the name after "Memory for" in the list (hypnos_memory_correct for "fix 3: ..."). For "show Tn", print the numbered facts it returns, exactly.
4. Reply in one line: how many facts were removed and how many remain.
