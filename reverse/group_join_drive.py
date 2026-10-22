"""Drive official UI: join group with a code. Usage: python group_join_drive.py <code>"""
import subprocess, sys, time
code = sys.argv[1]
def sh(*a): subprocess.run(['python', 'ui.py', *a], check=False)
sh('focus', 'flix'); time.sleep(0.5)
# ensure group pane: open + menu -> 群组同传
sh('click', '697', '535'); time.sleep(0.9)
sh('click', '595', '579'); time.sleep(0.9)
sh('click', '685', '912'); time.sleep(1.2)   # 加入群组 item
sh('click', '795', '817'); time.sleep(0.6)   # input
sh('type', code); time.sleep(0.5)
sh('click', '877', '886'); time.sleep(2.5)   # 加入
print('driven')
