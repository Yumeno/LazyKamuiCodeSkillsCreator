"""CLI entry point for generate-skill command."""
import sys
import os


def main():
    scripts_dir = os.path.join(
        os.path.dirname(__file__),
        ".claude", "skills", "mcp-async-skill", "scripts",
    )
    sys.path.insert(0, scripts_dir)
    from generate_skill import main as gen_main
    gen_main()


if __name__ == "__main__":
    main()
