import glob
import sys
import yaml

all_valid = True
for f in glob.glob('.github/workflows/*.yml'):
    try:
        with open(f, 'r', encoding='utf-8') as file:
            yaml.safe_load(file)
            print(f"VALID: {f}")
    except Exception as e:
        print(f"INVALID: {f}\nError: {e}")
        all_valid = False

sys.exit(0 if all_valid else 1)
