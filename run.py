import subprocess, sys, os
dir = os.path.dirname(os.path.abspath(__file__))
subprocess.run([sys.executable, os.path.join(dir, "매물확인.py")] + sys.argv[1:])
