import argparse
import subprocess
import sys
from pathlib import Path

EDGE_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
]


def browser() -> Path:
    for candidate in EDGE_CANDIDATES:
        if candidate.exists():
            return candidate
    raise SystemExit("Edge veya Chrome bulunamadı")


def capture(url: str, output: Path, width: int, height: int, wait_ms: int, live: bool = False) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(browser()),
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        f"--window-size={width},{height}",
        f"--timeout={wait_ms}" if live else f"--virtual-time-budget={wait_ms}",
        f"--screenshot={output.resolve()}",
        url,
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=120)
    print(f"{output} <- {url}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Çalışan ContractFlow ekranlarının gerçek görüntüsünü alır")
    parser.add_argument("pairs", nargs="+", help="dosya_adı.png=http://127.0.0.1:8000/yol")
    parser.add_argument("--out", default="docs/screenshots")
    parser.add_argument("--width", type=int, default=1400)
    parser.add_argument("--height", type=int, default=1600)
    parser.add_argument("--wait-ms", type=int, default=6000)
    parser.add_argument("--live", action="store_true", help="Açık SSE bağlantısı olan sayfalar için gerçek süre bekler")
    arguments = parser.parse_args()
    for pair in arguments.pairs:
        name, _, url = pair.partition("=")
        capture(url, Path(arguments.out) / name, arguments.width, arguments.height, arguments.wait_ms, arguments.live)
    return 0


if __name__ == "__main__":
    sys.exit(main())
