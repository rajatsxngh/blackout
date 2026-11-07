"""Run the CLI with ``python -m blackout``."""

from blackout.cli import app

# The guard matters on Windows: multiprocessing's spawn start method re-imports the entry module.
if __name__ == "__main__":
    app(prog_name="blackout")
