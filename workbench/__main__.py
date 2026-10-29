import argparse
from .server import serve

parser = argparse.ArgumentParser(description="Research Atlas scientific workbench")
parser.add_argument("--port", type=int, default=8765)
parser.add_argument("--data-dir", help="Directory for persistent application data")
args = parser.parse_args()
serve(args.port, args.data_dir)
