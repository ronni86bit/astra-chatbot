import os
import sys

# Add the source directory to the path
src_dir = r"C:\Ronni\Projects\astra chatbot\src"
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)

# Now run pytest on the test module
sys.exit(os.system(f"python -m pytest tests\\test_engine.py -v --tb=short".replace("tests\\test_engine.py", os.path.join(os.getcwd(), "tests\\test_engine.py"))))