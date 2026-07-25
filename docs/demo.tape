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
Sleep 3.5s

# Navigate the overview grid (hjkl), then focus a chart on the stage
Type "l"  Sleep 600ms
Type "j"  Sleep 600ms
Enter
Sleep 2s

# Command line: filter the grid to error and latency panels
Type ":"  Sleep 600ms
Type "filter errors latency"  Sleep 500ms
Enter
Sleep 2s

# Reset the filter
Type ":"  Sleep 400ms
Type "filter all"  Sleep 400ms
Enter
Sleep 1s

# Command line: widen the window with tab-completion
Type ":"  Sleep 400ms
Type "ra"  Sleep 300ms
Tab
Sleep 300ms
Type " 6h"  Sleep 400ms
Enter
Sleep 1.5s

# which-key reference
Type ","  Sleep 2s
Escape
Sleep 500ms

# Dive from the errors panel into the logs behind it
Type "l"  Sleep 300ms
Type "j"  Sleep 300ms
Type "d"  Sleep 2.5s

# Search within the dived logs, then go back
Type "/"  Sleep 400ms
Type "ERROR"  Sleep 400ms
Enter
Sleep 1.5s
Escape
Sleep 1s

Type "q"  Sleep 500ms
