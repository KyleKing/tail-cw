# Record with: mise run gif  (or: vhs docs/demo.tape)
# Charts are native plotext (Unicode), so VHS renders them faithfully.

Output docs/images/demo.gif

Set Shell bash
Set FontSize 13
Set Width 1500
Set Height 950
Set Padding 10
Set PlaybackSpeed 1.0
Set TypingSpeed 12ms

Hide
Type `cd ~/Developer/kyleking/tail-cw`  Enter
Type `clear`  Enter
Show

Type `uv run tail-cw dash --demo`  Sleep 500ms  Enter
Sleep 3s

# Focus a chart on the stage
Type ":"  Sleep 300ms
Type "focus 5xx"  Sleep 300ms
Enter
Sleep 1.2s

# Add a second chart beside it, side-by-side
Type ":"  Sleep 300ms
Type "add latency"  Sleep 300ms
Enter
Sleep 1.5s

# Dive from the recent-errors log panel into the logs behind it
Type ":"  Sleep 300ms
Type "focus recent"  Sleep 300ms
Enter
Sleep 400ms
Type "d"  Sleep 1.2s
Enter
Sleep 800ms

# Search within the dived logs
Type "/"  Sleep 300ms
Type "ERROR"  Sleep 300ms
Enter
Sleep 1.2s

Type "q"  Sleep 400ms
