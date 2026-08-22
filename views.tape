# Record with: mise run views  (or: vhs docs/views.tape)
# Offline synthetic data throughout, so nothing here is account data.
# Screenshot filenames must not start with a digit: VHS's parser rejects them.

Output docs/images/views-throwaway.gif

Set Shell bash
Set FontSize 13
Set Width 1400
Set Height 330
Set Padding 10
Set TypingSpeed 8ms

Hide
Type "cd ~/Developer/kyleking/tail-cw && clear" Enter
Show

Type "uv run tail-cw logs --demo" Sleep 500ms Enter
Sleep 8s

# When the events on screen happened, coloured by the worst severity per column
Type "h"
Sleep 1500ms
Screenshot docs/images/histogram.png

# One X-Ray trace as a waterfall: real intervals, real parents
Type ":" Sleep 300ms
Type "xray 1-6a89adb9-b279784d231d504c96c8815f" Sleep 300ms
Enter
Sleep 3s
Screenshot docs/images/waterfall.png
