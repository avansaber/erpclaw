import os, sys
sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
from erpclaw_lib.response import ok, err

def main():
    ok({"message": "ok"})

if __name__ == "__main__":
    main()
