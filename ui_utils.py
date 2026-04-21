import os
import sys
import io

# Force UTF-8 output on Windows to prevent UnicodeEncodeError with ANSI/symbols
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

class UI:
    # ANSI Colors
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    DIM = '\033[2m'
    BOLD = '\033[1m'
    RESET = '\033[0m'

    @staticmethod
    def header():
        print(f"{UI.CYAN}{UI.BOLD}DISTRIBUTED G-DRIVE STORAGE{UI.RESET}\n")

    @staticmethod
    def location(acc_count, path):
        print(f"{UI.DIM}accounts:{UI.RESET} {UI.GREEN}{acc_count}{UI.RESET}  {UI.DIM}location:{UI.RESET} {UI.CYAN}{UI.BOLD}/{path}{UI.RESET}")

    @staticmethod
    def status(msg, success=True):
        icon = f"{UI.GREEN}[OK]{UI.RESET}" if success else f"{UI.RED}[ERR]{UI.RESET}"
        print(f" {icon} {msg}")

    @staticmethod
    def info(msg):
        print(f" {UI.DIM}*{UI.RESET} {msg}")

    @staticmethod
    def progress_bar(current, total, prefix='', length=30):
        percent = (current / total) * 100
        filled = int(length * current // total)
        bar = f"{UI.CYAN}#{UI.RESET}" * filled + f"{UI.DIM}-{UI.RESET}" * (length - filled)
        print(f"\r {UI.DIM}> {prefix}{UI.RESET} [{bar}] {UI.BOLD}{percent:3.0f}%{UI.RESET}", end='', flush=True)
        if current == total:
            print() # Move to next line on completion

    @staticmethod
    def clear():
        os.system('cls' if os.name == 'nt' else 'clear')
