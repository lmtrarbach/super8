import sys


def main() -> int:
    from super8.ui.main_window import run
    return run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
